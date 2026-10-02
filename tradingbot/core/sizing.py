"""Position sizing.

The bug in the original spec was sizing off "1 ATR" while stopping at 2-3 ATR,
which silently turns 1% risk into 2-3%. The fix: size off the *actual stop
distance the strategy declared*, never a fixed ATR multiple. Then dollar risk is
exactly `equity * risk_pct` by construction, whatever the stop multiple is.

    qty = (equity * risk_pct) / (stop_distance_in_price * point_value)

`point_value` is 1 for ETFs and spot crypto; it exists so futures (e.g. /CL at
$1000/pt, /GC at $100/pt) drop in without rewriting the math. Crypto allows
fractional qty; equities are floored to whole shares, which means realized risk
is <= the budget, never above.
"""
from __future__ import annotations

from dataclasses import dataclass

from .types import Signal


@dataclass
class InstrumentSpec:
    symbol: str
    point_value: float = 1.0      # $ per 1.0 of price movement per unit
    fractional: bool = False      # True for crypto
    min_qty: float = 0.0


def size_position(
    equity: float,
    risk_pct: float,
    signal: Signal,
    spec: InstrumentSpec,
    max_notional: float = None,
) -> float:
    """Return quantity such that hitting the stop loses exactly risk_pct of equity.

    If `max_notional` is given (free buying power), the position is capped so its
    cost never exceeds it. A capped position risks *less* than risk_pct — the right
    tradeoff for a small cash account that can't fund the full risk-target size (a
    tight stop on a pricey instrument implies leverage you don't have).

    Raises if the resulting size is below the instrument minimum (caller treats
    that as "skip the trade", not "round up").
    """
    if equity <= 0:
        raise ValueError("equity must be positive")
    if not 0 < risk_pct < 1:
        raise ValueError("risk_pct must be a fraction in (0, 1)")

    risk_budget = equity * risk_pct
    per_unit_risk = signal.stop_distance() * spec.point_value
    raw_qty = risk_budget / per_unit_risk

    capped = False
    if max_notional is not None and signal.reference_price:
        notional_per_unit = signal.reference_price * spec.point_value
        cap_qty = max_notional / notional_per_unit if notional_per_unit > 0 else raw_qty
        if cap_qty < raw_qty:
            raw_qty, capped = cap_qty, True

    qty = raw_qty if spec.fractional else float(int(raw_qty))  # floor whole units
    if qty < max(spec.min_qty, 0.0) or qty <= 0:
        why = (f"capped by buying power ${max_notional:,.2f}" if capped
               else f"stop too wide for {risk_pct:.1%} on ${equity:,.0f}")
        raise InsufficientSizing(
            f"{spec.symbol}: computed qty {raw_qty:.6f} below minimum "
            f"{spec.min_qty}; {why}"
        )
    return qty


def realized_risk(qty: float, signal: Signal, spec: InstrumentSpec) -> float:
    """Dollar risk of the *actual* (possibly floored) position. Sanity check."""
    return qty * signal.stop_distance() * spec.point_value


class InsufficientSizing(Exception):
    pass
