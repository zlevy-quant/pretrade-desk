"use strict";

// Deletes the database file so the next boot re-seeds from scratch.
const fs = require("fs");
const { DB_PATH } = require("./db");

for (const f of [DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm"]) {
  try { fs.unlinkSync(f); console.log("removed " + f); } catch (e) { /* not present */ }
}
console.log("Database reset. It will be recreated and seeded on next start.");
