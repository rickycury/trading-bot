"""The engine.

Ties everything together and owns the rules that no single strategy can see:
act only on *closed* candles, enforce hard stops, run the risk gates, persist
state, and flatten everything if the data feed goes quiet (dead-man's switch).

The loop is event-driven: a scheduler (out of scope here — cron, asyncio, or a
broker's bar stream) calls `on_bar_close(symbol, candles)` exactly once when a
bar on that symbol's timeframe finishes. The engine never sees a forming bar.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List

from .execution import Execution, decision_log
from .risk import RiskManager
from .sizing import InstrumentSpec, InsufficientSizing, realized_risk, size_position
from .state import Store
from .strategy import Strategy
from .types import Action, Candle, Position, Side, Signal


class Engine:
    def __init__(
        self,
        strategies: Dict[str, Strategy],     # symbol -> strategy instance
        specs: Dict[str, InstrumentSpec],    # symbol -> instrument spec
        risk: RiskManager,
        execution: Execution,
        store: Store,
        heartbeat_timeout_s: float = 300.0,  # flatten if no data for 5 min
    ):
        self.strats = strategies
        self.specs = specs
        self.risk = risk
        self.exec = execution
        self.store = store
        self.heartbeat_timeout = heartbeat_timeout_s
        self.positions: Dict[str, Position] = store.load_positions()
        self.last_prices: Dict[str, float] = {}
        self._last_data_ts: float = time.time()

    # ---- the per-bar entry point -----------------------------------------
    def on_bar_close(self, symbol: str, candles: List[Candle], equity: float) -> None:
        self._last_data_ts = time.time()
        if not candles:
            return
        price = candles[-1].close
        self.last_prices[symbol] = price
        strat = self.strats[symbol]

        # 1. update equity + drawdown breaker BEFORE any new risk
        self.risk.update_equity(equity)
        self.store.set_peak(self.risk.state.peak_equity)
        if self.risk.state.halted:
            self.store.set_halt(True, self.risk.state.halt_reason)
            self._flatten_all("drawdown circuit breaker")
            return

        pos = self.positions.get(symbol)

        # 2. manage an open position first: hard stop, trail, strategy exits
        if pos is not None:
            pos.bars_held += 1
            if self._hard_stop_hit(pos, candles[-1]):
                self._close(pos, pos.stop, "hard stop hit")
                return
            exit_reason = getattr(strat, "should_exit", lambda *_: None)(pos, candles)
            if exit_reason:
                self._close(pos, price, exit_reason)
                return
            proposed = strat.manage(pos, candles)
            new_stop = RiskManager.clamp_stop(pos, proposed)
            if new_stop != pos.stop:
                pos.stop = new_stop
                self.store.upsert_position(pos)
                self.exec.move_stop(pos, new_stop)
            else:
                self.store.upsert_position(pos)
            return

        # 3. no position: look for an entry
        signal = strat.on_candle(symbol, candles)
        if signal.action not in (Action.ENTER, Action.REVERSE):
            decision_log(symbol, signal, "no-op")
            return

        # Cap by free buying power: a cash account can't deploy more than the
        # capital not already tied up in open positions.
        open_notional = sum(
            p.qty * self.last_prices.get(p.symbol, p.entry_price)
            for p in self.positions.values()
        )
        free_cash = max(0.0, equity - open_notional)
        try:
            qty = size_position(equity, self.risk.cfg.risk_per_trade, signal,
                                self.specs[symbol], max_notional=free_cash)
        except InsufficientSizing as e:
            decision_log(symbol, signal, f"skip: {e}")
            return

        new_risk = realized_risk(qty, signal, self.specs[symbol])
        ok, why = self.risk.can_enter(
            signal, new_risk, equity, list(self.positions.values()),
            self.last_prices, strat.risk_bucket)
        if not ok:
            decision_log(symbol, signal, f"blocked: {why}")
            return

        opened = self.exec.open(symbol, signal, qty)
        decision_log(symbol, signal, f"ENTER qty={qty} risk=${new_risk:,.0f}")
        # In alert-only mode `opened` is None; we don't track a phantom position.
        if opened is not None:
            opened.risk_bucket = strat.risk_bucket  # so bucket/heat caps see it
            self.positions[symbol] = opened
            self.store.upsert_position(opened)

    # ---- dead-man's switch ------------------------------------------------
    def note_data_received(self) -> None:
        """Feed the dead-man's switch on every successful data poll, not only when
        a new bar closes. Without this, the normal gap between bar closes (up to
        4h here) looks like a feed outage and trips the heartbeat."""
        self._last_data_ts = time.time()

    def check_heartbeat(self) -> None:
        """Call on a timer independent of the data feed. If the feed has gone
        silent past the timeout we can't trust stops to fire, so flatten."""
        if time.time() - self._last_data_ts > self.heartbeat_timeout:
            self._flatten_all("heartbeat timeout: data feed silent")

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _hard_stop_hit(pos: Position, bar: Candle) -> bool:
        # Intrabar: a long is stopped if the bar's low pierced the stop.
        return (pos.side is Side.LONG and bar.low <= pos.stop) or \
               (pos.side is Side.SHORT and bar.high >= pos.stop)

    def _close(self, pos: Position, price: float, reason: str) -> None:
        self.exec.close(pos, price, reason)
        self.positions.pop(pos.symbol, None)
        self.store.delete_position(pos.symbol)

    def _flatten_all(self, reason: str) -> None:
        for pos in list(self.positions.values()):
            self._close(pos, self.last_prices.get(pos.symbol, pos.entry_price), reason)
