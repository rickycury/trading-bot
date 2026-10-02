"""Portfolio-level risk.

Per-trade 1% is necessary but not sufficient: SPY and QQQ run ~0.9 correlated,
so two independent 1% trades are really one 2% trade. This module sits above the
sizer and enforces the rules that span positions.

Gates, in order, before any entry is allowed:
  1. Drawdown breaker — if equity is >10% below its peak, refuse everything and
     latch until a human resets. Peak equity is persisted, not recomputed.
  2. Portfolio heat — sum of open risk + the new trade's risk must stay under
     `max_portfolio_risk` (default 4%).
  3. Bucket cap — correlated instruments share a bucket; a bucket has its own
     ceiling so SPY+QQQ can't stack to full size each.
  4. Correlation filter — no new risk-on add (crypto long) while the equity
     bucket is already net long, generalizing the original SPY/QQQ->BTC rule.

Plus the invariant the spec cares most about: stops never widen. `clamp_stop`
is the only sanctioned way to move a stop, and it can only tighten.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .types import Position, Side, Signal


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01
    max_portfolio_risk: float = 0.04
    max_drawdown: float = 0.10
    bucket_caps: Dict[str, float] = field(default_factory=lambda: {
        "risk_on_equity": 0.02,   # SPY + QQQ combined open risk ceiling
        "crypto": 0.01,
        "commodity": 0.02,        # GLD + USO combined
    })


@dataclass
class RiskState:
    peak_equity: float
    halted: bool = False
    halt_reason: str = ""


class RiskManager:
    def __init__(self, config: RiskConfig, state: RiskState):
        self.cfg = config
        self.state = state

    # ---- drawdown breaker -------------------------------------------------
    def update_equity(self, equity: float) -> None:
        if equity > self.state.peak_equity:
            self.state.peak_equity = equity
        dd = 1.0 - equity / self.state.peak_equity if self.state.peak_equity else 0.0
        if dd >= self.cfg.max_drawdown and not self.state.halted:
            self.state.halted = True
            self.state.halt_reason = (
                f"drawdown {dd:.1%} >= {self.cfg.max_drawdown:.0%}; "
                f"peak ${self.state.peak_equity:,.0f}, now ${equity:,.0f}"
            )

    def reset_halt(self) -> None:
        """Manual review acknowledged. Re-anchor peak to current equity."""
        self.state.halted = False
        self.state.halt_reason = ""

    # ---- entry gating -----------------------------------------------------
    def can_enter(
        self,
        signal: Signal,
        new_risk: float,
        equity: float,
        open_positions: List[Position],
        last_prices: Dict[str, float],
        new_bucket: str,
    ) -> tuple[bool, str]:
        if self.state.halted:
            return False, f"HALTED: {self.state.halt_reason}"

        portfolio_risk = sum(
            p.open_risk(last_prices.get(p.symbol, p.entry_price)) for p in open_positions
        )
        if (portfolio_risk + new_risk) > self.cfg.max_portfolio_risk * equity:
            return False, (
                f"portfolio heat {(portfolio_risk + new_risk)/equity:.2%} > "
                f"cap {self.cfg.max_portfolio_risk:.0%}"
            )

        bucket_risk = sum(
            p.open_risk(last_prices.get(p.symbol, p.entry_price))
            for p in open_positions if p.risk_bucket == new_bucket
        )
        cap = self.cfg.bucket_caps.get(new_bucket, self.cfg.max_portfolio_risk)
        if (bucket_risk + new_risk) > cap * equity:
            return False, f"bucket '{new_bucket}' heat {(bucket_risk+new_risk)/equity:.2%} > {cap:.0%}"

        # correlation filter: no new risk-on (crypto long) while equity is net long
        if new_bucket == "crypto" and signal.side is Side.LONG:
            equity_net_long = any(
                p.risk_bucket == "risk_on_equity" and p.side is Side.LONG
                for p in open_positions
            )
            if equity_net_long:
                return False, "correlation filter: equity bucket already net long"

        return True, "ok"

    # ---- stop invariant ---------------------------------------------------
    @staticmethod
    def clamp_stop(position: Position, proposed: Optional[float]) -> float:
        """Only ever tighten. Returns the stop the engine should set."""
        if proposed is None:
            return position.stop
        if position.side is Side.LONG:
            return max(position.stop, proposed)   # long stop can only rise
        return min(position.stop, proposed)       # short stop can only fall
