"""The strategy interface.

A Strategy is a pure function of history -> Signal. It holds no broker, no
equity, no clock. That purity is the whole point: the backtester and the live
engine feed it identical Candle lists and get identical Signals, so what you
test is exactly what trades.

`on_candle` is called once per *closed* candle on the strategy's own timeframe.
`manage` is called every bar an open position exists, and is where trailing
stops live — it may tighten a stop but the engine enforces that it never widens.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from .types import Candle, Position, Signal


class Strategy(ABC):
    #: bar size this strategy consumes, e.g. "15m", "1h", "4h"
    timeframe: str
    #: minimum candles before the strategy will emit anything actionable
    warmup: int
    #: risk bucket every position from this strategy belongs to
    risk_bucket: str

    @abstractmethod
    def on_candle(self, symbol: str, candles: List[Candle]) -> Signal:
        """Decide on the latest closed candle. `candles` is oldest-first and
        includes the just-closed bar as the last element."""
        ...

    @abstractmethod
    def manage(self, position: Position, candles: List[Candle]) -> Optional[float]:
        """Return a new (tighter) stop price, or None to leave it. The engine
        rejects any value that would widen the stop."""
        ...
