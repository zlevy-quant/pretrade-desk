"use strict";

// Route table for the API. Each route is { method, path, auth, handler }.
// A handler receives a ctx { params, body, session } and returns a plain object
// (sent as 200 JSON) or throws httpError(status, message). The tiny dispatcher
// lives in server.js, so there is no web framework dependency.

const crypto = require("crypto");
const { db } = require("./db");
const { login, sessionFromToken } = require("./auth");
const { evaluate, decide } = require("./engine");

function httpError(status, message) {
  const e = new Error(message);
  e.status = status;
  return e;
}

function audit(actor, event, orderId, detail) {
  db.prepare(
    "INSERT INTO audit_log (ts, actor, event, order_id, detail) VALUES (?, ?, ?, ?, ?)"
  ).run(new Date().toISOString(), actor, event, orderId || null, detail);
}

const routes = [
  {
    method: "POST", path: "/api/login", auth: false,
    handler: (ctx) => {
      const { username, role, tenant } = ctx.body || {};
      if (!username || !["advisor", "supervisor"].includes(role)) {
        throw httpError(400, "username and a valid role (advisor|supervisor) are required");
      }
      const t = tenant === "BD" ? "BD" : "RIA";
      const token = login(username, role, t);
      audit(username, "SESSION_OPEN", null, `session opened: ${username} (${role}), tenant ${t}`);
      return { token, username, role, tenant: t };
    }
  },

  {
    method: "GET", path: "/api/market", auth: true,
    handler: () => {
      const quotes = db.prepare(
        "SELECT q.symbol, i.name, i.cusip, q.bid, q.ask, q.as_of FROM quotes q JOIN instruments i ON i.symbol = q.symbol ORDER BY q.symbol"
      ).all();
      const config = db.prepare("SELECT * FROM controls_config WHERE id = 1").get();
      const now = Date.now();
      for (const q of quotes) {
        q.age_sec = Math.round((now - new Date(q.as_of).getTime()) / 1000);
        q.stale = q.age_sec > config.quote_max_age_sec;
      }
      return { quotes, config };
    }
  },

  {
    method: "GET", path: "/api/accounts", auth: true,
    handler: () => ({ accounts: db.prepare("SELECT * FROM accounts ORDER BY account_id").all() })
  },

  {
    method: "POST", path: "/api/market/:symbol/stale", auth: true,
    handler: (ctx) => {
      const old = new Date(Date.now() - 16 * 3600 * 1000).toISOString();
      const r = db.prepare("UPDATE quotes SET as_of = ? WHERE symbol = ?").run(old, ctx.params.symbol);
      if (r.changes === 0) throw httpError(404, "unknown symbol");
      audit(ctx.session.username, "MARKETDATA_STALE", null, `forced stale feed on ${ctx.params.symbol}`);
      return { ok: true };
    }
  },
  {
    method: "POST", path: "/api/market/:symbol/fresh", auth: true,
    handler: (ctx) => {
      const r = db.prepare("UPDATE quotes SET as_of = ? WHERE symbol = ?").run(new Date().toISOString(), ctx.params.symbol);
      if (r.changes === 0) throw httpError(404, "unknown symbol");
      audit(ctx.session.username, "MARKETDATA_FRESH", null, `refreshed feed on ${ctx.params.symbol}`);
      return { ok: true };
    }
  },

  {
    method: "GET", path: "/api/killswitch", auth: true,
    handler: (ctx) => {
      const k = db.prepare("SELECT * FROM kill_switch WHERE tenant = ?").get(ctx.session.tenant);
      return { tenant: ctx.session.tenant, engaged: !!(k && k.engaged) };
    }
  },
  {
    method: "POST", path: "/api/killswitch", auth: true,
    handler: (ctx) => {
      const engaged = ctx.body && ctx.body.engaged ? 1 : 0;
      db.prepare("UPDATE kill_switch SET engaged = ?, updated_at = ?, updated_by = ? WHERE tenant = ?")
        .run(engaged, new Date().toISOString(), ctx.session.username, ctx.session.tenant);
      audit(ctx.session.username, engaged ? "KILLSWITCH_ENGAGE" : "KILLSWITCH_RELEASE", null,
        `master kill switch ${engaged ? "ENGAGED" : "released"} for tenant ${ctx.session.tenant}`);
      return { tenant: ctx.session.tenant, engaged: !!engaged };
    }
  },

  {
    method: "POST", path: "/api/orders", auth: true,
    handler: (ctx) => {
      const b = ctx.body || {};
      const order = {
        account_id: String(b.account_id || "").trim(),
        symbol: String(b.symbol || "").trim(),
        side: String(b.side || "").trim(),
        order_type: String(b.order_type || "").trim(),
        quantity: Number.isFinite(b.quantity) ? Math.trunc(b.quantity) : parseInt(b.quantity, 10),
        limit_price: b.limit_price === "" || b.limit_price == null ? 0 : Number(b.limit_price)
      };
      if (!Number.isInteger(order.quantity)) order.quantity = NaN;

      const sctx = { username: ctx.session.username, tenant: ctx.session.tenant };
      const { controls, signature } = evaluate(order, sctx);
      const outcome = decide(controls);

      const id = "ORD-" + String(Date.now()).slice(-6) + "-" + crypto.randomBytes(2).toString("hex");
      const status = outcome.decision === "ALLOW" ? "ACCEPTED" : "BLOCKED";

      db.prepare(`INSERT INTO orders
        (id, created_at, username, tenant, account_id, symbol, side, order_type, quantity, limit_price, signature, decision, overridable, status, verdict)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(
        id, new Date().toISOString(), sctx.username, sctx.tenant,
        order.account_id, order.symbol, order.side, order.order_type,
        Number.isInteger(order.quantity) ? order.quantity : 0,
        order.limit_price || null, signature, outcome.decision,
        outcome.overridable ? 1 : 0, status, JSON.stringify(controls)
      );

      const blocked = controls.filter((c) => c.status === "block").map((c) => c.name).join(",");
      audit(sctx.username, status, id,
        `${order.account_id} ${order.side} ${order.quantity} ${order.symbol} -> ${status}` + (blocked ? ` [${blocked}]` : ""));

      return { id, decision: outcome.decision, overridable: outcome.overridable, status, controls };
    }
  },

  {
    method: "POST", path: "/api/orders/:id/override", auth: true,
    handler: (ctx) => {
      if (ctx.session.role !== "supervisor") {
        throw httpError(403, "override requires a trade-desk supervisor (Series 24)");
      }
      const reason = String((ctx.body && ctx.body.reason) || "").trim();
      if (!reason) throw httpError(400, "a reason is required before release");

      const order = db.prepare("SELECT * FROM orders WHERE id = ?").get(ctx.params.id);
      if (!order) throw httpError(404, "order not found");
      if (!(order.decision === "BLOCK" && order.overridable === 1)) {
        throw httpError(409, "order is not an overridable soft block");
      }
      db.prepare("UPDATE orders SET status = 'OVERRIDDEN' WHERE id = ?").run(order.id);
      audit(ctx.session.username, "OVERRIDE", order.id,
        `released on override by ${ctx.session.username} (supervisor); reason: ${reason}`);
      return { id: order.id, status: "OVERRIDDEN", reason };
    }
  },

  {
    method: "GET", path: "/api/blotter", auth: true,
    handler: () => ({
      orders: db.prepare(
        "SELECT id, created_at, username, account_id, symbol, side, quantity, status FROM orders ORDER BY created_at DESC LIMIT 100"
      ).all()
    })
  },

  {
    method: "GET", path: "/api/audit", auth: true,
    handler: () => ({
      audit: db.prepare("SELECT ts, actor, event, order_id, detail FROM audit_log ORDER BY id DESC LIMIT 200").all()
    })
  }
];

module.exports = { routes, httpError, sessionFromToken };
