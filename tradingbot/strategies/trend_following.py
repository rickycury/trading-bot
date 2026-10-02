"""Strategy 3 — Trend Following (GLD, USO), 4-hour candles.

50/200 EMA crossover, long above / short (or flat) below, trailing a 3x ATR
stop. Be honest about cadence: 200 4h-bars is ~100+ trading days, so crossovers
are rare — this is a regime filter, not an active trader. That's a feature for
commodities, not a bug.

Caveat the engine config should carry: USO suffers roll/contango decay and is a
poor proxy for spot WTI. Backtest on USO's own price series, never on crude.
"""
from __future__ import annotations

from typing import List, Optional

from ..core.strategy import Strategy
from ..core.types import Action, Candle, Position, Side, Signal
from ..indicators import atr, ema


class TrendFollowing(Strategy):
    timeframe = "4h"
    risk_bucket = "commodity"

    def __init__(
        self,
        fast: int = 50,
        slow: int = 200,
        atr_period: int = 14,
        trail_atr_mult: float = 3.0,
        allow_short: bool = True,
    ):
        self.fast = fast
        self.slow = slow
        self.atr_period = atr_period
        self.trail_atr_mult = trail_atr_mult
        self.allow_short = allow_short
        self.warmup = slow + 1

    def _cross_state(self, candles: List[Candle]) -> Optional[tuple]:
        """Return (fast_now>slow_now, fast_prev>slow_prev) or None if not ready."""
        if len(candles) < self.slow + 1:
            return None
        f_now, s_now = ema(candles, self.fast), ema(candles, self.slow)
        f_prev, s_prev = ema(candles[:-1], self.fast), ema(candles[:-1], self.slow)
        if None in (f_now, s_now, f_prev, s_prev):
            return None
        return (f_now > s_now, f_prev > s_prev)

    def on_candle(self, symbol: str, candles: List[Candle]) -> Signal:
        state = self._cross_state(candles)
        a = atr(candles, self.atr_period)
        if state is None or a is None:
            return Signal(Action.NONE, reason="warming up")
        now_up, prev_up = state
        price = candles[-1].close

        if now_up and not prev_up:   # golden cross
            return Signal(
                Action.ENTER, side=Side.LONG,
                reference_price=price, stop=price - self.trail_atr_mult * a, atr=a,
                reason=f"{self.fast}/{self.slow} EMA golden cross",
            )
        if not now_up and prev_up:   # death cross
            side = Side.SHORT if self.allow_short else Side.FLAT
            action = Action.ENTER if self.allow_short else Action.EXIT
            return Signal(
                action, side=side,
                reference_price=price,
                stop=(price + self.trail_atr_mult * a) if self.allow_short else None,
                atr=a, reason=f"{self.fast}/{self.slow} EMA death cross",
            )
        return Signal(Action.NONE, atr=a, reason="no cross")

    def manage(self, position: Position, candles: List[Candle]) -> Optional[float]:
        a = atr(candles, self.atr_period)
        price = candles[-1].close
        if a is None:
            return None
        if position.side is Side.LONG:
            return price - self.trail_atr_mult * a
        return price + self.trail_atr_mult * a

    def should_exit(self, position: Position, candles: List[Candle]) -> Optional[str]:
        state = self._cross_state(candles)
        if state is None:
            return None
        now_up, _ = state
        if position.side is Side.LONG and not now_up:
            return "trend flipped down"
        if position.side is Side.SHORT and now_up:
            return "trend flipped up"
        return None
