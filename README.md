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
  - `price_collar` (soft) with a market-data freshness contract that fails
    closed on a missing or stale quote, keying freshness to the stalest side
  - `duplicate_rate_guard` (hard): duplicate-intent / runaway-algorithm
    detection on account, side, symbol, and quantity within a time window
  - `buying_power` and `credit_limit` (audit-only)
- A supervisory override that releases a soft block with a recorded reason, and
  that cannot release a hard control.
- An append-only (write-once) approval and decision audit trail, mirroring an
  SEC Rule 17a-4 retention posture.
- Exact monetary arithmetic with `Decimal`, not binary floating point.

## Run it

Requires Python 3.9+ only. There are no third-party dependencies.

```
python -m pretrade
```

Then open http://localhost:8080 for the optional browser client. The SQLite
database is created and seeded on first run under `./data`.

With Docker:

```
docker build -t pretrade . && docker run -p 8080:8080 pretrade
```

## Test it

```
python -m unittest discover -s tests -v
```

The suite covers every control, the fail-closed paths (stale quote, missing
quote, future-dated quote), the duplicate window, the hard-over-soft composition
rule, and the audit-only behaviour of buying power.

## Benchmark it

The engine runs in line on the order path, so its latency matters.

```
python -m bench.latency 20000
```

Representative result on a developer laptop (full control chain, in-memory
store): roughly 30,000+ orders/sec with p50 around 26 microseconds and p99 under
100 microseconds. Numbers vary by machine; the point is that the control chain
is well inside a sub-millisecond budget.

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
tests/            unittest suite
bench/            latency benchmark
docs/             architecture and design-decision notes
public/           optional browser client (static HTML/CSS/JS)
```

See `docs/ARCHITECTURE.md` and `docs/DESIGN_DECISIONS.md` for the design.
