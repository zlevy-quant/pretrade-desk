# Pre-Trade Desk

A self-contained, runnable demonstration of a pre-trade risk and order-management control layer. You log in, place an order, and the order is evaluated by a control engine that runs on the server, reads live state from a real database, and returns an allow or block verdict with a reason. Orders and the approval audit trail persist across restarts.

This is a personal, generalized reimplementation of pre-trade control patterns. It contains no employer code, data, or secrets. Market data, accounts, and verdicts are simulated. It has zero npm dependencies: the server, routing, and database all run on Node built-ins (`node:http`, `node:sqlite`), so it starts with nothing to install.

## What it demonstrates

- A single enforcement boundary: every order passes through one server-side gateway before it is persisted or routed.
- A fail-closed control engine. Each control reads its inputs once, and a missing or stale input blocks the order rather than letting it through.
- The full control set:
  - `field_validation` (hard) - required, well-typed fields.
  - `kill_switch` (hard) - a master stop per tenant, held in the database.
  - `price_collar` (soft, overridable) - the order price against a collar around the mid, failing closed on a missing or stale quote.
  - `duplicate_rate_guard` (hard) - duplicate-intent detection on account, side, symbol, and quantity within a time window, counted from the database.
  - `buying_power` and `credit_limit` (audit-only) - informational, never blocking on their own.
- A supervisory override: a soft block can be released only by a supervisor (Series 24), with a reason recorded before release.
- A write-once approval audit trail: every decision and override is appended and never updated or deleted by the application.
- Multi-tenant context (RIA and broker-dealer), each with its own kill switch.

## Run it

### Option A: Docker (one command, nothing else to install)

```
docker compose up --build
```

Then open http://localhost:8080. The database is created and seeded automatically and is persisted in `./data` on your host.

### Option B: Node directly (no install step)

Requires Node.js 22.5 or newer (built-in SQLite). There are no npm dependencies, so no `npm install` is needed.

```
npm start
```

or equivalently:

```
node --experimental-sqlite src/server.js
```

Then open http://localhost:8080. The database file is created at `./data/pretrade.db` on first run.

To wipe the database and re-seed from scratch:

```
npm run seed:reset
```

## How to demo it (about 90 seconds)

1. Sign in as **advisor** (role dropdown), tenant RIA.
2. Click **Through-collar limit**, then **Run pre-trade check**. The price collar soft-blocks it, and the override panel says it needs a supervisor.
3. Sign out, sign back in as **supervisor**, run the same scenario, and release it with a reason. Watch it move to OVERRIDDEN in the blotter and a new line appear in the audit trail.
4. Click **Duplicate x3** and submit three times to trip the hard duplicate guard.
5. Click **Stale market data** to show the collar failing closed on a stale feed (the staleness is set server-side in the database).
6. Toggle the **Kill switch** in the header and submit any order to show the hard master stop.
7. Restart the server (or `docker compose restart`) and reload: the blotter and audit trail are still there, because they are in the database.

## Architecture

```
browser (public/)  ->  Express API (src/routes.js)
                         |-- src/auth.js      session tokens
                         |-- src/engine.js    the fail-closed verdict engine
                         |-- src/db.js        SQLite schema + seed (better-sqlite3)
                         `-- data/pretrade.db the database (persisted)
```

The data layer is isolated behind `src/db.js`. The control engine and routes do not depend on the database being SQLite, so the store could be moved to Postgres without changing the control logic.

## API (all JSON; protected routes need `Authorization: Bearer <token>`)

- `POST /api/login` `{username, role, tenant}` -> `{token, ...}`
- `GET  /api/market` -> quotes and control config
- `POST /api/market/:symbol/stale` | `/fresh` -> simulate feed staleness
- `GET  /api/killswitch` | `POST /api/killswitch` `{engaged}`
- `POST /api/orders` `{account_id, symbol, side, order_type, quantity, limit_price}` -> verdict
- `POST /api/orders/:id/override` `{reason}` -> supervisor only
- `GET  /api/blotter` -> recent orders
- `GET  /api/audit` -> the append-only audit trail
