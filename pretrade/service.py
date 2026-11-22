"""A thin JSON/HTTP surface over the engine, using only the standard library.

Transport is intentionally minimal: parse the request, translate to domain types,
call the engine, persist the outcome, and serialize the verdict. The risk logic
lives in the engine and controls, not here. A static file handler serves the
optional browser client under ``public/``.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse

from .audit import AuditTrail
from .controls import EvaluationContext, default_controls
from .domain import ControlResult, Decision, Order, OrderType, Side, Verdict
from .engine import PreTradeEngine
from .market_data import utcnow
from .store import Store

PUBLIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public")
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".ico": "image/x-icon"}


def verdict_to_json(order_id: str, status: str, verdict: Verdict) -> dict[str, Any]:
    return {
        "id": order_id,
        "decision": verdict.decision.name,          # ALLOW / BLOCK
        "overridable": verdict.overridable,
        "status": status,
        "controls": [{"name": c.name, "kind": c.kind.value, "status": c.status.value, "reason": c.reason}
                     for c in verdict.controls],
    }


def parse_order(body: dict[str, Any], tenant: str) -> tuple[Optional[Order], Optional[str]]:
    try:
        side = Side(str(body.get("side", "")).strip())
        otype = OrderType(str(body.get("order_type", "")).strip())
    except ValueError:
        return None, "unknown side or order type"
    raw_qty: Any = body.get("quantity")
    try:
        qty = int(raw_qty)
    except (TypeError, ValueError):
        return None, "quantity must be a whole number"
    lp = body.get("limit_price")
    limit_price: Optional[Decimal] = None
    if lp not in (None, ""):
        try:
            limit_price = Decimal(str(lp))
        except InvalidOperation:
            return None, "limit price is not a number"
    return Order(account_id=str(body.get("account_id", "")).strip(), symbol=str(body.get("symbol", "")).strip(),
                 side=side, order_type=otype, quantity=qty, limit_price=limit_price, tenant=tenant), None


class App:
    """Holds the long-lived collaborators shared across requests."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.engine = PreTradeEngine(default_controls())
        self.audit = AuditTrail(store)


class Handler(BaseHTTPRequestHandler):
    app: App  # injected via server

    def log_message(self, *args: Any) -> None:  # quiet by default
        pass

    # ---- helpers ----
    def _json(self, status: int, obj: Any) -> None:
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data: Any = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {"__parse_error": True}
        return data if isinstance(data, dict) else {}

    def _session(self) -> Optional[dict[str, Any]]:
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else None
        return self.app.store.get_session(token)

    # ---- routing ----
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._json(200, {"ok": True})
        if path.startswith("/api/"):
            return self._api("GET", path)
        return self._static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self._api("POST", path)
        self._json(405, {"error": "method not allowed"})

    def _api(self, method: str, path: str) -> None:
        store, engine, audit = self.app.store, self.app.engine, self.app.audit

        if method == "POST" and path == "/api/login":
            body = self._body()
            username, role = str(body.get("username", "")).strip(), body.get("role")
            if not username or role not in ("advisor", "supervisor"):
                return self._json(400, {"error": "username and a valid role (advisor|supervisor) are required"})
            tenant = "BD" if body.get("tenant") == "BD" else "RIA"
            token = store.create_session(username, role, tenant)
            audit.record_event(username, "SESSION_OPEN", f"session opened: {username} ({role}), tenant {tenant}")
            return self._json(200, {"token": token, "username": username, "role": role, "tenant": tenant})

        session = self._session()
        if session is None:
            return self._json(401, {"error": "unauthenticated"})
        actor, tenant, role = session["username"], session["tenant"], session["role"]

        if method == "GET" and path == "/api/market":
            cfg = store.get_config()
            now = utcnow()
            quotes = store.list_quotes()
            for q in quotes:
                from .store import _parse  # local import to avoid widening the public API
                age = int((now - _parse(q["as_of"])).total_seconds())
                q["age_sec"], q["stale"] = age, age > cfg.quote_max_age_sec
            return self._json(200, {"quotes": quotes, "config": {
                "collar_bps": cfg.collar_bps, "quote_max_age_sec": cfg.quote_max_age_sec,
                "dup_window_sec": cfg.dup_window_sec, "dup_threshold": cfg.dup_threshold}})

        if method == "POST" and path.startswith("/api/market/") and path.endswith(("/stale", "/fresh")):
            parts = path.split("/")
            symbol, action = parts[3], parts[4]
            from datetime import timedelta
            new_as_of = utcnow() - (timedelta(hours=16) if action == "stale" else timedelta(0))
            if store.set_quote_as_of(symbol, new_as_of) == 0:
                return self._json(404, {"error": "unknown symbol"})
            audit.record_event(actor, "MARKETDATA_" + action.upper(), f"{action} feed on {symbol}")
            return self._json(200, {"ok": True})

        if method == "GET" and path == "/api/accounts":
            cfg = store.get_config()
            now = utcnow()
            from .store import _parse  # local import to avoid widening the public API
            accounts = store.list_accounts()
            for a in accounts:
                if a.get("as_of"):
                    age = int((now - _parse(a["as_of"])).total_seconds())
                    a["age_sec"], a["stale"] = age, age > cfg.account_max_age_sec
                else:
                    a["age_sec"], a["stale"] = None, True
            return self._json(200, {"accounts": accounts,
                                    "config": {"account_max_age_sec": cfg.account_max_age_sec}})

        if method == "POST" and path.startswith("/api/accounts/") and path.endswith(("/stale", "/fresh")):
            parts = path.split("/")
            account_id, action = parts[3], parts[4]
            from datetime import timedelta
            new_as_of = utcnow() - (timedelta(hours=16) if action == "stale" else timedelta(0))
            if store.set_account_as_of(account_id, new_as_of) == 0:
                return self._json(404, {"error": "unknown account"})
            audit.record_event(actor, "ACCOUNTVIEW_" + action.upper(), f"{action} account view on {account_id}")
            return self._json(200, {"ok": True})

        if path == "/api/killswitch":
            if method == "GET":
                return self._json(200, {"tenant": tenant, "engaged": store.kill_engaged(tenant)})
            engaged = bool(self._body().get("engaged"))
            store.set_kill(tenant, engaged, actor)
            audit.record_event(actor, "KILLSWITCH_" + ("ENGAGE" if engaged else "RELEASE"),
                               f"master kill switch {'ENGAGED' if engaged else 'released'} for tenant {tenant}")
            return self._json(200, {"tenant": tenant, "engaged": engaged})

        if method == "POST" and path == "/api/orders":
            body = self._body()
            if body.get("__parse_error"):
                return self._json(400, {"error": "invalid JSON body"})
            order, err = parse_order(body, tenant)
            now = utcnow()
            if err or order is None:
                # Mirror a field_validation hard block for an unparseable request.
                from .domain import ControlKind, ControlStatus
                v = Verdict(Decision.BLOCK, False,
                            (ControlResult("field_validation", ControlKind.HARD, ControlStatus.BLOCK, err or "invalid order"),))
                oid = "ORD-REJECTED"
                return self._json(200, verdict_to_json(oid, "BLOCKED", v))
            ctx = EvaluationContext(store=store, config=store.get_config(), market_data=store, now=now, actor=actor)
            verdict = engine.evaluate(order, ctx)
            status = "ACCEPTED" if verdict.allowed else "BLOCKED"
            verdict_json = json.dumps([{"name": c.name, "kind": c.kind.value, "status": c.status.value, "reason": c.reason}
                                       for c in verdict.controls])
            oid = store.insert_order(order, verdict.decision.name, verdict.overridable, status, verdict_json, actor, now)
            audit.record_decision(order, oid, verdict, actor)
            return self._json(200, verdict_to_json(oid, status, verdict))

        if method == "POST" and path.startswith("/api/orders/") and path.endswith("/override"):
            if role != "supervisor":
                return self._json(403, {"error": "override requires a trade-desk supervisor (Series 24)"})
            reason = str(self._body().get("reason", "")).strip()
            if not reason:
                return self._json(400, {"error": "a reason is required before release"})
            oid = path.split("/")[3]
            o = store.get_order(oid)
            if o is None:
                return self._json(404, {"error": "order not found"})
            if not (o["decision"] == "BLOCK" and o["overridable"] == 1):
                return self._json(409, {"error": "order is not an overridable soft block"})
            store.set_order_status(oid, "OVERRIDDEN")
            audit.record_override(oid, actor, reason)
            return self._json(200, {"id": oid, "status": "OVERRIDDEN", "reason": reason})

        if method == "GET" and path == "/api/blotter":
            return self._json(200, {"orders": store.list_orders()})
        if method == "GET" and path == "/api/audit":
            return self._json(200, {"audit": store.list_audit()})
        return self._json(404, {"error": "not found"})

    def _static(self, path: str) -> None:
        rel = "/index.html" if path == "/" else path
        full = os.path.normpath(os.path.join(PUBLIC_DIR, rel.lstrip("/")))
        if not full.startswith(PUBLIC_DIR) or not os.path.isfile(full):
            self.send_response(404); self.end_headers(); self.wfile.write(b"Not found"); return
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(full)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(store: Store, port: int = 8080) -> None:
    Handler.app = App(store)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Pre-Trade Desk listening on http://localhost:{port}")
    httpd.serve_forever()
