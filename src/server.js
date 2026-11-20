"use strict";

// Zero-dependency HTTP server. Serves the JSON API and the static web UI using
// only Node built-ins (node:http, node:sqlite). No npm install is required.

const http = require("http");
const fs = require("fs");
const path = require("path");
const { init, DB_PATH } = require("./db");
const { routes, sessionFromToken } = require("./routes");

init(); // create the schema and seed demo data on first boot

const PUBLIC_DIR = path.join(__dirname, "..", "public");
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".json": "application/json; charset=utf-8"
};

function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  res.end(body);
}

function matchRoute(method, pathname) {
  for (const r of routes) {
    if (r.method !== method) continue;
    const rp = r.path.split("/");
    const pp = pathname.split("/");
    if (rp.length !== pp.length) continue;
    const params = {};
    let ok = true;
    for (let i = 0; i < rp.length; i++) {
      if (rp[i].startsWith(":")) params[rp[i].slice(1)] = decodeURIComponent(pp[i]);
      else if (rp[i] !== pp[i]) { ok = false; break; }
    }
    if (ok) return { route: r, params };
  }
  return null;
}

function readBody(req) {
  return new Promise((resolve) => {
    let data = "";
    let tooBig = false;
    req.on("data", (chunk) => {
      data += chunk;
      if (data.length > 1e6) { tooBig = true; req.destroy(); }
    });
    req.on("end", () => {
      if (tooBig) return resolve({});
      if (!data) return resolve({});
      try { resolve(JSON.parse(data)); } catch (e) { resolve({ __parse_error: true }); }
    });
    req.on("error", () => resolve({}));
  });
}

function serveStatic(req, res, pathname) {
  let rel = pathname === "/" ? "/index.html" : pathname;
  const filePath = path.normalize(path.join(PUBLIC_DIR, rel));
  if (!filePath.startsWith(PUBLIC_DIR)) { res.writeHead(403); return res.end("forbidden"); }
  fs.readFile(filePath, (err, buf) => {
    if (err) {
      res.writeHead(404, { "Content-Type": "text/plain" });
      return res.end("Not found");
    }
    const ext = path.extname(filePath).toLowerCase();
    res.writeHead(200, { "Content-Type": MIME[ext] || "application/octet-stream" });
    res.end(buf);
  });
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://localhost");
    const pathname = url.pathname;

    if (pathname === "/api/health") return sendJson(res, 200, { ok: true, db: DB_PATH });

    if (pathname.startsWith("/api/")) {
      const m = matchRoute(req.method, pathname);
      if (!m) return sendJson(res, 404, { error: "not found" });

      let session = null;
      if (m.route.auth) {
        const header = req.headers["authorization"] || "";
        const tok = header.startsWith("Bearer ") ? header.slice(7) : null;
        session = sessionFromToken(tok);
        if (!session) return sendJson(res, 401, { error: "unauthenticated" });
      }

      let body = {};
      if (req.method === "POST" || req.method === "PUT") body = await readBody(req);
      if (body && body.__parse_error) return sendJson(res, 400, { error: "invalid JSON body" });

      try {
        const result = m.route.handler({ params: m.params, body, session, query: url.searchParams });
        return sendJson(res, 200, result);
      } catch (e) {
        return sendJson(res, e.status || 500, { error: e.message || "internal error" });
      }
    }

    // static UI
    if (req.method !== "GET") { res.writeHead(405); return res.end("method not allowed"); }
    return serveStatic(req, res, pathname);
  } catch (e) {
    sendJson(res, 500, { error: "internal error" });
  }
});

const PORT = process.env.PORT || 8080;
server.listen(PORT, () => {
  console.log(`Pre-Trade Desk running at http://localhost:${PORT}`);
  console.log(`Database: ${DB_PATH}`);
});
