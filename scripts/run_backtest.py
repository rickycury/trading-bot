"""Run a backtest over all five instruments through the real Engine.

    python3 -m scripts.run_backtest                 # synthetic data, runs offline
    python3 -m scripts.run_backtest --source alpaca --years 2

Synthetic mode generates deterministic OHLCV so you can smoke-test the entire
pipeline (sizing, risk gates, stops, drawdown breaker, metrics) with no keys and
no network. It is NOT a measure of edge — random data has none. Use --source
alpaca with real keys to measure anything you'd act on.

Run from the project root (the directory that contains the `tradingbot/` and
`scripts/` packages).
"""
from __future__ import annotations

import argparse
import logging
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from tradingbot.backtest import BacktestBroker, Backtester
from tradingbot.core.engine import Engine
from tradingbot.core.risk import RiskConfig, RiskManager, RiskState
from tradingbot.core.sizing import InstrumentSpec
from tradingbot.core.state import Store
from tradingbot.core.types import Candle
from tradingbot.strategies.mean_reversion import MeanReversion
from tradingbot.strategies.momentum_breakout import MomentumBreakout
from tradingbot.strategies.trend_following import TrendFollowing

# symbol -> (alpaca symbol, timeframe, minutes-per-bar, strategy, spec, is_crypto)
INSTRUMENTS = {
    "SPY": ("SPY", "15m", 15, MeanReversion(entry_sigma=1.5), InstrumentSpec("SPY", fractional=True), False),
    "QQQ": ("QQQ", "15m", 15, MeanReversion(entry_sigma=1.8), InstrumentSpec("QQQ", fractional=True), False),
    "BTC": ("BTC/USD", "1h", 60, MomentumBreakout(), InstrumentSpec("BTC", fractional=True, min_qty=0.0001), True),
    "GLD": ("GLD", "4h", 240, TrendFollowing(), InstrumentSpec("GLD", fractional=True), False),
    "USO": ("USO", "4h", 240, TrendFollowing(), InstrumentSpec("USO", fractional=True), False),
}


def synthetic_series(start_price: float, minutes: int, n: int, seed: int) -> List[Candle]:
    """Random walk with drifting regimes and volume spikes on big moves, so all
    three strategy types have something to react to. Deterministic per seed."""
    rng = random.Random(seed)
    t0 = datetime(2023, 1, 1, tzinfo=timezone.utc)
    price = start_price
    drift = 0.0
    out: List[Candle] = []
    for i in range(n):
        if i % 300 == 0:                       # occasional regime shift -> EMA crosses
            drift = rng.uniform(-0.0009, 0.0009)
        ret = drift + rng.gauss(0, 0.004)      # drift + mean-zero noise -> reversions
        new = max(0.01, price * (1.0 + ret))
        hi = max(price, new) * (1.0 + abs(rng.gauss(0, 0.0015)))
        lo = min(price, new) * (1.0 - abs(rng.gauss(0, 0.0015)))
        vol = rng.uniform(8e5, 1.2e6)
        if abs(ret) > 0.008:                   # volume confirmation for breakouts
            vol *= rng.uniform(1.6, 3.0)
        out.append(Candle(ts=t0 + timedelta(minutes=minutes * i),
                          open=price, high=hi, low=lo, close=new, volume=vol))
        price = new
    return out


def load_synthetic(symbols) -> Dict[str, List[Candle]]:
    # bar counts chosen so even the 200-period 4h trend filter warms up and trades
    counts = {15: 4000, 60: 3000, 240: 1500}
    starts = {"SPY": 450, "QQQ": 380, "BTC": 30000, "GLD": 185, "USO": 75}
    data = {}
    for i, (sym, (_, _, minutes, _, _, _)) in enumerate(INSTRUMENTS.items()):
        if sym not in symbols:
            continue  # seed stays tied to the fixed index, so filtering is deterministic
        data[sym] = synthetic_series(starts[sym], minutes, counts[minutes], seed=42 + i)
    return data


def build_feed(source: str, exchange: str):
    if source == "alpaca":
        from tradingbot.data.alpaca import AlpacaDataFeed
        return AlpacaDataFeed()
    if source == "free":
        from tradingbot.data.composite import free_feed
        return free_feed(crypto_exchange=exchange)
    if source == "polygon":
        from tradingbot.data.composite import polygon_feed
        return polygon_feed(crypto_exchange=exchange)
    raise ValueError(source)


def load_from_feed(feed, years: float, symbols) -> Dict[str, List[Candle]]:
    """Works for any feed with the common interface; crypto routing/inference is
    the feed's job (BTC/USD carries a '/')."""
    from tradingbot.session import in_session
    start = datetime.now(timezone.utc) - timedelta(days=int(365 * years))
    data = {}
    for sym, (asym, tf, _, _, _, _) in INSTRUMENTS.items():
        if sym not in symbols:
            continue
        bars = feed.history(asym, tf, start=start)
        # Match live: the session guard skips out-of-hours bars, so the backtest
        # must too (Polygon returns pre/post-market 15m bars; yfinance doesn't).
        kept = [b for b in bars if in_session(b.ts, asym)]
        logging.warning("fetched %d %s bars for %s (%s); %d in-session",
                        len(bars), tf, sym, asym, len(kept))
        data[sym] = kept
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["synthetic", "free", "polygon", "alpaca"], default="synthetic")
    ap.add_argument("--years", type=float, default=2.0, help="history window for free/alpaca")
    ap.add_argument("--exchange", default="kraken", help="ccxt exchange for BTC (free source)")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated subset, e.g. BTC or SPY,QQQ (default: all). "
                         "Isolating instruments avoids mixing very different history depths.")
    ap.add_argument("--equity", type=float, default=100_000.0)
    ap.add_argument("--no-halt", action="store_true",
                    help="disable the 10%% drawdown breaker so you see the full-period "
                         "edge instead of the strategy freezing after the first -10%%")
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--fee-bps", type=float, default=1.0)
    ap.add_argument("--plot", default="equity_curve.png")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    from tradingbot.env import load_env
    load_env()   # picks up ALPACA_* for --source alpaca

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        unknown = [s for s in symbols if s not in INSTRUMENTS]
        if unknown:
            ap.error(f"unknown symbol(s) {unknown}; choose from {list(INSTRUMENTS)}")
    else:
        symbols = list(INSTRUMENTS)

    if args.source == "synthetic":
        data = load_synthetic(symbols)
    else:
        data = load_from_feed(build_feed(args.source, args.exchange), args.years, symbols)

    strategies = {s: INSTRUMENTS[s][3] for s in symbols}
    specs = {s: INSTRUMENTS[s][4] for s in symbols}
    broker = BacktestBroker(args.equity, slippage_bps=args.slippage_bps, fee_bps=args.fee_bps)
    cfg = RiskConfig(max_drawdown=1.0) if args.no_halt else RiskConfig()
    risk = RiskManager(cfg, RiskState(peak_equity=args.equity))
    engine = Engine(strategies, specs, risk, broker, Store(":memory:"))

    result = Backtester(engine, broker, data).run()
    print(result.summary())
    saved = result.plot(args.plot)
    if saved:
        print(f"\nEquity curve written to {saved}")


if __name__ == "__main__":
    main()
