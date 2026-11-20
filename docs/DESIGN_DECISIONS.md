# Design decisions

Short records of the decisions that shape the engine, with the reasoning behind
each. They reflect choices made building pre-trade controls for a regulated RIA
and broker-dealer.

## 1. Controls fail closed

A control that cannot evaluate its inputs blocks the order; it does not pass it.
A missing quote, a stale quote, or an unreadable field is a block, not a
best-effort allow. The safe state is the default, so a data outage degrades to
"no trading" rather than "unchecked trading." The price collar is the clearest
case: no quote, or a quote older than the freshness bound, returns `data_stale`
and blocks.

## 2. Controls are classified hard, soft, or audit

Not every control should stop an order the same way. A hard control (field
validation, kill switch, duplicate guard) is final. A soft control (price
collar) is gating but can be released by an authorized supervisor, because a
price just outside the band is a judgement a licensed person can make. An
audit-only control (buying power, credit limit) is recorded for the trail but
never blocks on its own, which lets exposure be observed before it is made
gating. The engine encodes this as the composition rule, and the classification
is explicit in each control's `kind`.

## 3. Duplicate-intent detection, not a blanket volume cap

A requirement to stop algorithms that "run amok" is best served by detecting
duplicate intent (the same account, side, symbol, and quantity repeated within a
short window), not by a flat orders-per-interval ceiling. A blanket cap rejects
legitimate bursts at the open and still misses a slow loop. The guard counts
identical signatures in a window and blocks at a small threshold.

## 4. The approval and decision trail is append-only (write-once)

Supervisory overrides and order decisions must be retained immutably under SEC
Rule 17a-4. The store exposes only append and read for the audit log; there is no
update or delete path in code. In a production deployment the same posture is
enforced at the storage layer with object-lock / WORM retention; here it is
enforced by the module's surface.

## 5. Money and prices use Decimal

Notional, cash, limit prices, and collar bands are computed with `Decimal`, not
`float`. Binary floating point cannot represent decimal currency exactly, and
rounding drift is unacceptable in a control that decides whether an order is
within a band or within available cash.

## 6. The kill switch is per tenant

The platform serves separate regulated entities (an RIA and a broker-dealer).
The master stop is keyed by tenant so one entity can be halted without halting
the other, matching the isolation the rest of the platform maintains between
tenants.

## 7. A single evaluation point

Every order takes the same verdict before intake, regardless of whether it came
from a proposal engine or a manual ticket. One evaluation point keeps the rules
from being reimplemented per path and makes the audit trail complete.

## 8. The engine is pure and transport-agnostic

`engine.evaluate(order, context)` is deterministic given its inputs, and the
`now` timestamp is injected rather than read inside the controls. That makes
every control testable without mocking a clock, and it makes the latency
benchmark meaningful.
