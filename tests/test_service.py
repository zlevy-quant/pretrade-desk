"""Service-level tests: exercise the HTTP surface end to end.

Starts the real handler on an ephemeral port and drives it over HTTP, so the
routing, session handling, and the order and override flows are covered, not
just the engine. Uses only the standard library.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from pretrade.service import App, Handler
from pretrade.store import Store


class ServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = Store(":memory:")
        cls.store.seed_if_empty()
        Handler.app = App(cls.store)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.close()

    # ---- helpers ----
    def call(self, method: str, path: str, body: dict | None = None, token: str | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def login(self, username: str, role: str, tenant: str = "RIA") -> str:
        status, body = self.call("POST", "/api/login", {"username": username, "role": role, "tenant": tenant})
        self.assertEqual(status, 200)
        return body["token"]

    # ---- tests ----
    def test_health_is_open(self) -> None:
        status, body = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_api_requires_authentication(self) -> None:
        status, _ = self.call("GET", "/api/blotter")
        self.assertEqual(status, 401)

    def test_clean_order_is_accepted(self) -> None:
        token = self.login("ava.advisor", "advisor")
        status, body = self.call("POST", "/api/orders", {
            "account_id": "ACME-0042", "symbol": "AAPL", "side": "buy",
            "order_type": "market", "quantity": 100}, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body["decision"], "ALLOW")
        self.assertEqual(body["status"], "ACCEPTED")

    def test_soft_block_then_supervisor_override(self) -> None:
        advisor = self.login("ava.advisor", "advisor")
        status, blocked = self.call("POST", "/api/orders", {
            "account_id": "ACME-0042", "symbol": "AAPL", "side": "buy",
            "order_type": "limit", "quantity": 100, "limit_price": "260.00"}, token=advisor)
        self.assertEqual(blocked["decision"], "BLOCK")
        self.assertTrue(blocked["overridable"])
        oid = blocked["id"]

        # An advisor cannot override.
        status, _ = self.call("POST", f"/api/orders/{oid}/override", {"reason": "x"}, token=advisor)
        self.assertEqual(status, 403)

        # A supervisor can, with a reason.
        supervisor = self.login("sam.super", "supervisor")
        status, _ = self.call("POST", f"/api/orders/{oid}/override", {}, token=supervisor)
        self.assertEqual(status, 400)  # reason required
        status, released = self.call("POST", f"/api/orders/{oid}/override",
                                     {"reason": "price verified at venue"}, token=supervisor)
        self.assertEqual(status, 200)
        self.assertEqual(released["status"], "OVERRIDDEN")

    def test_hard_block_is_not_overridable(self) -> None:
        advisor = self.login("ava.advisor", "advisor")
        _, blocked = self.call("POST", "/api/orders", {
            "account_id": "ACME-0300", "symbol": "AAPL", "side": "sell",
            "order_type": "market", "quantity": 50}, token=advisor)  # restricted account
        self.assertEqual(blocked["decision"], "BLOCK")
        self.assertFalse(blocked["overridable"])
        supervisor = self.login("sam.super", "supervisor")
        status, _ = self.call("POST", f"/api/orders/{blocked['id']}/override",
                              {"reason": "nope"}, token=supervisor)
        self.assertEqual(status, 409)  # not an overridable soft block


if __name__ == "__main__":
    unittest.main()
