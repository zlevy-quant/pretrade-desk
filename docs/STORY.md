# The story behind this project

## What it is

A compact, dependency-free pre-trade risk engine: the control layer that sits
between order entry and the market and decides, for every order, whether it may
proceed. It runs a chain of risk controls at a single enforcement boundary and
returns an allow-or-block verdict with a reason and an append-only audit record,
before the order is persisted or routed.

I built it as a clean, self-contained reimplementation of pre-trade control
patterns I have designed in production trading systems. It contains no employer
code, data, or secrets; the market data, accounts, and verdicts are simulated,
and the repository is deliberately small enough to read end to end.

## The problem it models

On a multi-tenant platform serving a registered investment adviser and a
broker-dealer, every order has to clear a pre-trade gate that a regulator can
inspect. The hard requirement is not "check some things," it is that a check the
system could not complete must never silently let an order through. That single
property, failing closed, drives most of the design.

## The decisions I am most deliberate about

- **Fail closed, everywhere.** A missing quote, a stale quote, a stale or
  unreadable account view, or an unparseable field all block the order rather
  than defaulting to allow. A data outage degrades to "no trading," not
  "unchecked trading."
- **Severity on the result, not the control.** A price merely outside the collar
  is a soft block a licensed supervisor may release, because the price is a
  judgment. The same control returns a hard block when the quote is missing or
  stale, because a data-integrity failure is not a judgment and must not be
  waveable. That distinction is the kind of control semantics that actually
  matter at a trading desk, and it is the heart of this engine.
- **One evaluation point.** Every order takes the same verdict whether it came
  from a proposal engine or a manual ticket, so the rules are never reimplemented
  per path and the audit trail is complete.
- **Write-once audit.** Supervisory overrides and order decisions are appended
  and never updated or deleted, mirroring an SEC Rule 17a-4 retention posture.
- **Exact money.** Notional, cash, limit prices, and collar bands use `Decimal`,
  not binary floating point.
- **Per-tenant isolation.** The kill switch and the controls are keyed by tenant,
  so one regulated entity can be halted without halting the other.

## What it demonstrates

- Risk and control semantics for a regulated RIA and broker-dealer: hard versus
  soft versus audit controls, supervisory override with a Series 24 gate, a
  per-tenant kill switch, duplicate-intent detection instead of a blunt volume
  cap, and account-restriction handling that fails closed on a stale read.
- Engineering judgment: a strict separation between risk logic and
  infrastructure (the engine and controls never touch SQL or HTTP), a pure and
  deterministic evaluation function, and a store that could be swapped for
  Postgres without touching risk logic.
- Discipline a reviewer can verify in minutes: a 32-case test suite covering the
  fail-closed paths and the composition rule, a scenario runner that narrates
  each behavior live, `mypy --strict` passing across the package, a latency
  benchmark (the chain runs well inside a sub-millisecond budget), and CI on
  three Python versions.

## How to walk it in five minutes

1. `python -m pretrade.scenarios` prints the full narrated walkthrough: a clean
   allow, a soft collar block released by a supervisor, a stale-feed
   fail-closed, the kill switch, duplicate intent, and the restricted and
   closing-only accounts.
2. `python -m pretrade` serves the same engine with an optional browser client,
   so the same decisions can be driven interactively.
3. `docs/ARCHITECTURE.md` shows the enforcement boundary and the composition
   rule; `docs/DESIGN_DECISIONS.md` records the reasoning above in short,
   citable entries.
