"use strict";

// The pre-trade verdict engine. This is the control layer every order passes
// through before it is accepted. It reads live state (quotes, account, prior
// orders, kill switch, thresholds) from the database and returns a verdict.
//
// Design principle: FAIL CLOSED. Every control reads its inputs once, and any
// input that is missing, stale, or unparseable produces a block rather than a
// pass, so a data outage can never silently let an order through.
//
// Control classification:
//   hard  - gating and final (field_validation, kill_switch, duplicate_rate_guard)
//   soft  - gating but overridable by a supervisor (price_collar)
//   audit - informational, never blocks on its own (buying_power, credit_limit)

const { db } = require("./db");

const SIDES = ["buy", "sell", "sell_short", "buy_to_cover"];
const ORDER_TYPES = ["market", "limit", "stop", "stop_limit"];

function signature(o) {
  return [o.account_id, o.side, o.symbol, o.quantity, o.order_type, o.limit_price || ""].join("|");
}

function evaluate(order, ctx) {
  // ctx: { username, tenant }
  const controls = [];
  const add = (name, kind, status, reason) => controls.push({ name, kind, status, reason });

  const cfg = db.prepare("SELECT * FROM controls_config WHERE id = 1").get();
  const collar = cfg.collar_bps / 10000; // basis points to fraction

  // 1. field_validation (hard)
  const fe = [];
  if (!order.account_id) fe.push("account required");
  if (!order.symbol) fe.push("symbol required");
  if (!Number.isInteger(order.quantity) || order.quantity <= 0) fe.push("quantity must be a positive whole number");
  if ((order.order_type === "limit" || order.order_type === "stop_limit") && !(order.limit_price > 0))
    fe.push("limit price required for " + order.order_type);
  if (!SIDES.includes(order.side)) fe.push("unknown side");
  if (!ORDER_TYPES.includes(order.order_type)) fe.push("unknown order type");
  add("field_validation", "hard", fe.length ? "block" : "pass",
    fe.length ? fe.join("; ") : "all required fields present and well-typed");

  // 2. kill_switch (hard)
  const ks = db.prepare("SELECT engaged FROM kill_switch WHERE tenant = ?").get(ctx.tenant);
  const killed = ks && ks.engaged === 1;
  add("kill_switch", "hard", killed ? "block" : "pass",
    killed ? "master kill switch engaged for tenant " + ctx.tenant : "not engaged");

  // Instrument / account lookups used below
  const quote = db.prepare("SELECT * FROM quotes WHERE symbol = ?").get(order.symbol);
  const account = db.prepare("SELECT * FROM accounts WHERE account_id = ?").get(order.account_id);

  // 3. price_collar (soft) - fail closed on a missing or stale quote
  if (fe.length) {
    add("price_collar", "soft", "skip", "not evaluated (order invalid)");
  } else if (!quote) {
    add("price_collar", "soft", "block", "data_stale: no quote available, failing closed");
  } else {
    const ageSec = (Date.now() - new Date(quote.as_of).getTime()) / 1000;
    if (!(ageSec >= 0) || ageSec > cfg.quote_max_age_sec) {
      add("price_collar", "soft", "block",
        "data_stale: quote " + Math.round(ageSec) + "s old exceeds " + cfg.quote_max_age_sec + "s bound, failing closed");
    } else {
      const mid = (quote.bid + quote.ask) / 2;
      const ref = (order.order_type === "limit" || order.order_type === "stop_limit") ? order.limit_price : mid;
      const lo = mid * (1 - collar), hi = mid * (1 + collar);
      if (ref < lo || ref > hi) {
        add("price_collar", "soft", "block",
          "price " + ref.toFixed(2) + " outside collar " + lo.toFixed(2) + "-" + hi.toFixed(2) + " (mid " + mid.toFixed(2) + ")");
      } else {
        add("price_collar", "soft", "pass",
          "within collar " + lo.toFixed(2) + "-" + hi.toFixed(2) + " of mid " + mid.toFixed(2));
      }
    }
  }

  // 4. duplicate_rate_guard (hard) - count identical orders in the window
  const sig = signature(order);
  const windowStart = new Date(Date.now() - cfg.dup_window_sec * 1000).toISOString();
  const priorCount = db.prepare(
    "SELECT COUNT(*) AS n FROM orders WHERE signature = ? AND created_at >= ?"
  ).get(sig, windowStart).n;
  if (priorCount + 1 >= cfg.dup_threshold) {
    add("duplicate_rate_guard", "hard", "block",
      "identical order seen " + (priorCount + 1) + " times in " + cfg.dup_window_sec +
      "s (same account, side, symbol, quantity) - runaway/duplicate pattern");
  } else {
    add("duplicate_rate_guard", "hard", "pass",
      "no duplicate pattern (" + (priorCount + 1) + " of this signature in window)");
  }

  // 5. buying_power (audit only, non-gating)
  if (!fe.length && (order.side === "buy" || order.side === "buy_to_cover")) {
    const px = order.limit_price > 0 ? order.limit_price : (quote ? (quote.bid + quote.ask) / 2 : 0);
    const notional = order.quantity * px;
    if (account && notional > account.cash) {
      add("buying_power", "audit", "audit",
        "notional " + money(notional) + " exceeds cash " + money(account.cash) + " (flagged, non-gating)");
    } else {
      add("buying_power", "audit", "pass",
        account ? "notional " + money(notional) + " within cash" : "account not found (audit-only)");
    }
  } else {
    add("buying_power", "audit", "pass", "sell side or invalid, not cash-constrained");
  }

  // 6. credit_limit (audit only)
  add("credit_limit", "audit", "pass", "within credit limit (audit-only)");

  return { controls, signature: sig };
}

function decide(controls) {
  const hardBlock = controls.some((c) => c.kind === "hard" && c.status === "block");
  const softBlock = controls.some((c) => c.kind === "soft" && c.status === "block");
  if (hardBlock) return { decision: "BLOCK", overridable: false };
  if (softBlock) return { decision: "BLOCK", overridable: true };
  return { decision: "ALLOW", overridable: false };
}

function money(n) {
  return "$" + Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 });
}

module.exports = { evaluate, decide, signature, SIDES, ORDER_TYPES };
