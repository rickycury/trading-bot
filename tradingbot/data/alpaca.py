"""Alpaca integration: historical + live data feed, and a paper/live execution
backend.

Two independent pieces:

  * `AlpacaDataFeed` — pulls OHLCV bars. Used two ways: bulk history to feed the
    backtester, and `latest_closed()` polling to drive the live engine. Works
    for the four ETFs (stock endpoints) and BTC/USD (crypto endpoints); pass
    crypto=True or use a symbol containing "/".

  * `AlpacaExecution` — an `Execution` backend for when a strategy graduates off
    AlertOnly. Entries are market orders with a broker-resting stop attached
    (Alpaca OTO order class), so the stop sits at the exchange and fills intrabar
    on a fast move instead of waiting for your next bar close. This is the safe
    default; the engine's own bar-by-bar stop check becomes a backstop.

`alpaca-py` is an optional dependency — this module imports cleanly without it
and raises a clear install hint only when you actually try to use it. Get paper
keys at https://app.alpaca.markets/ (Paper Trading -> API Keys); pass them in or
set ALPACA_API_KEY / ALPACA_SECRET_KEY in the environment.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ..core.execution import Execution
from ..core.types import Candle, Position, Side, Signal

try:  # the whole SDK is optional
    from alpaca.data.historical import (
        CryptoHistoricalDataClient,
        StockHistoricalDataClient,
    )
    from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import (
        OrderClass,
        OrderSide,
        OrderType,
        QueryOrderStatus,
        TimeInForce,
    )
    from alpaca.trading.requests import (
        GetOrdersRequest,
        MarketOrderRequest,
        ReplaceOrderRequest,
        StopLossRequest,
    )

    _ALPACA_OK = True
except ImportError:  # pragma: no cover
    _ALPACA_OK = False


def _require() -> None:
    if not _ALPACA_OK:
        raise ImportError(
            "alpaca-py is not installed. Run:  pip install alpaca-py\n"
            "(this is only needed for the Alpaca data feed / execution backend)"
        )


_TF_RE = re.compile(r"^\s*(\d+)\s*([mhdMHD])\s*$")
_UNIT = {"m": "Minute", "h": "Hour", "d": "Day"}


def _to_alpaca_timeframe(tf: str):
    """'15m' -> TimeFrame(15, Minute), '1h' -> TimeFrame(1, Hour), etc."""
    _require()
    m = _TF_RE.match(tf)
    if not m:
        raise ValueError(f"unrecognized timeframe {tf!r} (use e.g. '15m', '1h', '4h')")
    n, unit = int(m.group(1)), _UNIT[m.group(2).lower()]
    return TimeFrame(n, getattr(TimeFrameUnit, unit))


def _is_crypto(symbol: str) -> bool:
    return "/" in symbol


def _to_candle(bar) -> Candle:
    return Candle(
        ts=bar.timestamp if bar.timestamp.tzinfo else bar.timestamp.replace(tzinfo=timezone.utc),
        open=float(bar.open),
        high=float(bar.high),
        low=float(bar.low),
        close=float(bar.close),
        volume=float(bar.volume),
    )


# --------------------------------------------------------------------------- #
# Data feed
# --------------------------------------------------------------------------- #
class AlpacaDataFeed:
    def __init__(self, api_key: Optional[str] = None, secret_key: Optional[str] = None):
        _require()
        api_key = api_key or os.getenv("ALPACA_API_KEY")
        secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY")
        # Stock data requires keys; crypto historical works keyless but we pass
        # keys when we have them.
        self._stock = StockHistoricalDataClient(api_key, secret_key)
        self._crypto = CryptoHistoricalDataClient(api_key, secret_key)

    def history(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: Optional[datetime] = None,
        crypto: Optional[bool] = None,
    ) -> List[Candle]:
        """Bulk historical bars, oldest-first. Feed straight into the backtester."""
        crypto = _is_crypto(symbol) if crypto is None else crypto
        tf = _to_alpaca_timeframe(timeframe)
        end = end or datetime.now(timezone.utc)
        if crypto:
            req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, end=end)
            barset = self._crypto.get_crypto_bars(req)
        else:
            req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, end=end)
            barset = self._stock.get_stock_bars(req)
        bars = barset.data.get(symbol, [])
        return [_to_candle(b) for b in bars]

    def latest_closed(
        self,
        symbol: str,
        timeframe: str,
        crypto: Optional[bool] = None,
        lookback_bars: int = 5,
    ) -> Optional[Candle]:
        """The most recent *closed* bar, for live polling. Pulls a short window
        and returns its last element (Alpaca's historical endpoint returns closed
        bars only, so the forming bar is not included)."""
        m = _TF_RE.match(timeframe)
        minutes = int(m.group(1)) * {"m": 1, "h": 60, "d": 1440}[m.group(2).lower()]
        start = datetime.now(timezone.utc) - timedelta(minutes=minutes * (lookback_bars + 2))
        bars = self.history(symbol, timeframe, start=start, crypto=crypto)
        return bars[-1] if bars else None


# --------------------------------------------------------------------------- #
# Execution backend (paper or live)
# --------------------------------------------------------------------------- #
class AlpacaExecution(Execution):
    """Live/paper order placement. Defaults to paper — flip paper=False only once
    a strategy has earned it. Equity entries carry a broker-resting stop (OTO);
    crypto on Alpaca has no attached-stop support, so for crypto the stop is
    submitted as a separate order after the entry fills.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        paper: bool = True,
    ):
        _require()
        api_key = api_key or os.getenv("ALPACA_API_KEY")
        secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY")
        self.client = TradingClient(api_key, secret_key, paper=paper)
        self.paper = paper

    @staticmethod
    def _entry_side(side: Side):
        return OrderSide.BUY if side is Side.LONG else OrderSide.SELL

    @staticmethod
    def _exit_side(side: Side):
        return OrderSide.SELL if side is Side.LONG else OrderSide.BUY

    def open(self, symbol: str, signal: Signal, qty: float) -> Optional[Position]:
        tif = TimeInForce.GTC if _is_crypto(symbol) else TimeInForce.DAY
        if _is_crypto(symbol):
            # Crypto: market entry now, resting stop as a follow-up order.
            self.client.submit_order(MarketOrderRequest(
                symbol=symbol, qty=qty, side=self._entry_side(signal.side), time_in_force=tif,
            ))
            self.client.submit_order({  # stop order on the opposite side
                "symbol": symbol, "qty": qty, "side": self._exit_side(signal.side).value,
                "type": "stop", "stop_price": round(signal.stop, 2), "time_in_force": tif.value,
            })
        else:
            # Equities: OTO market entry with an attached stop leg that rests at
            # the exchange the moment the entry fills.
            self.client.submit_order(MarketOrderRequest(
                symbol=symbol, qty=qty, side=self._entry_side(signal.side), time_in_force=tif,
                order_class=OrderClass.OTO,
                stop_loss=StopLossRequest(stop_price=round(signal.stop, 2)),
            ))
        return Position(
            symbol=symbol, side=signal.side, qty=qty,
            entry_price=signal.reference_price, stop=signal.stop,
            atr_at_entry=signal.atr or 0.0, opened_at=datetime.now(timezone.utc),
            risk_bucket="",
        )

    def move_stop(self, position: Position, new_stop: float) -> None:
        stop_order = self._find_stop_order(position.symbol)
        if stop_order is None:
            return
        self.client.replace_order_by_id(
            stop_order.id, ReplaceOrderRequest(stop_price=round(new_stop, 2)))

    def close(self, position: Position, price: float, reason: str) -> None:
        # Cancel the resting stop, then flatten at market.
        stop_order = self._find_stop_order(position.symbol)
        if stop_order is not None:
            try:
                self.client.cancel_order_by_id(stop_order.id)
            except Exception:
                pass
        self.client.close_position(position.symbol)

    # ---- reconciliation ---------------------------------------------------
    def _find_stop_order(self, symbol: str):
        orders = self.client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[symbol]))
        for o in orders:
            if o.type == OrderType.STOP:
                return o
        return None

    def sync_positions(self) -> Dict[str, Position]:
        """Best-effort read of live broker positions, for startup reconciliation
        against the local SQLite store. Note: the broker does not know your ATR,
        risk bucket, or original stop intent — those come from your store; this
        is for detecting drift (manual closes, partial fills), not full restore.
        """
        out: Dict[str, Position] = {}
        for p in self.client.get_all_positions():
            qty = abs(float(p.qty))
            side = Side.LONG if float(p.qty) > 0 else Side.SHORT
            stop_order = self._find_stop_order(p.symbol)
            stop = float(stop_order.stop_price) if stop_order is not None else float(p.avg_entry_price)
            out[p.symbol] = Position(
                symbol=p.symbol, side=side, qty=qty,
                entry_price=float(p.avg_entry_price), stop=stop,
                atr_at_entry=0.0, opened_at=datetime.now(timezone.utc), risk_bucket="",
            )
        return out
