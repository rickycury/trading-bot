"""Polygon.io data feed for the ETFs — deep intraday history on the free tier.

yfinance caps 15-minute bars at ~60 days, which is too short to judge the SPY/QQQ
mean-reversion edge. Polygon's free "Basic" plan gives **2 years** of intraday
aggregates (5 calls/min, data 15-min delayed — irrelevant for a historical
backtest), with no account verification: sign up with an email at
https://polygon.io/, grab the API key, set POLYGON_API_KEY (in .env or the
environment). No tax id, no funding.

One aggregates request returns up to 50k bars (2 yrs of 15m ≈ 13k), so this is
usually a single call per symbol; pagination via `next_url` is handled anyway.
429s (rate limit) are retried with a back-off. Bar timestamps are bar-start in
UTC, matching the other feeds.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from ..core.types import Candle
from .base import DataFeed, parse_timeframe, timeframe_minutes

log = logging.getLogger("bot")

try:
    import requests
    _REQUESTS_OK = True
except ImportError:  # pragma: no cover
    _REQUESTS_OK = False

_SPAN = {"m": "minute", "h": "hour", "d": "day"}


class PolygonFeed(DataFeed):
    BASE = "https://api.polygon.io"

    def __init__(self, api_key: Optional[str] = None):
        if not _REQUESTS_OK:
            raise ImportError("requests is required for PolygonFeed: pip install requests")
        self.api_key = api_key or os.getenv("POLYGON_API_KEY")
        if not self.api_key:
            raise ValueError(
                "POLYGON_API_KEY is not set. Get a free key at https://polygon.io/ "
                "(email signup, no tax id) and put POLYGON_API_KEY=... in .env"
            )

    def history(
        self, symbol: str, timeframe: str, start: datetime, end: Optional[datetime] = None
    ) -> List[Candle]:
        n, unit = parse_timeframe(timeframe)
        span = _SPAN[unit]
        end = end or datetime.now(timezone.utc)
        frm, to = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        url = (f"{self.BASE}/v2/aggs/ticker/{symbol}/range/{n}/{span}/{frm}/{to}")
        params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": self.api_key}

        out: List[Candle] = []
        while url:
            data = self._get(url, params)
            for r in data.get("results", []):
                out.append(Candle(
                    ts=datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc),
                    open=float(r["o"]), high=float(r["h"]), low=float(r["l"]),
                    close=float(r["c"]), volume=float(r.get("v", 0.0)),
                ))
            url = data.get("next_url")
            params = {"apiKey": self.api_key}  # next_url already carries the cursor
        return out

    def latest_closed(
        self, symbol: str, timeframe: str, lookback_bars: int = 5
    ) -> Optional[Candle]:
        tf_min = timeframe_minutes(timeframe)
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=tf_min * (lookback_bars + 3))
        bars = self.history(symbol, timeframe, start=start)
        closed = [b for b in bars if b.ts + timedelta(minutes=tf_min) <= now]
        return closed[-1] if closed else None

    def _get(self, url: str, params: dict) -> dict:
        for attempt in range(6):
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 429:                # free tier: 5 calls/min
                log.warning("polygon rate-limited; waiting 13s (attempt %d)", attempt + 1)
                time.sleep(13)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return {}
