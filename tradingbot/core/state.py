"""Persistence.

The drawdown breaker is worthless if a crash resets peak equity, and orphaned
positions are worse. Everything that must survive a restart lives here: open
positions, peak equity, and the halt latch. SQLite keeps it single-file and
transactional without a server.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List

from .types import Position, Side


class Store:
    def __init__(self, path: str = "bot_state.db"):
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS positions (
            symbol TEXT PRIMARY KEY, side TEXT, qty REAL, entry REAL, stop REAL,
            atr REAL, opened_at TEXT, bucket TEXT, bars_held INTEGER)""")
        self.db.commit()

    # peak equity / halt latch
    def get_peak(self, default: float) -> float:
        row = self.db.execute("SELECT v FROM kv WHERE k='peak'").fetchone()
        return float(row[0]) if row else default

    def set_peak(self, v: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES ('peak', ?)", (str(v),))
        self.db.commit()

    def get_halt(self) -> tuple[bool, str]:
        row = self.db.execute("SELECT v FROM kv WHERE k='halt'").fetchone()
        if not row:
            return False, ""
        d = json.loads(row[0])
        return d["halted"], d["reason"]

    def set_halt(self, halted: bool, reason: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES ('halt', ?)",
                        (json.dumps({"halted": halted, "reason": reason}),))
        self.db.commit()

    # positions
    def load_positions(self) -> Dict[str, Position]:
        out: Dict[str, Position] = {}
        for r in self.db.execute("SELECT * FROM positions"):
            out[r[0]] = Position(
                symbol=r[0], side=Side(r[1]), qty=r[2], entry_price=r[3], stop=r[4],
                atr_at_entry=r[5], opened_at=datetime.fromisoformat(r[6]),
                risk_bucket=r[7], bars_held=r[8])
        return out

    def upsert_position(self, p: Position) -> None:
        self.db.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?)",
                        (p.symbol, p.side.value, p.qty, p.entry_price, p.stop,
                         p.atr_at_entry, p.opened_at.isoformat(), p.risk_bucket, p.bars_held))
        self.db.commit()

    def delete_position(self, symbol: str) -> None:
        self.db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        self.db.commit()
