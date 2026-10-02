"""yfinance data feed for the ETFs (SPY/QQQ/GLD/USO).

Free, no account, no API key, no tax id — just `pip install yfinance`. The
tradeoffs are real and worth stating plainly:

  * Intraday history is capped by Yahoo: 15-minute bars only go back ~60 days,
    60-minute bars ~730 days. We clamp the requested start to those windows and
    log a warning rather than silently returning a short series.
  * There is no native 4-hour interval, so 4h is built by pulling 60m bars and
    resampling (open=first, high=max, low=min, close=last, volume=sum).
  * Data is delayed (~15 min) and unofficial — fine for manual/semi-auto, where
    you place the trade yourself, but not something to wire straight to live
    auto-execution.

Bar timestamps are bar-*start* times in UTC, matching the Alpaca adapter, so the
backtester's event ordering is consistent across sources.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from ..core.types import Candle
from .base import DataFeed, timeframe_minutes

log = logging.getLogger("bot")

try:
    import pandas as pd
    import yfinance as yf
    _YF_OK = True
except ImportError:  # pragma: no cover
    _YF_OK = False

# Yahoo's hard limits on how far back each intraday interval is available (days).
_INTRADAY_MAX_DAYS = {"15m": 59, "30m": 59, "60m": 729}
# our timeframe string -> yfinance interval (4h handled by resampling 60m)
_INTERVAL = {"15m": "15m", "30m": "30m", "1h": "60m", "4h": "60m"}


def _require() -> None:
    if not _YF_OK:
        raise ImportError(
            "yfinance is not installed. Run:  pip install yfinance\n"
            "(needed for the free ETF data feed)"
        )


class YFinanceFeed(DataFeed):
    def history(
        self, symbol: str, timeframe: str, start: datetime, end: Optional[datetime] = None
    ) -> List[Candle]:
        _require()
        interval = _INTERVAL.get(timeframe)
        if interval is None:
            raise ValueError(f"YFinanceFeed: unsupported timeframe {timeframe!r}")

        start = self._clamp_start(interval, start)
        df = self._download(symbol, interval, start, end)
        if df is None or df.empty:
            return []
        if timeframe == "4h":
            df = self._resample(df, "4h")
        return self._to_candles(df)

    def latest_closed(
        self, symbol: str, timeframe: str, lookback_bars: int = 5
    ) -> Optional[Candle]:
        _require()
        tf_min = timeframe_minutes(timeframe)
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=tf_min * (lookback_bars + 3))
        bars = self.history(symbol, timeframe, start=start)
        # Yahoo includes the in-progress bar; keep only bars whose window has ended.
        closed = [b for b in bars if b.ts + timedelta(minutes=tf_min) <= now]
        return closed[-1] if closed else None

    # ---- internals --------------------------------------------------------
    @staticmethod
    def _clamp_start(interval: str, start: datetime) -> datetime:
        max_days = _INTRADAY_MAX_DAYS.get(interval)
        if max_days is None:
            return start
        floor = datetime.now(timezone.utc) - timedelta(days=max_days)
        if start < floor:
            log.warning("yfinance: %s history capped at ~%d days; clamping start %s -> %s",
                        interval, max_days, start.date(), floor.date())
            return floor
        return start

    @staticmethod
    def _download(symbol: str, interval: str, start: datetime, end: Optional[datetime]):
        t = yf.Ticker(symbol)
        df = t.history(start=start, end=end, interval=interval, auto_adjust=True)
        if df is None or df.empty:
            return df
        # normalize index to tz-aware UTC
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")
        return df

    @staticmethod
    def _resample(df, timeframe: str):
        rule = {"4h": "4h"}[timeframe]
        agg = df.resample(rule, label="left", closed="left").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        )
        return agg.dropna(subset=["Open", "High", "Low", "Close"])

    @staticmethod
    def _to_candles(df) -> List[Candle]:
        out: List[Candle] = []
        for ts, row in df.iterrows():
            out.append(Candle(
                ts=ts.to_pydatetime(),
                open=float(row["Open"]), high=float(row["High"]),
                low=float(row["Low"]), close=float(row["Close"]),
                volume=float(row["Volume"]),
            ))
        return out
