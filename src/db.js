"use strict";

// Database layer. Uses SQLite through better-sqlite3 so the whole system
// spins up with no external database service: the file is created on first
// boot and seeded with demo data. The data layer is deliberately isolated
// behind this module, so the store could be swapped for Postgres without
// touching the control engine or the routes.

const path = require("path");
const fs = require("fs");
const { DatabaseSync } = require("node:sqlite");

const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, "..", "data");
const DB_PATH = process.env.DB_PATH || path.join(DATA_DIR, "pretrade.db");

fs.mkdirSync(DATA_DIR, { recursive: true });

const db = new DatabaseSync(DB_PATH);
db.exec("PRAGMA journal_mode = WAL");
db.exec("PRAGMA foreign_keys = ON");

function migrate() {
  db.exec(`
    CREATE TABLE IF NOT EXISTS users (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      username   TEXT UNIQUE NOT NULL,
      role       TEXT NOT NULL CHECK (role IN ('advisor','supervisor'))
    );

    CREATE TABLE IF NOT EXISTS sessions (
      token      TEXT PRIMARY KEY,
      username   TEXT NOT NULL,
      role       TEXT NOT NULL,
      tenant     TEXT NOT NULL DEFAULT 'RIA',
      created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS instruments (
      symbol     TEXT PRIMARY KEY,
      name       TEXT NOT NULL,
      cusip      TEXT
    );

    CREATE TABLE IF NOT EXISTS quotes (
      symbol     TEXT PRIMARY KEY REFERENCES instruments(symbol),
      bid        REAL NOT NULL,
      ask        REAL NOT NULL,
      as_of      TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS accounts (
      account_id   TEXT PRIMARY KEY,
      tenant       TEXT NOT NULL DEFAULT 'RIA',
      cash         REAL NOT NULL,
      credit_limit REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS controls_config (
      id                INTEGER PRIMARY KEY CHECK (id = 1),
      collar_bps        INTEGER NOT NULL,   -- collar half-width in basis points
      quote_max_age_sec INTEGER NOT NULL,   -- quote freshness bound
      dup_window_sec    INTEGER NOT NULL,   -- duplicate-detection window
      dup_threshold     INTEGER NOT NULL    -- identical orders that trip the guard
    );

    CREATE TABLE IF NOT EXISTS kill_switch (
      tenant     TEXT PRIMARY KEY,
      engaged    INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT,
      updated_by TEXT
    );

    CREATE TABLE IF NOT EXISTS orders (
      id          TEXT PRIMARY KEY,
      created_at  TEXT NOT NULL,
      username    TEXT NOT NULL,
      tenant      TEXT NOT NULL,
      account_id  TEXT NOT NULL,
      symbol      TEXT NOT NULL,
      side        TEXT NOT NULL,
      order_type  TEXT NOT NULL,
      quantity    INTEGER NOT NULL,
      limit_price REAL,
      signature   TEXT NOT NULL,            -- account|side|symbol|qty|type|limit
      decision    TEXT NOT NULL,            -- ALLOW or BLOCK
      overridable INTEGER NOT NULL DEFAULT 0,
      status      TEXT NOT NULL,            -- ACCEPTED, BLOCKED, OVERRIDDEN
      verdict     TEXT NOT NULL             -- JSON of the per-control results
    );
    CREATE INDEX IF NOT EXISTS idx_orders_sig ON orders(signature, created_at);

    -- Append-only approval and decision audit trail. The application never
    -- updates or deletes a row here; it is written once, which mirrors a
    -- write-once (WORM) 17a-4 retention posture.
    CREATE TABLE IF NOT EXISTS audit_log (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      ts         TEXT NOT NULL,
      actor      TEXT NOT NULL,
      event      TEXT NOT NULL,
      order_id   TEXT,
      detail     TEXT NOT NULL
    );
  `);
}

function seedIfEmpty() {
  const now = new Date().toISOString();
  const userCount = db.prepare("SELECT COUNT(*) AS n FROM users").get().n;
  if (userCount === 0) {
    const insUser = db.prepare("INSERT INTO users (username, role) VALUES (?, ?)");
    insUser.run("j.rivera", "advisor");
    insUser.run("s.chen", "supervisor");
  }

  const instrCount = db.prepare("SELECT COUNT(*) AS n FROM instruments").get().n;
  if (instrCount === 0) {
    const insInstr = db.prepare("INSERT INTO instruments (symbol, name, cusip) VALUES (?, ?, ?)");
    const insQuote = db.prepare("INSERT INTO quotes (symbol, bid, ask, as_of) VALUES (?, ?, ?, ?)");
    const rows = [
      ["AAPL", "Apple Inc.", "037833100", 227.40, 227.52],
      ["MSFT", "Microsoft Corp.", "594918104", 431.10, 431.34],
      ["NVDA", "NVIDIA Corp.", "67066G104", 138.62, 138.70],
      ["ZVZZT", "Test Symbol (NASDAQ)", null, 14.00, 14.06]
    ];
    for (const [sym, name, cusip, bid, ask] of rows) {
      insInstr.run(sym, name, cusip);
      insQuote.run(sym, bid, ask, now);
    }
  }

  const acctCount = db.prepare("SELECT COUNT(*) AS n FROM accounts").get().n;
  if (acctCount === 0) {
    const insAcct = db.prepare("INSERT INTO accounts (account_id, tenant, cash, credit_limit) VALUES (?, ?, ?, ?)");
    insAcct.run("ACME-0042", "RIA", 250000, 500000);
    insAcct.run("ACME-0100", "BD", 1000000, 2000000);
  }

  const cfg = db.prepare("SELECT COUNT(*) AS n FROM controls_config").get().n;
  if (cfg === 0) {
    db.prepare(
      "INSERT INTO controls_config (id, collar_bps, quote_max_age_sec, dup_window_sec, dup_threshold) VALUES (1, ?, ?, ?, ?)"
    ).run(300, 60, 60, 3);
  }

  for (const tenant of ["RIA", "BD"]) {
    const k = db.prepare("SELECT COUNT(*) AS n FROM kill_switch WHERE tenant = ?").get(tenant).n;
    if (k === 0) {
      db.prepare("INSERT INTO kill_switch (tenant, engaged, updated_at, updated_by) VALUES (?, 0, ?, ?)")
        .run(tenant, now, "seed");
    }
  }
}

function init() {
  migrate();
  seedIfEmpty();
}

module.exports = { db, init, DB_PATH };
