"""Wiring example — five instruments, three strategies, alert-only to start.

This is the file you'd actually run. Note every instrument starts in AlertOnly:
nothing trades until you've watched the kill switches fire on paper and promote
modules to SemiAuto / Live one at a time.
"""
from __future__ import annotations

import logging

from .core.engine import Engine
from .core.execution import AlertOnly, ConsoleNotifier
from .core.risk import RiskConfig, RiskManager, RiskState
from .core.sizing import InstrumentSpec
from .core.state import Store
from .strategies.mean_reversion import MeanReversion
from .strategies.momentum_breakout import MomentumBreakout
from .strategies.trend_following import TrendFollowing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def build(starting_equity: float = 100_000.0) -> Engine:
    strategies = {
        "SPY": MeanReversion(entry_sigma=1.5),
        "QQQ": MeanReversion(entry_sigma=1.8),   # wider band, Nasdaq is noisier
        "BTC": MomentumBreakout(),
        "GLD": TrendFollowing(),
        "USO": TrendFollowing(),                 # remember: USO != spot WTI
    }
    specs = {
        "SPY": InstrumentSpec("SPY"),
        "QQQ": InstrumentSpec("QQQ"),
        "BTC": InstrumentSpec("BTC", fractional=True, min_qty=0.0001),
        "GLD": InstrumentSpec("GLD"),
        "USO": InstrumentSpec("USO"),
    }
    store = Store("bot_state.db")
    state = RiskState(peak_equity=store.get_peak(starting_equity))
    halted, reason = store.get_halt()
    state.halted, state.halt_reason = halted, reason

    risk = RiskManager(RiskConfig(), state)
    execution = AlertOnly(ConsoleNotifier())
    return Engine(strategies, specs, risk, execution, store)


if __name__ == "__main__":
    bot = build()
    print("Engine built. Feed it closed candles via bot.on_bar_close(symbol, candles, equity)")
    print(f"Halted: {bot.risk.state.halted}  Peak equity: ${bot.risk.state.peak_equity:,.0f}")
