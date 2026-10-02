"""Market session guard.

The ETFs trade only during US regular hours (09:30–16:00 ET, Mon–Fri, minus
holidays); BTC trades 24/7. Without this guard the live loop would evaluate ETF
signals on whatever the latest bar is, including stale after-hours/weekend bars.

The check is keyed off the *bar's* timestamp, not wall-clock now, so the final
bar of the day (15:45 ET, covering 15:45–16:00) is still processed when you poll
just after the close — gating on "is it open right now" would wrongly drop it.

Timezone/DST is handled by stdlib zoneinfo (no dependency). Holidays are a
hardcoded set of full-day closures; a missing holiday is low-risk because on a
closed day the data source returns no new bar, so the prior session's bar never
re-fires anyway. Early-close half-days (13:00 ET) are not modeled — alerts in
the 13:00–16:00 window on those ~3 days/year are the only gap. Extend
`_HOLIDAYS` each year.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone

from .data.base import is_crypto

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo data missing
    _ET = None

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)

# US equity market full-day closures (NYSE/Nasdaq).
_HOLIDAYS = {
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}


def _to_et(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(_ET) if _ET else ts


def in_session(ts: datetime, symbol: str) -> bool:
    """True if `symbol` is tradeable at bar-time `ts`. Crypto is always open."""
    if is_crypto(symbol):
        return True
    et = _to_et(ts)
    if et.weekday() >= 5:            # Saturday / Sunday
        return False
    if et.date() in _HOLIDAYS:
        return False
    return RTH_OPEN <= et.time() < RTH_CLOSE
