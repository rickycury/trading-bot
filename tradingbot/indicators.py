"""Hand-rolled indicators.

Five rules don't justify a TA dependency, and rolling your own removes any
ambiguity about how EMA is seeded or whether ATR uses Wilder smoothing. All
functions take a list of Candles (oldest-first) and return the value *as of the
last candle*, or None when there isn't enough history. Closed candles only.
"""
from __future__ import annotations

from typing import List, Optional

from .core.types import Candle


def _closes(candles: List[Candle]) -> List[float]:
    return [c.close for c in candles]


def sma(candles: List[Candle], period: int) -> Optional[float]:
    if len(candles) < period:
        return None
    return sum(_closes(candles[-period:])) / period


def rolling_std(candles: List[Candle], period: int) -> Optional[float]:
    """Population std of closes over `period` (matches a 20-period band spec)."""
    if len(candles) < period:
        return None
    window = _closes(candles[-period:])
    mean = sum(window) / period
    var = sum((x - mean) ** 2 for x in window) / period
    return var ** 0.5


def ema(candles: List[Candle], period: int) -> Optional[float]:
    """SMA-seeded EMA. Walks the whole series so the seed is deterministic."""
    if len(candles) < period:
        return None
    closes = _closes(candles)
    k = 2.0 / (period + 1)
    val = sum(closes[:period]) / period          # seed
    for price in closes[period:]:
        val = price * k + val * (1 - k)
    return val


def true_range(prev_close: float, c: Candle) -> float:
    return max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))


def atr(candles: List[Candle], period: int = 14) -> Optional[float]:
    """Wilder's ATR. Needs period+1 candles (first TR needs a prior close)."""
    if len(candles) < period + 1:
        return None
    trs = [true_range(candles[i - 1].close, candles[i]) for i in range(1, len(candles))]
    val = sum(trs[:period]) / period             # Wilder seed
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return val


def rolling_high(candles: List[Candle], period: int, exclude_last: bool = True) -> Optional[float]:
    """Highest high over `period`. exclude_last avoids comparing a bar to itself
    when testing for a breakout *of the prior* N-period high."""
    src = candles[:-1] if exclude_last else candles
    if len(src) < period:
        return None
    return max(c.high for c in src[-period:])


def rolling_low(candles: List[Candle], period: int, exclude_last: bool = True) -> Optional[float]:
    src = candles[:-1] if exclude_last else candles
    if len(src) < period:
        return None
    return min(c.low for c in src[-period:])


def avg_volume(candles: List[Candle], period: int) -> Optional[float]:
    if len(candles) < period:
        return None
    return sum(c.volume for c in candles[-period:]) / period
