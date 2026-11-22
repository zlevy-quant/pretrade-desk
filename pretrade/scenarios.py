"""Scenario runner: a narrated walkthrough of the pre-trade controls.

Run:  python -m pretrade.scenarios

Each scenario seeds a small, specific world state, sends one order through the
same ``PreTradeEngine`` the service uses, and prints the verdict together with
the control that drove it. The set is chosen to show each behavior once: a clean
allow, a soft block released by a supervisor, a market-data fail-closed, the
tenant kill switch, duplicate-intent detection, and the account restriction
states. It is meant to be run and narrated live.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Callable, Optional

from .audit import AuditTrail
from .controls import EvaluationContext, default_controls
from .domain import Order, OrderType, Side, Verdict
from .engine import PreTradeEngine
from .market_data import utcnow
from .store import Store


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    expect: str
    order: Order
    actor: str = "ava.trader"
    # A setup hook may return anything; several store mutators return a row count,
    # and the return value is ignored, so the result type is left open.
    setup: Optional[Callable[[Store], object]] = None
    # When set, a supervisor releases the order if it is a soft, overridable block.
    override_reason: Optional[str] = None


@dataclass
class Outcome:
    scenario: Scenario
    verdict: Verdict
    order_id: str
    final_status: str
    overridden: bool


def _o(**kw: object) -> Order:
    base: dict[str, object] = dict(
        account_id="ACME-0042", symbol="AAPL", side=Side.BUY,
        order_type=OrderType.MARKET, quantity=100, limit_price=None, tenant="RIA",
    )
    base.update(kw)
    return Order(**base)  # type: ignore[arg-type]


def _seed_duplicates(store: Store, order: Order) -> None:
    now = utcnow()
    for _ in range(2):  # two priors; the scenario order is the third identical in the window
        store.insert_order(order, "allow", False, "ACCEPTED", "{}", "ava.trader", now)


def scenarios() -> list[Scenario]:
    """The ordered demo set. Each shows exactly one behavior."""
    dup = _o(symbol="NVDA", quantity=400)
    return [
        Scenario(
            "clean_allow", "Clean market buy",
            "ALLOW: every control passes",
            _o(symbol="AAPL", quantity=100),
        ),
        Scenario(
            "collar_override", "Limit through the collar, released by a supervisor",
            "BLOCK (soft), then OVERRIDDEN by a Series 24 supervisor with a recorded reason",
            _o(symbol="AAPL", order_type=OrderType.LIMIT, limit_price=Decimal("260.00")),
            override_reason="price verified against the primary venue; client instruction on file",
        ),
        Scenario(
            "stale_quote", "Stale market-data feed",
            "BLOCK (hard): the price collar fails closed on a stale quote",
            _o(symbol="AAPL"),
            setup=lambda s: s.set_quote_as_of("AAPL", utcnow() - timedelta(hours=16)),
        ),
        Scenario(
            "kill_switch", "Tenant kill switch engaged",
            "BLOCK (hard): the master kill switch halts the tenant",
            _o(symbol="MSFT", quantity=100),
            setup=lambda s: s.set_kill("RIA", True, "ops.supervisor"),
        ),
        Scenario(
            "duplicate_burst", "Runaway / duplicate intent",
            "BLOCK (hard): the same order is seen three times inside the window",
            dup,
            setup=lambda s: _seed_duplicates(s, dup),
        ),
        Scenario(
            "restricted_account", "Restricted account",
            "BLOCK (hard): all trading is halted on the account",
            _o(account_id="ACME-0300", symbol="AAPL", side=Side.SELL, quantity=50),
        ),
        Scenario(
            "closing_only", "Closing-only account, opening order",
            "BLOCK (hard): a buy adds exposure on a closing-only account",
            _o(account_id="ACME-0200", symbol="AAPL", side=Side.BUY, quantity=50),
        ),
    ]


def run_scenario(scenario: Scenario, store: Optional[Store] = None) -> Outcome:
    """Run one scenario end to end against a fresh (or supplied) store.

    When this function creates the store it also closes it before returning, so a
    walkthrough of many scenarios leaves no open connections behind.
    """
    own = store is None
    if store is None:
        store = Store(":memory:")
        store.seed_if_empty()
    try:
        if scenario.setup is not None:
            scenario.setup(store)

        engine = PreTradeEngine(default_controls())
        audit = AuditTrail(store)
        now = utcnow()
        ctx = EvaluationContext(store=store, config=store.get_config(),
                                market_data=store, now=now, actor=scenario.actor)

        verdict = engine.evaluate(scenario.order, ctx)
        status = "ACCEPTED" if verdict.allowed else "BLOCKED"
        verdict_json = json.dumps(
            [{"name": c.name, "kind": c.kind.value, "status": c.status.value, "reason": c.reason}
             for c in verdict.controls]
        )
        order_id = store.insert_order(
            scenario.order, verdict.decision.name, verdict.overridable, status, verdict_json, scenario.actor, now
        )
        audit.record_decision(scenario.order, order_id, verdict, scenario.actor)

        overridden = False
        final_status = status
        if (not verdict.allowed) and verdict.overridable and scenario.override_reason:
            store.set_order_status(order_id, "OVERRIDDEN")
            audit.record_override(order_id, "sam.super (supervisor)", scenario.override_reason)
            overridden = True
            final_status = "OVERRIDDEN"

        return Outcome(scenario, verdict, order_id, final_status, overridden)
    finally:
        if own:
            store.close()


def _format(outcome: Outcome) -> str:
    sc, v = outcome.scenario, outcome.verdict
    o = sc.order
    lp = "" if o.limit_price is None else f" @ {o.limit_price}"
    lines = [
        f"  order     {o.account_id}  {o.side.value} {o.quantity} {o.symbol} ({o.order_type.value}{lp}), tenant {o.tenant}",
        f"  verdict   {v.decision.name}" + ("  (overridable)" if v.overridable else ""),
    ]
    driver = next((c for c in v.blocking_controls()), None)
    if driver is not None:
        lines.append(f"  driver    {driver.name} [{driver.kind.value}]: {driver.reason}")
    else:
        flagged = [c for c in v.controls if c.status.value == "audit"]
        if flagged:
            lines.append(f"  note      {flagged[0].name}: {flagged[0].reason}")
    if outcome.overridden:
        lines.append(f"  override  released by a supervisor; reason recorded to the write-once audit trail")
    lines.append(f"  status    {outcome.final_status}   ({outcome.order_id})")
    lines.append(f"  expected  {sc.expect}")
    return "\n".join(lines)


def main() -> None:
    items = scenarios()
    print("=" * 78)
    print("Pre-Trade Risk Engine - scenario walkthrough")
    print("=" * 78)
    for i, sc in enumerate(items, 1):
        outcome = run_scenario(sc)
        print(f"\n{i}. {sc.title}")
        print(_format(outcome))
    print("\n" + "=" * 78)
    print(f"{len(items)} scenarios run. Every blocked order names the control that stopped it,")
    print("and every data problem fails closed rather than passing silently.")
    print("=" * 78)


if __name__ == "__main__":
    main()
