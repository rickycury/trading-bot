"""Routes each symbol to the right source: crypto ('BTC/USD') to the crypto feed,
everything else to the stock feed. To the engine, backtester, and run scripts it
looks like one `DataFeed`, so the multi-source setup stays invisible upstream.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from ..core.types import Candle
from .base import DataFeed, is_crypto


class CompositeFeed(DataFeed):
    def __init__(self, stock_feed: DataFeed, crypto_feed: DataFeed):
        self.stock = stock_feed
        self.crypto = crypto_feed

    def _route(self, symbol: str) -> DataFeed:
        return self.crypto if is_crypto(symbol) else self.stock

    def history(
        self, symbol: str, timeframe: str, start: datetime, end: Optional[datetime] = None
    ) -> List[Candle]:
        return self._route(symbol).history(symbol, timeframe, start, end)

    def latest_closed(
        self, symbol: str, timeframe: str, lookback_bars: int = 5
    ) -> Optional[Candle]:
        return self._route(symbol).latest_closed(symbol, timeframe, lookback_bars)


def free_feed(crypto_exchange: str = "kraken") -> CompositeFeed:
    """The no-account stack: yfinance for ETFs, CCXT for BTC. Imports are local so
    this module loads even if one of the optional deps is missing."""
    from .ccxt_feed import CCXTFeed
    from .yfinance_feed import YFinanceFeed
    return CompositeFeed(YFinanceFeed(), CCXTFeed(exchange=crypto_exchange))


def polygon_feed(api_key: str = None, crypto_exchange: str = "kraken") -> CompositeFeed:
    """Deeper-history stack: Polygon for ETFs (2yr of 15m on the free tier),
    CCXT for BTC. Needs a free Polygon key (POLYGON_API_KEY)."""
    from .ccxt_feed import CCXTFeed
    from .polygon_feed import PolygonFeed
    return CompositeFeed(PolygonFeed(api_key), CCXTFeed(exchange=crypto_exchange))
