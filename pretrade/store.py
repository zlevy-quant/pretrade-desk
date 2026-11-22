"""SQLite-backed persistence.

A thin repository over the standard-library sqlite3 driver. Every statement is
parameterized. The data layer is isolated here so the engine and controls never
see SQL; the store could be replaced with Postgres without touching risk logic.

The audit trail is append-only by contract: this module exposes only methods to
append and to read it, never to update or delete a row, which mirrors a
write-once (WORM) retention posture for approval records (SEC Rule 17a-4).
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from .config import ControlConfig
from .domain import Order, OrderType, Side
from .market_data import Quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, username TEXT NOT NULL, role TEXT NOT NULL,
  tenant TEXT NOT NULL DEFAULT 'RIA', created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS instruments (
  symbol TEXT PRIMARY KEY, name TEXT NOT NULL, cusip TEXT);

CREATE TABLE IF NOT EXISTS quotes (
  symbol TEXT PRIMARY KEY REFERENCES instruments(symbol),
  bid REAL NOT NULL, ask REAL NOT NULL, as_of TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS accounts (
  account_id TEXT PRIMARY KEY, tenant TEXT NOT NULL DEFAULT 'RIA',
  cash REAL NOT NULL, credit_limit REAL NOT NULL,
  restriction TEXT NOT NULL DEFAULT 'none',   -- none | closing_only | restricted | unknown
  as_of TEXT);                                -- RTP read as-of; NULL reads as unavailable

CREATE TABLE IF NOT EXISTS kill_switch (
  tenant TEXT PRIMARY KEY, engaged INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT, updated_by TEXT);

CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, username TEXT NOT NULL,
  tenant TEXT NOT NULL, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
  side TEXT NOT NULL, order_type TEXT NOT NULL, quantity INTEGER NOT NULL,
  limit_price REAL, signature TEXT NOT NULL, decision TEXT NOT NULL,
  overridable INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, verdict TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_orders_sig ON orders(signature, created_at);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL,
  event TEXT NOT NULL, order_id TEXT, detail TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS controls_config (
  id INTEGER PRIMARY KEY CHECK (id = 1), collar_bps INTEGER NOT NULL,
  quote_max_age_sec INTEGER NOT NULL, dup_window_sec INTEGER NOT NULL,
  dup_threshold INTEGER NOT NULL,
  account_max_age_sec INTEGER NOT NULL DEFAULT 60);
"""

# Defensive, additive migrations for an existing database created by an earlier
# schema. Each is idempotent: a duplicate-column error is swallowed.
_ADDITIVE_MIGRATIONS = (
    "ALTER TABLE accounts ADD COLUMN restriction TEXT NOT NULL DEFAULT 'none'",
    "ALTER TABLE accounts ADD COLUMN as_of TEXT",
    "ALTER TABLE controls_config ADD COLUMN account_max_age_sec INTEGER NOT NULL DEFAULT 60",
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        self.conn.executescript(SCHEMA)
        for stmt in _ADDITIVE_MIGRATIONS:
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already present
        self.conn.commit()

    # ---- seed ----
    def seed_if_empty(self) -> None:
        cur = self.conn
        now = _iso(datetime.now(timezone.utc))
        if cur.execute("SELECT COUNT(*) AS n FROM controls_config").fetchone()["n"] == 0:
            cur.execute(
                "INSERT INTO controls_config (id, collar_bps, quote_max_age_sec, dup_window_sec,"
                " dup_threshold, account_max_age_sec) VALUES (1, 300, 60, 60, 3, 60)"
            )
        if cur.execute("SELECT COUNT(*) AS n FROM instruments").fetchone()["n"] == 0:
            rows = [
                ("AAPL", "Apple Inc.", "037833100", 227.40, 227.52),
                ("MSFT", "Microsoft Corp.", "594918104", 431.10, 431.34),
                ("NVDA", "NVIDIA Corp.", "67066G104", 138.62, 138.70),
                ("ZVZZT", "Test Symbol (NASDAQ)", None, 14.00, 14.06),
            ]
            for sym, name, cusip, bid, ask in rows:
                cur.execute("INSERT INTO instruments (symbol, name, cusip) VALUES (?,?,?)", (sym, name, cusip))
                cur.execute("INSERT INTO quotes (symbol, bid, ask, as_of) VALUES (?,?,?,?)", (sym, bid, ask, now))
        if cur.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"] == 0:
            accts = [
                # account_id, tenant, cash, credit_limit, restriction, as_of
                ("ACME-0042", "RIA", 250000, 500000, "none", now),
                ("ACME-0100", "BD", 1000000, 2000000, "none", now),
                ("ACME-0200", "RIA", 75000, 150000, "closing_only", now),
                ("ACME-0300", "RIA", 500000, 1000000, "restricted", now),
            ]
            cur.executemany(
                "INSERT INTO accounts (account_id, tenant, cash, credit_limit, restriction, as_of)"
                " VALUES (?,?,?,?,?,?)", accts
            )
        for tenant in ("RIA", "BD"):
            if cur.execute("SELECT COUNT(*) AS n FROM kill_switch WHERE tenant=?", (tenant,)).fetchone()["n"] == 0:
                cur.execute("INSERT INTO kill_switch (tenant, engaged, updated_at, updated_by) VALUES (?,0,?, 'seed')", (tenant, now))
        self.conn.commit()

    # ---- config ----
    def get_config(self) -> ControlConfig:
        r = self.conn.execute("SELECT * FROM controls_config WHERE id=1").fetchone()
        return ControlConfig(r["collar_bps"], r["quote_max_age_sec"], r["dup_window_sec"],
                             r["dup_threshold"], r["account_max_age_sec"])

    # ---- market data ----
    def get_quote(self, symbol: str) -> Optional[Quote]:
        r = self.conn.execute("SELECT * FROM quotes WHERE symbol=?", (symbol,)).fetchone()
        if r is None:
            return None
        return Quote(r["symbol"], Decimal(str(r["bid"])), Decimal(str(r["ask"])), _parse(r["as_of"]))

    def list_quotes(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT q.symbol, i.name, i.cusip, q.bid, q.ask, q.as_of FROM quotes q "
            "JOIN instruments i ON i.symbol=q.symbol ORDER BY q.symbol"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_quote_as_of(self, symbol: str, as_of: datetime) -> int:
        cur = self.conn.execute("UPDATE quotes SET as_of=? WHERE symbol=?", (_iso(as_of), symbol))
        self.conn.commit()
        return cur.rowcount

    # ---- accounts ----
    def get_account(self, account_id: str) -> Optional[dict[str, Any]]:
        r = self.conn.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        return dict(r) if r else None

    def list_accounts(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT account_id, tenant, cash, credit_limit, restriction, as_of FROM accounts ORDER BY account_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_account_as_of(self, account_id: str, as_of: datetime) -> int:
        cur = self.conn.execute("UPDATE accounts SET as_of=? WHERE account_id=?", (_iso(as_of), account_id))
        self.conn.commit()
        return cur.rowcount

    def set_restriction(self, account_id: str, restriction: str) -> int:
        cur = self.conn.execute("UPDATE accounts SET restriction=? WHERE account_id=?", (restriction, account_id))
        self.conn.commit()
        return cur.rowcount

    # ---- kill switch ----
    def kill_engaged(self, tenant: str) -> bool:
        r = self.conn.execute("SELECT engaged FROM kill_switch WHERE tenant=?", (tenant,)).fetchone()
        return bool(r and r["engaged"])

    def set_kill(self, tenant: str, engaged: bool, by: str) -> None:
        self.conn.execute(
            "UPDATE kill_switch SET engaged=?, updated_at=?, updated_by=? WHERE tenant=?",
            (1 if engaged else 0, _iso(datetime.now(timezone.utc)), by, tenant),
        )
        self.conn.commit()

    # ---- duplicate detection ----
    def count_recent_signature(self, signature: str, window_sec: int, now: datetime) -> int:
        since = _iso(datetime.fromtimestamp(now.timestamp() - window_sec, tz=timezone.utc))
        r = self.conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE signature=? AND created_at>=?", (signature, since)
        ).fetchone()
        return int(r["n"])

    # ---- orders ----
    def insert_order(self, order: Order, decision: str, overridable: bool, status: str,
                     verdict_json: str, username: str, now: datetime) -> str:
        oid = "ORD-" + datetime.now(timezone.utc).strftime("%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.conn.execute(
            "INSERT INTO orders (id, created_at, username, tenant, account_id, symbol, side, order_type,"
            " quantity, limit_price, signature, decision, overridable, status, verdict)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, _iso(now), username, order.tenant, order.account_id, order.symbol, order.side.value,
             order.order_type.value, order.quantity,
             float(order.limit_price) if order.limit_price is not None else None,
             order.signature(), decision, 1 if overridable else 0, status, verdict_json),
        )
        self.conn.commit()
        return oid

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        r = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return dict(r) if r else None

    def set_order_status(self, order_id: str, status: str) -> None:
        self.conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        self.conn.commit()

    def list_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, created_at, username, account_id, symbol, side, quantity, status "
            "FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- audit (append-only) ----
    def append_audit(self, actor: str, event: str, detail: str, order_id: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (ts, actor, event, order_id, detail) VALUES (?,?,?,?,?)",
            (_iso(datetime.now(timezone.utc)), actor, event, order_id, detail),
        )
        self.conn.commit()

    def list_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT ts, actor, event, order_id, detail FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- sessions ----
    def create_session(self, username: str, role: str, tenant: str) -> str:
        token = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO sessions (token, username, role, tenant, created_at) VALUES (?,?,?,?,?)",
            (token, username, role, tenant, _iso(datetime.now(timezone.utc))),
        )
        self.conn.commit()
        return token

    def get_session(self, token: Optional[str]) -> Optional[dict[str, Any]]:
        if not token:
            return None
        r = self.conn.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
        return dict(r) if r else None
