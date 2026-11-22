# Pre-Trade Risk Engine

A compact, dependency-free reference implementation of the control layer that
sits between order entry and the market on a multi-tenant brokerage trading
platform. Every order passes through a single enforcement boundary that runs a
chain of risk controls and returns an allow-or-block verdict with a reason,
before the order is persisted or routed.

The engine is designed to **fail closed**: a missing, stale, or unparseable
input produces a block, never a pass, so a data outage can never silently let an
order through.

This is a personal, generalized reimplementation of pre-trade control patterns
from production trading-systems work. It contains no employer code, data, or
secrets; market data, accounts, and verdicts are simulated.

## What it demonstrates

- A clean separation between risk logic and infrastructure: the engine and
  controls operate on domain types and never touch SQL or HTTP.
- Each control as an independently testable unit, declaring whether it is
  **hard** (gating, final), **soft** (gating, supervisor-overridable), or
  **audit** (recorded, non-gating):
  - `field_validation` (hard)
  - `kill_switch` (hard, per tenant)
  - `account_restriction` (hard, per tenant): reads the account's restriction
    status (none, closing-only, restricted) with an as-of freshness bound, and
    fails closed on a missing, stale, or unknown account read
  - `price_collar` (soft when a price is merely outside the band and a
    supervisor may release it; **escalates to hard** on a missing or stale
    quote, because a data-integrity failure is not a pricing judgment and must
    not be waveable)
  - `duplicate_rate_guard` (hard): duplicate-intent / runaway-algorithm
    detection on account, side, symbol, and quantity within a time window
  - `buying_power` and `credit_limit` (audit-only)
- A supervisory override that releases a soft block with a recorded reason, and
  that cannot release a hard control.
- An append-only (write-once) approval and decision audit trail, mirroring an
  SEC Rule 17a-4 retention posture.
- Exact monetary arithmetic with `Decimal`, not binary floating point.

## Run it

Requires Python 3.10+ only. There are no third-party dependencies.

```
python -m pretrade
```

Then open http://localhost:8080 for the optional browser client. The SQLite
database is created and seeded on first run under `./data`.

With Docker:

```
docker build -t pretrade . && docker run -p 8080:8080 pretrade
```

## See it decide (scenario walkthrough)

A narrated run of each behavior, against the same engine the service uses:

```
python -m pretrade.scenarios
```

It walks a clean allow, a soft collar block released by a supervisor, a
stale-feed fail-closed, the tenant kill switch, duplicate-intent detection, and
the restricted and closing-only account cases, printing the verdict and the
control that drove each one.

## Test it

```
python -m unittest discover -s tests -v   # 32 tests
python -m mypy pretrade bench             # strict, no issues
```

The suite covers every control, the fail-closed paths (stale, missing, and
future-dated quotes, and a stale or unknown account read), the duplicate window,
the hard-over-soft composition rule, the soft-to-hard escalation on a data
failure, the audit-only buying-power behavior, the scenario outcomes, and the
HTTP surface end to end (login, order, supervisor override). Continuous
integration runs the type check, the tests, and the smoke runs on Python 3.10,
3.11, and 3.12 (`.github/workflows/ci.yml`).

## Benchmark it

The engine runs in line on the order path, so its latency matters.

```
python -m bench.latency 20000
```

Representative result on a developer laptop (full seven-control chain, in-memory
store): around 26,000 orders/sec with p50 near 33 microseconds and p99 under 100
microseconds. Numbers vary by machine; the point is that the whole control chain
sits comfortably inside a sub-millisecond budget.

## Layout

```
pretrade/
  domain.py       value types: Side, OrderType, Order, ControlResult, Verdict
  config.py       risk thresholds (collar width, freshness bound, dup window)
  market_data.py  quote model and the freshness contract
  store.py        SQLite repositories; the audit table is append-only by contract
  controls.py     one evaluatable unit per control
  engine.py       orchestrates the controls and composes the verdict
  audit.py        append-only approval/decision trail
  service.py      thin JSON/HTTP surface over the engine
  scenarios.py    narrated walkthrough runner (python -m pretrade.scenarios)
tests/            unittest suite: engine, scenarios, and the HTTP service
bench/            latency benchmark
docs/             architecture, design decisions, and the project story
public/           optional browser client (static HTML/CSS/JS)
.github/          CI: mypy strict, tests, and smoke runs on 3.10 / 3.11 / 3.12
```

See `docs/ARCHITECTURE.md`, `docs/DESIGN_DECISIONS.md`, and `docs/STORY.md` for
the design and the background.
