"use strict";

// Minimal demo session auth. A login issues an opaque token stored in the
// sessions table; protected routes require it as a Bearer token. This is a
// demo authentication scheme, not a production identity provider.

const crypto = require("crypto");
const { db } = require("./db");

function login(username, role, tenant) {
  const token = crypto.randomUUID();
  db.prepare(
    "INSERT INTO sessions (token, username, role, tenant, created_at) VALUES (?, ?, ?, ?, ?)"
  ).run(token, username, role, tenant || "RIA", new Date().toISOString());
  return token;
}

function sessionFromToken(token) {
  if (!token) return null;
  return db.prepare("SELECT * FROM sessions WHERE token = ?").get(token) || null;
}

function requireAuth(req, res, next) {
  const header = req.headers["authorization"] || "";
  const token = header.startsWith("Bearer ") ? header.slice(7) : null;
  const session = sessionFromToken(token);
  if (!session) return res.status(401).json({ error: "unauthenticated" });
  req.session = session;
  next();
}

module.exports = { login, requireAuth, sessionFromToken };
