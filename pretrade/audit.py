"""Append-only approval and decision audit trail.

A thin façade over the store that exposes only append and read. There is no
update or delete path, by design: approval records for supervisory overrides and
order decisions are written once and retained, mirroring an SEC Rule 17a-4
write-once (WORM) posture.
"""

from __future__ import annotations

from typing import Any, Optional

from .domain import Order, Verdict
from .store import Store


class AuditTrail:
    def __init__(self, store: Store) -> None:
        self._store = store

    def record_decision(self, order: Order, order_id: str, verdict: Verdict, actor: str) -> None:
        status = "ACCEPTED" if verdict.allowed else "BLOCKED"
        blocked = ",".join(c.name for c in verdict.blocking_controls())
        suffix = f" [{blocked}]" if blocked else ""
        detail = f"{order.account_id} {order.side.value} {order.quantity} {order.symbol} -> {status}{suffix}"
        self._store.append_audit(actor, status, detail, order_id)

    def record_override(self, order_id: str, actor: str, reason: str) -> None:
        self._store.append_audit(
            actor, "OVERRIDE", f"released on override by {actor} (supervisor); reason: {reason}", order_id
        )

    def record_event(self, actor: str, event: str, detail: str, order_id: Optional[str] = None) -> None:
        self._store.append_audit(actor, event, detail, order_id)

    def tail(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._store.list_audit(limit)
