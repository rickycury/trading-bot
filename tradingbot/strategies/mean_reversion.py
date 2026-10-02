"""Strategy 1 — Mean Reversion (SPY, QQQ), 15-minute candles.

Fade moves beyond N standard deviations from the 20-SMA, exit back at the mean.
This is the most dangerous module because a trend day will keep going while you
keep fading it, so two guards are bolted on beyond the bare spec:

  * Regime guard: don't fade when the longer-term drift agrees with the move.
    A move *down* during an established downtrend isn't an overextension to buy,
    it's the trend. We approximate the daily trend with a slow SMA on the same
    series (configurable lookback) and refuse entries that lean into it.
  * Time stop: a reversion that hasn't reverted within `max_bars` is a thesis
    that failed quietly; cut it rather than wait for the hard stop.

The stop is placed `stop_atr_mult` ATRs beyond entry. Sizing keys off that
distance, so a noisier tape automatically shrinks the position.
"""
from __future__ import annotations

from typing import List, Optional

from ..core.strategy import Strategy
from ..core.types import Action, Candle, Position, Side, Signal
from ..indicators import atr, rolling_std, sma


class MeanReversion(Strategy):
    timeframe = "15m"
    risk_bucket = "risk_on_equity"

    def __init__(
        self,
        entry_sigma: float,          # 1.5 for SPY, 1.8 for QQQ
        sma_period: int = 20,
        atr_period: int = 14,
        stop_atr_mult: float = 2.5,
        regime_period: int = 130,    # ~ one session of 15m bars as a drift proxy
        max_bars: int = 26,          # time stop: ~one session
    ):
        self.entry_sigma = entry_sigma
        self.sma_period = sma_period
        self.atr_period = atr_period
        self.stop_atr_mult = stop_atr_mult
        self.regime_period = regime_period
        self.max_bars = max_bars
        self.warmup = max(regime_period, sma_period, atr_period) + 1

    def on_candle(self, symbol: str, candles: List[Candle]) -> Signal:
        if len(candles) < self.warmup:
            return Signal(Action.NONE, reason="warming up")

        mean = sma(candles, self.sma_period)
        sd = rolling_std(candles, self.sma_period)
        a = atr(candles, self.atr_period)
        drift = sma(candles, self.regime_period)
        price = candles[-1].close
        if None in (mean, sd, a, drift) or sd == 0:
            return Signal(Action.NONE, reason="indicators unavailable")

        z = (price - mean) / sd

        # Long setup: stretched below the mean, but only if not in a downtrend.
        if z <= -self.entry_sigma:
            if price < drift:
                return Signal(Action.NONE, atr=a,
                              reason=f"z={z:.2f} long blocked: below regime drift")
            return Signal(
                Action.ENTER, side=Side.LONG,
                reference_price=price, stop=price - self.stop_atr_mult * a,
                target=mean, atr=a,
                reason=f"z={z:.2f} <= -{self.entry_sigma}, fade up to mean {mean:.2f}",
            )

        # Short setup: stretched above the mean, but only if not in an uptrend.
        if z >= self.entry_sigma:
            if price > drift:
                return Signal(Action.NONE, atr=a,
                              reason=f"z={z:.2f} short blocked: above regime drift")
            return Signal(
                Action.ENTER, side=Side.SHORT,
                reference_price=price, stop=price + self.stop_atr_mult * a,
                target=mean, atr=a,
                reason=f"z={z:.2f} >= {self.entry_sigma}, fade down to mean {mean:.2f}",
            )

        return Signal(Action.NONE, atr=a, reason=f"z={z:.2f} inside band")

    def manage(self, position: Position, candles: List[Candle]) -> Optional[float]:
        mean = sma(candles, self.sma_period)
        price = candles[-1].close
        # Exit at the mean (target) or on time stop. Engine handles the hard stop.
        if mean is not None:
            if position.side is Side.LONG and price >= mean:
                return None  # target hit; exit handled by engine via Action below
            if position.side is Side.SHORT and price <= mean:
                return None
        return None  # MR doesn't trail; stop stays fixed at entry placement

    def should_exit(self, position: Position, candles: List[Candle]) -> Optional[str]:
        """Returns a reason string if the position should be flattened now."""
        mean = sma(candles, self.sma_period)
        price = candles[-1].close
        if mean is not None:
            if position.side is Side.LONG and price >= mean:
                return "reverted to mean"
            if position.side is Side.SHORT and price <= mean:
                return "reverted to mean"
        if position.bars_held >= self.max_bars:
            return f"time stop ({self.max_bars} bars, no reversion)"
        return None
