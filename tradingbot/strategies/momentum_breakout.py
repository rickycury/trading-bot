"""Strategy 2 — Momentum Breakout (BTC/USD), 1-hour candles.

Break of the prior 20-bar high (low) with volume >= 1.5x the 20-bar average
goes long (short). Trails a 2x ATR chandelier stop. Unlike the indices we ride
the move rather than fade it, because crypto autocorrelates positively at this
horizon often enough to pay for the whipsaws.
"""
from __future__ import annotations

from typing import List, Optional

from ..core.strategy import Strategy
from ..core.types import Action, Candle, Position, Side, Signal
from ..indicators import atr, avg_volume, rolling_high, rolling_low


class MomentumBreakout(Strategy):
    timeframe = "1h"
    risk_bucket = "crypto"

    def __init__(
        self,
        channel: int = 20,
        vol_period: int = 20,
        vol_mult: float = 1.5,
        atr_period: int = 14,
        trail_atr_mult: float = 2.0,
    ):
        self.channel = channel
        self.vol_period = vol_period
        self.vol_mult = vol_mult
        self.atr_period = atr_period
        self.trail_atr_mult = trail_atr_mult
        self.warmup = max(channel, vol_period, atr_period) + 1

    def on_candle(self, symbol: str, candles: List[Candle]) -> Signal:
        if len(candles) < self.warmup:
            return Signal(Action.NONE, reason="warming up")

        hi = rolling_high(candles, self.channel)      # prior N-bar high
        lo = rolling_low(candles, self.channel)
        avg_v = avg_volume(candles[:-1], self.vol_period)
        a = atr(candles, self.atr_period)
        last = candles[-1]
        if None in (hi, lo, avg_v, a):
            return Signal(Action.NONE, reason="indicators unavailable")

        vol_ok = last.volume >= self.vol_mult * avg_v

        if last.close > hi and vol_ok:
            return Signal(
                Action.ENTER, side=Side.LONG,
                reference_price=last.close, stop=last.close - self.trail_atr_mult * a,
                atr=a, reason=f"break > {self.channel}-bar high {hi:.0f}, vol {last.volume/avg_v:.1f}x",
            )
        if last.close < lo and vol_ok:
            return Signal(
                Action.ENTER, side=Side.SHORT,
                reference_price=last.close, stop=last.close + self.trail_atr_mult * a,
                atr=a, reason=f"break < {self.channel}-bar low {lo:.0f}, vol {last.volume/avg_v:.1f}x",
            )
        return Signal(Action.NONE, atr=a, reason="no breakout / volume not confirmed")

    def manage(self, position: Position, candles: List[Candle]) -> Optional[float]:
        a = atr(candles, self.atr_period)
        price = candles[-1].close
        if a is None:
            return None
        if position.side is Side.LONG:
            return price - self.trail_atr_mult * a   # engine clamps so it can only rise
        return price + self.trail_atr_mult * a       # short: can only fall

    def should_exit(self, position: Position, candles: List[Candle]) -> Optional[str]:
        # Opposite-channel break is an exit even before the trail is touched.
        lo = rolling_low(candles, self.channel)
        hi = rolling_high(candles, self.channel)
        price = candles[-1].close
        if position.side is Side.LONG and lo is not None and price < lo:
            return "broke opposite channel"
        if position.side is Side.SHORT and hi is not None and price > hi:
            return "broke opposite channel"
        return None
