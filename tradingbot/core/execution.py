"""Alerts, decision logging, and the execution boundary.

Two things matter here. First, *every* decision is logged, not just fills —
including the no-trades and the blocks — because when you're debugging "why
didn't it trade", the absence of a fill tells you nothing but the logged reason
tells you everything. Second, execution is an interface with three swappable
backends: AlertOnly (notify, never trade), SemiAuto (propose, wait for a tap),
and Live (trade automatically). You graduate a strategy from one to the next as
it earns trust; you do not flip the whole bot to Live at once.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, Optional

from .types import Position, Side, Signal

log = logging.getLogger("bot")


def decision_log(symbol: str, signal: Signal, outcome: str) -> None:
    """One structured line per decision, tradeable or not."""
    log.info("DECISION %-8s %-7s side=%-5s outcome=%s | %s",
             symbol, signal.action.value, signal.side.value, outcome, signal.reason)


class Notifier(ABC):
    @abstractmethod
    def send(self, title: str, body: str) -> None: ...


class ConsoleNotifier(Notifier):
    def send(self, title: str, body: str) -> None:
        log.warning("ALERT [%s] %s", title, body)
    # swap for Telegram/Discord/SMS by implementing send()


class Execution(ABC):
    """The seam between a decision and the world."""
    @abstractmethod
    def open(self, symbol: str, signal: Signal, qty: float) -> Optional[Position]: ...
    @abstractmethod
    def close(self, position: Position, price: float, reason: str) -> None: ...
    @abstractmethod
    def move_stop(self, position: Position, new_stop: float) -> None: ...


class AlertOnly(Execution):
    """Never touches a broker. Notifies and returns None so the engine records
    nothing as a live position. Use this until the kill switches have proven
    themselves against paper fills."""
    def __init__(self, notifier: Notifier):
        self.n = notifier

    def open(self, symbol, signal, qty):
        self.n.send(f"ENTRY {symbol}",
                    f"{signal.side.value} {qty} @ ~{signal.reference_price} "
                    f"stop {signal.stop} | {signal.reason}")
        return None

    def close(self, position, price, reason):
        self.n.send(f"EXIT {position.symbol}",
                    f"close {position.qty} @ ~{price} | {reason}")

    def move_stop(self, position, new_stop):
        self.n.send(f"STOP {position.symbol}", f"trail to {new_stop:.4f}")


class PaperExecution(Execution):
    """The semi-auto brain. Tracks every signal as a paper position and pushes the
    full lifecycle to the notifier — entry, trailing-stop moves, exits/targets —
    without touching a broker.

    The key difference from AlertOnly: open() returns a real Position, so the
    engine *tracks* it. That switches ON everything AlertOnly leaves dormant — the
    portfolio rules (heat cap, bucket caps, correlation filter, buying-power cap)
    and the management loop (hard stops, trailing stops, strategy target exits).
    You place the trades by hand; the bot tells you exactly when and what, in and
    out.

    Dollar figures assume point_value 1 (true for the ETFs and spot BTC). Trail
    alerts are throttled to material moves so a trending position doesn't spam you
    with a tightening-stop ping every bar.
    """

    TRAIL_ALERT_PCT = 0.0025   # re-alert a trailing stop only on a >=0.25% move

    def __init__(self, notifier: Notifier, account_equity: float):
        self.n = notifier
        self.equity = account_equity
        self._last_trail: Dict[str, float] = {}

    def open(self, symbol: str, signal: Signal, qty: float) -> Optional[Position]:
        capital = qty * (signal.reference_price or 0.0)
        risk = qty * signal.stop_distance()
        side = signal.side.value.upper()
        body = [
            f"{side} {qty:g}   (~${capital:,.0f} capital)",
            f"Entry  ~{signal.reference_price:,.4f}",
            f"Stop   {signal.stop:,.4f}   (risk ${risk:,.2f}, {risk / self.equity:.2%})",
        ]
        if signal.target is not None:
            body.append(f"🎯 Target {signal.target:,.4f}  — exit here")
        else:
            body.append("Exit: trailing stop — ride it, no fixed target")
        body.append(signal.reason)
        self.n.send(f"🟢 ENTRY  {symbol}  {side}", "\n".join(body))
        self._last_trail.pop(symbol, None)
        return Position(
            symbol=symbol, side=signal.side, qty=qty,
            entry_price=signal.reference_price, stop=signal.stop,
            atr_at_entry=signal.atr or 0.0,
            opened_at=datetime.now(timezone.utc), risk_bucket="",
        )

    def move_stop(self, position: Position, new_stop: float) -> None:
        last = self._last_trail.get(position.symbol)
        if last is not None and last > 0 and abs(new_stop - last) / last < self.TRAIL_ALERT_PCT:
            return   # immaterial move — don't spam
        self._last_trail[position.symbol] = new_stop
        self.n.send(
            f"🟡 TRAIL  {position.symbol}",
            f"Tighten your stop to {new_stop:,.4f}\n"
            f"{position.side.value.upper()} {position.qty:g}  (stops only ever tighten)",
        )

    def close(self, position: Position, price: float, reason: str) -> None:
        direction = 1.0 if position.side is Side.LONG else -1.0
        pnl = (price - position.entry_price) * direction * position.qty
        self.n.send(
            f"🔴 EXIT  {position.symbol}  {position.side.value.upper()}",
            f"Close {position.qty:g} @ ~{price:,.4f}\n"
            f"Est P/L  ${pnl:,.2f}\n"
            f"Reason: {reason}",
        )
        self._last_trail.pop(position.symbol, None)


# Live (Alpaca / exchange) implements the same three methods; Live.open() places
# the order immediately. PaperExecution is the stepping stone: same tracking and
# alerts, no real orders.
