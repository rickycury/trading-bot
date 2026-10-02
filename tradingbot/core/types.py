"""Domain types shared across strategies, risk, and the engine.

Everything downstream speaks in these objects, which is what lets the backtest
and the live loop run the *same* strategy code: a strategy only ever consumes
Candles and emits Signals. It never touches a broker, a clock, or equity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class Action(Enum):
    ENTER = "enter"      # open a new position
    EXIT = "exit"        # close an existing position
    REVERSE = "reverse"  # close and open the opposite side
    NONE = "none"        # no actionable signal this bar


@dataclass(frozen=True)
class Candle:
    """A *closed* OHLCV bar. The engine never passes a forming candle."""
    ts: datetime          # close time of the bar (timezone-aware, UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Signal:
    """A strategy's decision on a single closed candle.

    The strategy is responsible for declaring the stop price. The sizer derives
    quantity from the stop distance, so a strategy that stops wide automatically
    gets a smaller position and the 1%-risk invariant holds regardless of which
    module fired. `stop` is mandatory for ENTER/REVERSE.
    """
    action: Action
    side: Side = Side.FLAT
    stop: Optional[float] = None          # absolute price of the protective stop
    reference_price: Optional[float] = None  # expected fill (usually candle close)
    target: Optional[float] = None        # take-profit price, if the strategy has one
    atr: Optional[float] = None           # ATR at signal time, for trailing logic
    reason: str = ""                      # human-readable, goes into the decision log

    def stop_distance(self) -> float:
        if self.stop is None or self.reference_price is None:
            raise ValueError("stop and reference_price required to size this signal")
        d = abs(self.reference_price - self.stop)
        if d <= 0:
            raise ValueError("stop distance must be positive")
        return d


@dataclass
class Position:
    symbol: str
    side: Side
    qty: float
    entry_price: float
    stop: float                  # current protective stop (may trail, never widens)
    atr_at_entry: float
    opened_at: datetime
    risk_bucket: str             # e.g. "risk_on_equity", used by the heat cap
    bars_held: int = 0

    def open_risk(self, last_price: float) -> float:
        """Dollar risk still on the table given where the stop sits now."""
        return abs(last_price - self.stop) * self.qty

    def initial_risk(self) -> float:
        return abs(self.entry_price - self.stop) * self.qty
