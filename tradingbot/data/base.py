"""Data-feed interface shared by every source (Alpaca, yfinance, CCXT, ...).

A feed does exactly two things: hand back bulk history (to warm a buffer or feed
the backtester) and hand back the latest *closed* bar (to drive the live loop).
Keeping the surface this small is what lets sources be swapped without touching
the engine, the backtester, or the run scripts.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from ..core.types import Candle

_TF_RE = re.compile(r"^\s*(\d+)\s*([mhdMHD])\s*$")
_UNIT_MIN = {"m": 1, "h": 60, "d": 1440}


def parse_timeframe(tf: str):
    """'15m' -> (15, 'm'). Raises on anything unrecognized."""
    m = _TF_RE.match(tf)
    if not m:
        raise ValueError(f"unrecognized timeframe {tf!r} (use e.g. '15m', '1h', '4h')")
    return int(m.group(1)), m.group(2).lower()


def timeframe_minutes(tf: str) -> int:
    n, unit = parse_timeframe(tf)
    return n * _UNIT_MIN[unit]


def is_crypto(symbol: str) -> bool:
    """Our convention: crypto symbols carry a quote currency, e.g. 'BTC/USD'."""
    return "/" in symbol


class DataFeed(ABC):
    @abstractmethod
    def history(
        self, symbol: str, timeframe: str, start: datetime, end: Optional[datetime] = None
    ) -> List[Candle]:
        """Oldest-first closed bars in [start, end]."""
        ...

    @abstractmethod
    def latest_closed(
        self, symbol: str, timeframe: str, lookback_bars: int = 5
    ) -> Optional[Candle]:
        """The most recent *fully closed* bar, or None."""
        ...
