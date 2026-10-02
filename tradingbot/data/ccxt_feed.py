"""CCXT data feed for BTC/USD.

Free, no account, no API key — pulls public OHLCV straight from a crypto
exchange. `pip install ccxt`.

Exchange default is Kraken: reliable, US-accessible, native BTC/USD, and it
serves the 1-hour bars the momentum strategy needs with zero config. Its public
OHLC endpoint only retains the most recent ~720 bars per timeframe, which is
plenty for the live loop's warmup (~260 bars) but shallow for deep backtests.
For multi-year BTC history, pass exchange='coinbase' or 'binanceus', which
paginate further back. The code paginates generically, so it just works either
way — it stops when the exchange stops handing back older data.

Timestamps are bar-start times in UTC, consistent with the other feeds.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from ..core.types import Candle
from .base import DataFeed, timeframe_minutes

log = logging.getLogger("bot")

try:
    import ccxt
    _CCXT_OK = True
except ImportError:  # pragma: no cover
    _CCXT_OK = False


def _require() -> None:
    if not _CCXT_OK:
        raise ImportError(
            "ccxt is not installed. Run:  pip install ccxt\n"
            "(needed for the free BTC data feed)"
        )


class CCXTFeed(DataFeed):
    def __init__(self, exchange: str = "kraken", page_limit: int = 720):
        _require()
        if not hasattr(ccxt, exchange):
            raise ValueError(f"unknown ccxt exchange {exchange!r}")
        self.ex = getattr(ccxt, exchange)({"enableRateLimit": True})
        self.page_limit = page_limit

    def history(
        self, symbol: str, timeframe: str, start: datetime, end: Optional[datetime] = None
    ) -> List[Candle]:
        _require()
        tf_ms = timeframe_minutes(timeframe) * 60_000
        since = int(start.timestamp() * 1000)
        end_ms = int((end or datetime.now(timezone.utc)).timestamp() * 1000)

        rows: List[list] = []
        last_seen = -1
        # Page forward until we reach the end or the exchange stops advancing.
        # Don't break on a short page: some exchanges (e.g. Coinbase) cap a
        # response at 300 even when more history exists, so a sub-`page_limit`
        # page is normal, not the end. Progress (`newest > last_seen`) is the
        # real terminator; `max_pages` is a safety stop.
        for _ in range(2000):
            if since >= end_ms:
                break
            batch = self.ex.fetch_ohlcv(symbol, timeframe, since=since, limit=self.page_limit)
            if not batch:
                break
            rows.extend(batch)
            newest = batch[-1][0]
            if newest <= last_seen:        # no forward progress -> exchange done
                break
            last_seen = newest
            since = newest + tf_ms

        # de-dupe (paginated batches can overlap) and trim to the window
        seen = set()
        out: List[Candle] = []
        for ts_ms, o, h, l, c, v in rows:
            if ts_ms in seen or ts_ms > end_ms:
                continue
            seen.add(ts_ms)
            out.append(Candle(
                ts=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v),
            ))
        out.sort(key=lambda c: c.ts)
        return out

    def latest_closed(
        self, symbol: str, timeframe: str, lookback_bars: int = 5
    ) -> Optional[Candle]:
        _require()
        tf_min = timeframe_minutes(timeframe)
        batch = self.ex.fetch_ohlcv(symbol, timeframe, limit=lookback_bars + 3)
        if not batch:
            return None
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        # last element is usually the still-forming bar; keep only ended windows
        closed = [b for b in batch if b[0] + tf_min * 60_000 <= now_ms]
        if not closed:
            return None
        ts_ms, o, h, l, c, v = closed[-1]
        return Candle(
            ts=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
            open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v),
        )
