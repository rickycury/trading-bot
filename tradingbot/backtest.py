"""Backtester.

The whole point of the pure-Strategy design is that the backtester drives the
*same* objects the live loop does. So this module does not re-implement any
trading logic — it wires a simulated fill engine (`BacktestBroker`) in as the
`Execution` backend and replays historical candles through the real `Engine`.
Every risk gate, every stop clamp, the drawdown breaker, the sizing math — all
of it is exercised exactly as it will be in production. What you measure here is
what trades.

Accounting is mark-to-market, not cash/margin mechanics, so longs and shorts are
symmetric and there's no borrow/margin modelling to get wrong:

    equity(t) = starting_equity + realized_pnl + Σ open unrealized_pnl(t)

Costs are modelled as adverse slippage (bps off the signal price, always against
you) plus a per-fill fee (bps of notional). Stops fill at the stop price *plus*
slippage, which is the honest assumption — stops slip.

Multiple instruments on different timeframes are merged into one event timeline
ordered by candle close time, so the portfolio-level risk gates (heat cap,
buckets, correlation filter, drawdown) see positions in the real order they'd
have opened. That cross-symbol ordering is the part a naive per-symbol backtest
gets wrong.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from .core.engine import Engine
from .core.execution import Execution
from .core.types import Position, Side, Signal


# --------------------------------------------------------------------------- #
# Simulated broker
# --------------------------------------------------------------------------- #
@dataclass
class Fill:
    ts: datetime
    symbol: str
    side: Side
    qty: float
    price: float
    fee: float
    kind: str       # "open" | "close"
    reason: str


@dataclass
class ClosedTrade:
    symbol: str
    side: Side
    qty: float
    entry_price: float
    exit_price: float
    entry_ts: datetime
    exit_ts: datetime
    bars_held: int
    fees: float
    pnl: float      # net of fees, in account currency
    reason: str

    @property
    def won(self) -> bool:
        return self.pnl > 0


class BacktestBroker(Execution):
    """An `Execution` backend that fills against the candle stream and keeps the
    books. The `Engine` talks to it through the exact same three-method
    interface (`open` / `close` / `move_stop`) a live broker adapter implements.
    """

    def __init__(
        self,
        starting_equity: float,
        slippage_bps: float = 2.0,
        fee_bps: float = 1.0,
        point_values: Optional[Dict[str, float]] = None,
    ):
        self.starting_equity = starting_equity
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps
        self.point_values = point_values or {}

        self.realized_pnl: float = 0.0
        self.fees_paid: float = 0.0
        self._open: Dict[str, Position] = {}
        self.last_prices: Dict[str, float] = {}
        self.fills: List[Fill] = []
        self.trades: List[ClosedTrade] = []

        # set by the Backtester before each engine call so opened_at is the
        # candle's close time rather than wall-clock now.
        self.now: Optional[datetime] = None

    # ---- helpers ----------------------------------------------------------
    def _pv(self, symbol: str) -> float:
        return self.point_values.get(symbol, 1.0)

    def _fill_price(self, price: float, side: Side, opening: bool) -> float:
        """Apply adverse slippage. A fill that is a *buy* prints higher, a
        *sell* prints lower — always against us."""
        is_buy = (opening and side is Side.LONG) or (not opening and side is Side.SHORT)
        sign = 1.0 if is_buy else -1.0
        return price * (1.0 + sign * self.slippage_bps / 1e4)

    @staticmethod
    def _pnl(side: Side, entry: float, exit_: float, qty: float, pv: float) -> float:
        direction = 1.0 if side is Side.LONG else -1.0
        return (exit_ - entry) * direction * qty * pv

    def _fee(self, price: float, qty: float, pv: float) -> float:
        return abs(price * qty * pv) * self.fee_bps / 1e4

    # ---- Execution interface ---------------------------------------------
    def open(self, symbol: str, signal: Signal, qty: float) -> Optional[Position]:
        pv = self._pv(symbol)
        fill = self._fill_price(signal.reference_price, signal.side, opening=True)
        fee = self._fee(fill, qty, pv)
        self.realized_pnl -= fee
        self.fees_paid += fee

        pos = Position(
            symbol=symbol,
            side=signal.side,
            qty=qty,
            entry_price=fill,
            stop=signal.stop,
            atr_at_entry=signal.atr or 0.0,
            opened_at=self.now,
            risk_bucket="",  # filled by engine's strategy bucket on the position? see note
        )
        # The engine doesn't set risk_bucket on the Position it tracks; it keys
        # buckets off the strategy. We stamp it here from the strategy via the
        # signal-less path is unavailable, so leave blank — risk gates read the
        # strategy bucket, not this field, on entry. open_risk() doesn't need it.
        self._open[symbol] = pos
        self.fills.append(Fill(self.now, symbol, signal.side, qty, fill, fee, "open", signal.reason))
        return pos

    def close(self, position: Position, price: float, reason: str) -> None:
        pv = self._pv(position.symbol)
        fill = self._fill_price(price, position.side, opening=False)
        fee = self._fee(fill, position.qty, pv)
        gross = self._pnl(position.side, position.entry_price, fill, position.qty, pv)
        self.realized_pnl += gross - fee
        self.fees_paid += fee

        opened = self._open.pop(position.symbol, None)
        entry_ts = opened.opened_at if opened else self.now
        self.trades.append(ClosedTrade(
            symbol=position.symbol, side=position.side, qty=position.qty,
            entry_price=position.entry_price, exit_price=fill,
            entry_ts=entry_ts, exit_ts=self.now, bars_held=position.bars_held,
            fees=fee, pnl=gross - fee, reason=reason,
        ))
        self.fills.append(Fill(self.now, position.symbol, position.side, position.qty, fill, fee, "close", reason))

    def move_stop(self, position: Position, new_stop: float) -> None:
        # Stops are virtual in the backtest: the engine already updated
        # position.stop and enforces it via _hard_stop_hit on the next bar.
        pass

    # ---- accounting -------------------------------------------------------
    def equity(self) -> float:
        unreal = 0.0
        for p in self._open.values():
            mark = self.last_prices.get(p.symbol, p.entry_price)
            unreal += self._pnl(p.side, p.entry_price, mark, p.qty, self._pv(p.symbol))
        return self.starting_equity + self.realized_pnl + unreal

    @property
    def has_open(self) -> bool:
        return bool(self._open)


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
class Backtester:
    """Replays `data` (symbol -> oldest-first Candle list, each already at the
    strategy's timeframe) through a fully-wired `Engine` whose execution backend
    is `broker`."""

    def __init__(self, engine: Engine, broker: BacktestBroker, data: Dict[str, List["object"]]):
        self.engine = engine
        self.broker = broker
        self.data = data
        self.equity_curve: List[Tuple[datetime, float]] = []
        self._in_market_events = 0

    def run(self) -> "BacktestResult":
        # One merged, time-ordered event stream across all symbols/timeframes.
        events = sorted(
            ((c.ts, sym, i) for sym, candles in self.data.items() for i, c in enumerate(candles)),
            key=lambda e: e[0],
        )
        for ts, sym, i in events:
            history = self.data[sym][: i + 1]
            self.broker.now = ts
            self.broker.last_prices[sym] = history[-1].close
            equity = self.broker.equity()
            self.engine.on_bar_close(sym, history, equity)
            if self.broker.has_open:
                self._in_market_events += 1
            self.equity_curve.append((ts, self.broker.equity()))

        exposure = self._in_market_events / len(events) if events else 0.0
        return BacktestResult(
            equity_curve=self.equity_curve,
            trades=self.broker.trades,
            fees_paid=self.broker.fees_paid,
            starting_equity=self.broker.starting_equity,
            exposure=exposure,
            halted=self.engine.risk.state.halted,
            halt_reason=self.engine.risk.state.halt_reason,
        )


# --------------------------------------------------------------------------- #
# Results & metrics
# --------------------------------------------------------------------------- #
@dataclass
class BacktestResult:
    equity_curve: List[Tuple[datetime, float]]
    trades: List[ClosedTrade]
    fees_paid: float
    starting_equity: float
    exposure: float
    halted: bool = False
    halt_reason: str = ""
    _metrics: Dict[str, float] = field(default_factory=dict)

    # -- portfolio metrics --
    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.starting_equity

    @property
    def total_return(self) -> float:
        return self.final_equity / self.starting_equity - 1.0

    @property
    def max_drawdown(self) -> float:
        peak = float("-inf")
        mdd = 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                mdd = max(mdd, (peak - eq) / peak)
        return mdd

    def _daily_equity(self) -> List[float]:
        by_day: Dict[object, float] = {}
        for ts, eq in self.equity_curve:
            by_day[ts.date()] = eq          # last equity seen on each calendar day
        return [by_day[d] for d in sorted(by_day)]

    @property
    def cagr(self) -> float:
        if not self.equity_curve:
            return 0.0
        first, last = self.equity_curve[0][0], self.equity_curve[-1][0]
        years = (last - first).days / 365.25
        if years <= 0 or self.starting_equity <= 0:
            return 0.0
        return (self.final_equity / self.starting_equity) ** (1.0 / years) - 1.0

    @property
    def sharpe(self) -> float:
        eqs = self._daily_equity()
        rets = [eqs[i] / eqs[i - 1] - 1.0 for i in range(1, len(eqs)) if eqs[i - 1] > 0]
        if len(rets) < 2:
            return 0.0
        sd = statistics.pstdev(rets)
        if sd == 0:
            return 0.0
        return statistics.fmean(rets) / sd * (252 ** 0.5)

    # -- trade metrics --
    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        return sum(t.won for t in self.trades) / self.n_trades if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = -sum(t.pnl for t in self.trades if t.pnl < 0)
        return gains / losses if losses > 0 else float("inf")

    def _avg(self, predicate) -> float:
        xs = [t.pnl for t in self.trades if predicate(t.pnl)]
        return statistics.fmean(xs) if xs else 0.0

    # -- reporting --
    def summary(self) -> str:
        wins = [t.pnl for t in self.trades if t.pnl > 0]
        losses = [t.pnl for t in self.trades if t.pnl < 0]
        by_symbol: Dict[str, List[ClosedTrade]] = {}
        for t in self.trades:
            by_symbol.setdefault(t.symbol, []).append(t)

        lines = [
            "=" * 60,
            "BACKTEST RESULTS",
            "=" * 60,
            f"Starting equity   ${self.starting_equity:>14,.2f}",
            f"Final equity      ${self.final_equity:>14,.2f}",
            f"Total return      {self.total_return:>14.2%}",
            f"CAGR              {self.cagr:>14.2%}",
            f"Max drawdown      {self.max_drawdown:>14.2%}",
            f"Sharpe (daily)    {self.sharpe:>14.2f}",
            f"Exposure          {self.exposure:>14.2%}",
            f"Fees paid         ${self.fees_paid:>14,.2f}",
            "-" * 60,
            f"Trades            {self.n_trades:>14d}",
            f"Win rate          {self.win_rate:>14.2%}",
            f"Profit factor     {self.profit_factor:>14.2f}",
            f"Avg win           ${(statistics.fmean(wins) if wins else 0):>14,.2f}",
            f"Avg loss          ${(statistics.fmean(losses) if losses else 0):>14,.2f}",
            f"Largest win       ${(max(wins) if wins else 0):>14,.2f}",
            f"Largest loss      ${(min(losses) if losses else 0):>14,.2f}",
        ]
        if by_symbol:
            lines.append("-" * 60)
            lines.append(f"{'Symbol':<8}{'Trades':>8}{'Win%':>8}{'PnL':>16}")
            for sym, ts in sorted(by_symbol.items()):
                pnl = sum(t.pnl for t in ts)
                wr = sum(t.won for t in ts) / len(ts)
                lines.append(f"{sym:<8}{len(ts):>8d}{wr:>8.0%}{pnl:>16,.2f}")
        if self.halted:
            lines.append("-" * 60)
            lines.append(f"!! CIRCUIT BREAKER TRIPPED: {self.halt_reason}")
        lines.append("=" * 60)
        return "\n".join(lines)

    def plot(self, path: str = "equity_curve.png") -> Optional[str]:
        """Save an equity-curve PNG. No-op (returns None) if matplotlib isn't
        installed, so the backtest never fails just because plotting is missing."""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            return None
        xs = [ts for ts, _ in self.equity_curve]
        ys = [eq for _, eq in self.equity_curve]
        fig, ax = plt.subplots(figsize=(11, 5))
        ax.plot(xs, ys, linewidth=1.2)
        ax.set_title("Equity curve")
        ax.set_ylabel("Equity ($)")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(path, dpi=120)
        plt.close(fig)
        return path
