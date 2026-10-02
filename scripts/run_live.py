"""Live loop — AlertOnly, Alpaca data in, Telegram alerts out.

This is the file you'd actually run once you've validated edges in the
backtester. It starts every instrument in AlertOnly: the engine computes real
signals, sizes them, runs every risk gate, and pushes an alert to your phone —
but places no orders. Nothing trades until you swap AlertOnly for AlpacaExecution
on a strategy you've decided to promote, one at a time.

The scheduler here is deliberately simple: poll each symbol on its own cadence
for a newly *closed* bar, keep a rolling candle buffer, and call the engine once
per new bar. Swap it for your broker's bar stream or an asyncio scheduler when
you outgrow polling. The dead-man's switch (`check_heartbeat`) runs on the same
loop tick but off the data timestamp, so a silent feed still flattens.

Env:  ALPACA_API_KEY, ALPACA_SECRET_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from tradingbot.env import load_env
from tradingbot.session import in_session
from tradingbot.core.engine import Engine
from tradingbot.core.execution import PaperExecution
from tradingbot.core.risk import RiskConfig, RiskManager, RiskState
from tradingbot.core.sizing import InstrumentSpec
from tradingbot.core.state import Store
from tradingbot.core.types import Candle
from tradingbot.data.composite import free_feed
from tradingbot.notify.telegram import TelegramNotifier
from tradingbot.strategies.mean_reversion import MeanReversion
from tradingbot.strategies.momentum_breakout import MomentumBreakout
from tradingbot.strategies.trend_following import TrendFollowing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# symbol -> (alpaca symbol, timeframe, minutes, strategy, spec, is_crypto)
INSTRUMENTS = {
    "SPY": ("SPY", "15m", 15, MeanReversion(entry_sigma=1.5), InstrumentSpec("SPY", fractional=True), False),
    "QQQ": ("QQQ", "15m", 15, MeanReversion(entry_sigma=1.8), InstrumentSpec("QQQ", fractional=True), False),
    "BTC": ("BTC/USD", "1h", 60, MomentumBreakout(), InstrumentSpec("BTC", fractional=True, min_qty=0.0001), True),
    "GLD": ("GLD", "4h", 240, TrendFollowing(), InstrumentSpec("GLD", fractional=True), False),
    "USO": ("USO", "4h", 240, TrendFollowing(), InstrumentSpec("USO", fractional=True), False),
}

STARTING_EQUITY = 1_000.0
BUFFER_BARS = 260  # keep enough history for the 200-period warmup + headroom


def get_equity() -> float:
    """Replace with AlpacaExecution account equity once you trade live. In
    AlertOnly there is no broker balance, so we anchor to a fixed figure for
    sizing math."""
    return STARTING_EQUITY


def warmup_start(minutes: int, crypto: bool, bars: int = BUFFER_BARS) -> datetime:
    """Calendar lookback that actually contains `bars` *trading* bars. Intraday
    equity bars are sparse (RTH ~6.5h/day, 5 days/week), so N bars span far more
    wall-clock time than N*minutes. Crypto is 24/7. Includes a 50% + 5-day cushion
    for holidays/half-days; the feed clamps to its own history limits."""
    per_calendar_day = (1440.0 if crypto else 390.0 * 5 / 7) / minutes
    days = (bars + 5) / per_calendar_day * 1.5 + 5
    return datetime.now(timezone.utc) - timedelta(days=days)


def build_engine() -> Engine:
    strategies = {s: INSTRUMENTS[s][3] for s in INSTRUMENTS}
    specs = {s: INSTRUMENTS[s][4] for s in INSTRUMENTS}
    store = Store("bot_state.db")
    state = RiskState(peak_equity=store.get_peak(STARTING_EQUITY))
    state.halted, state.halt_reason = store.get_halt()
    risk = RiskManager(RiskConfig(), state)

    notifier = TelegramNotifier()
    notifier.self_test()  # surface a broken token at startup, not on first alert
    # PaperExecution tracks positions (no real orders) so you get full-lifecycle
    # alerts and the portfolio rules actually fire. Swap for AlpacaExecution to go live.
    execution = PaperExecution(notifier, STARTING_EQUITY)
    return Engine(strategies, specs, risk, execution, store)


def main(poll_seconds: float = 30.0) -> None:
    load_env()   # pull TELEGRAM_* (and any ALPACA_*) from .env
    engine = build_engine()
    feed = free_feed()   # yfinance for ETFs, CCXT (Kraken) for BTC — no account needed

    # Warm each symbol's rolling buffer with history up front.
    buffers: Dict[str, List[Candle]] = {}
    last_ts: Dict[str, datetime] = {}
    for sym, (asym, tf, minutes, _, _, crypto) in INSTRUMENTS.items():
        start = warmup_start(minutes, crypto)
        bars = feed.history(asym, tf, start=start)  # feed routes crypto by symbol
        buffers[sym] = bars[-BUFFER_BARS:]
        last_ts[sym] = bars[-1].ts if bars else datetime.min.replace(tzinfo=timezone.utc)
        log.info("warmed %s with %d bars (last %s)", sym, len(buffers[sym]),
                 last_ts[sym].isoformat() if bars else "none")

    log.info("Live loop started (PaperExecution, tracked paper positions). Halted=%s",
             engine.risk.state.halted)
    while True:
        for sym, (asym, tf, _, _, _, _) in INSTRUMENTS.items():
            try:
                bar = feed.latest_closed(asym, tf)  # feed routes crypto by symbol
            except Exception as e:
                log.error("feed error for %s: %s", sym, e)
                continue
            if bar is None:
                continue
            engine.note_data_received()  # successful fetch feeds the dead-man's switch
            if bar.ts <= last_ts[sym]:
                continue  # no new closed bar yet
            buffers[sym].append(bar)
            buffers[sym] = buffers[sym][-BUFFER_BARS:]
            last_ts[sym] = bar.ts
            # Keep the buffer fresh for indicators, but only act on bars that
            # fall inside the instrument's trading session (ETFs: RTH only).
            if not in_session(bar.ts, asym):
                log.debug("skip %s bar %s: outside session", sym, bar.ts.isoformat())
                continue
            engine.on_bar_close(sym, buffers[sym], get_equity())

        engine.check_heartbeat()  # off wall-clock, independent of the feed
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
