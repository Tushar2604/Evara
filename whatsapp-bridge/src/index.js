// WhatsApp bridge — HTTP control plane over the Baileys session manager.
//
// Binds to localhost by default: this service has no per-tenant authorization
// of its own, so anything that can reach it can drive any linked account. The
// API is the only intended caller, they share a container, and a shared secret
// is checked on every request as a second lock.

import express from "express";
import pg from "pg";
import pino from "pino";

import { normalizeDbUrl, wantsTls } from "./dbUrl.js";
import { SessionManager } from "./sessions.js";

const log = pino({ level: process.env.BRIDGE_LOG_LEVEL || "info" });

const PORT = Number(process.env.BRIDGE_PORT || 8081);
const HOST = process.env.BRIDGE_HOST || "127.0.0.1";
const TOKEN = process.env.BRIDGE_TOKEN || "";
const API_BASE = (process.env.BRIDGE_API_BASE || "http://127.0.0.1:8000").replace(/\/$/, "");
const EVENT_PATH = "/api/v1/whatsapp-web/bridge-events";
const MEDIA_PATH = "/api/v1/whatsapp-web/bridge-media";
const HISTORY_PATH = "/api/v1/whatsapp-web/bridge-history";

if (!TOKEN) {
  log.error("BRIDGE_TOKEN is unset — refusing to start an unauthenticated bridge.");
  process.exit(1);
}


const pool = new pg.Pool({
  connectionString: normalizeDbUrl(process.env.DATABASE_URL),
  // The bridge is bursty and mostly idle; a big pool would just hold Neon
  // connections open next to the API's own pool.
  max: Number(process.env.BRIDGE_DB_POOL || 4),
  // Managed Postgres closes idle connections server-side — Neon's free tier
  // suspends the whole compute after a few minutes. The pool cannot tell a
  // dropped socket from a live one until it runs a query, which is where
  // "Connection terminated unexpectedly" comes from. Expiring our own idle
  // connections first means we reconnect on demand instead of handing out a
  // socket the server has already closed.
  idleTimeoutMillis: 10_000,
  // Generous on purpose. A suspended Neon compute can take 10-20s to accept its
  // first connection, and a timeout shorter than that cold start turns a slow
  // wake-up into "Connection terminated due to connection timeout" — a failure
  // that looks like a dead database but is really an impatient client.
  connectionTimeoutMillis: Number(process.env.BRIDGE_DB_CONNECT_TIMEOUT_MS || 30_000),
  keepAlive: true,
  // Encrypted, but without chain verification. Managed providers front their
  // databases with certificates Node's default trust store rejects (Supabase's
  // pooler is self-signed); the alternative would be shipping each provider's
  // CA bundle. The connection is still TLS — this only skips verifying who is
  // on the other end, which is the same posture the API side takes.
  ssl: wantsTls(process.env.DATABASE_URL) ? { rejectUnauthorized: false } : undefined,
});

// An idle client that dies emits 'error' on the pool. With no listener that is
// an uncaught exception, which used to take the whole bridge down every time
// the database went away for a moment.
pool.on("error", (err) => {
  log.warn({ err: err.message }, "idle database client error");
});

/**
 * Take the cold start once, at boot, instead of on someone's first scan.
 *
 * Pairing is the latency-sensitive path: the QR is only valid for ~20s, so a
 * 15s database wake-up inside that window is the difference between a code that
 * scans and one that has already expired. Waking the database here moves that
 * cost to a moment when nobody is waiting.
 */
async function warmDatabase() {
  try {
    await pool.query("SELECT 1");
    log.info("database reachable");
  } catch (err) {
    // Not fatal: the bridge still serves, and the retry path will wake the
    // database on demand. Worth logging loudly because it predicts slow pairing.
    log.warn({ err: err.message }, "database not reachable at boot");
  }
}

// This container starts the bridge before Uvicorn is accepting connections
// (see scripts/start.sh) — migrations plus Python/DB startup routinely take
// longer than Baileys takes to resume a session and fire its first
// connection event. Without a retry, that first notify() (often a "linked"
// status, sometimes a customer's actual first message) hit a closed port,
// failed, and was gone for good. `event` matters here — a "message" dropped
// this way is one the assistant was never even asked to answer, silently.
const NOTIFY_RETRIES = 5;
const NOTIFY_BACKOFF_MS = 1000; // 1s, 2s, 4s, 8s, 16s — ~31s worst case

/** Report an event to the API. Never throws — a momentarily unreachable API
 * must not kill the WhatsApp socket that produced the event. Retries only a
 * network-level failure (connection refused/reset — the API process isn't
 * there yet, or isn't there anymore); an HTTP error response is a real
 * answer from a live server and is not retried. */
async function notify(sessionId, event, payload, attempt = 0) {
  let res;
  try {
    res = await fetch(`${API_BASE}${EVENT_PATH}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Bridge-Token": TOKEN,
      },
      body: JSON.stringify({ session_id: sessionId, event, ...payload }),
    });
  } catch (err) {
    if (attempt < NOTIFY_RETRIES) {
      const delay = NOTIFY_BACKOFF_MS * 2 ** attempt;
      log.warn(
        { sessionId, event, err: err.message, attempt: attempt + 1, delay },
        "could not reach api, retrying",
      );
      await new Promise((resolve) => setTimeout(resolve, delay));
      return notify(sessionId, event, payload, attempt + 1);
    }
    log.warn({ sessionId, event, err: err.message }, "could not reach api, giving up");
    return null;
  }
  if (!res.ok) {
    log.warn({ sessionId, event, status: res.status }, "api rejected bridge event");
    return null;
  }
  const body = await res.json().catch(() => null);
  // The API already tells us when a session row is gone; until now nothing
  // acted on it, so an unlinked session kept its socket and its reconnect
  // timer forever — retrying a link that can never be restored, and filling
  // the logs with failures for a session the user deleted minutes ago.
  if (body?.status === "unknown_session") {
    log.info({ sessionId }, "session no longer exists — stopping socket");
    manager.stop(sessionId, { keepAuth: false }).catch(() => {});
  }
  return body;
}

/**
 * Ship media bytes to the API, which owns the storage backend.
 *
 * Multipart rather than base64 inside the JSON event: `express.json` here is
 * capped at 1mb and the API's own body limits are no larger, so anything but a
 * thumbnail would be rejected. Returns the storage key the API assigned.
 */
async function uploadMedia(sessionId, messageId, media, buffer) {
  const form = new FormData();
  form.append("session_id", sessionId);
  form.append("message_id", messageId);
  form.append("media_kind", media.kind);
  form.append(
    "file",
    new Blob([buffer], { type: media.mime_type || "application/octet-stream" }),
    media.filename || `${media.kind}-${messageId}`,
  );
  const res = await fetch(`${API_BASE}${MEDIA_PATH}`, {
    method: "POST",
    headers: { "X-Bridge-Token": TOKEN },
    body: form,
  });
  if (!res.ok) {
    log.warn({ sessionId, status: res.status }, "api rejected media upload");
    return null;
  }
  const body = await res.json().catch(() => null);
  return body?.storage_key || null;
}

/** Post one batch of imported history. Returns false rather than throwing so a
 * rejected chunk does not abort the rest of the sync. */
async function syncHistory(sessionId, batch) {
  try {
    const res = await fetch(`${API_BASE}${HISTORY_PATH}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Bridge-Token": TOKEN },
      body: JSON.stringify({ session_id: sessionId, ...batch }),
    });
    if (!res.ok) {
      log.warn({ sessionId, status: res.status }, "api rejected history batch");
      return false;
    }
    return true;
  } catch (err) {
    log.warn({ sessionId, err: err.message }, "could not deliver history batch");
    return false;
  }
}

const manager = new SessionManager({ pool, notify, uploadMedia, syncHistory });

const app = express();
app.use(express.json({ limit: "1mb" }));

app.use((req, res, next) => {
  if (req.path === "/healthz") return next();
  if (req.get("X-Bridge-Token") !== TOKEN) {
    return res.status(403).json({ detail: "Invalid bridge token" });
  }
  next();
});

app.get("/healthz", (_req, res) => {
  res.json({ status: "ok", sessions: manager.sockets.size });
});

/** Start a session — begins the pairing flow, or restores an existing link. */
app.post("/sessions/:id/start", async (req, res) => {
  try {
    await manager.start(req.params.id);
    res.json({ status: "starting" });
  } catch (err) {
    log.error({ err: err.message }, "session start failed");
    res.status(500).json({ detail: err.message });
  }
});

/** Stop the socket but keep credentials, so it can resume without a re-scan. */
app.post("/sessions/:id/stop", async (req, res) => {
  await manager.stop(req.params.id, { keepAuth: true });
  res.json({ status: "stopped" });
});

/** Unlink for real: tells WhatsApp to drop the device and wipes the keys. */
app.post("/sessions/:id/logout", async (req, res) => {
  await manager.stop(req.params.id, { keepAuth: false });
  res.json({ status: "logged_out" });
});

app.post("/sessions/:id/send", async (req, res) => {
  const { jid, text } = req.body || {};
  if (!jid || !text) {
    return res.status(400).json({ detail: "jid and text are required" });
  }
  try {
    await manager.sendText(req.params.id, jid, text);
    res.json({ status: "sent" });
  } catch (err) {
    res.status(502).json({ detail: err.message });
  }
});

/**
 * Send an attachment. The body is the file's raw bytes — not multipart, not
 * base64 — with everything else (jid, kind, filename, caption) in the query
 * string, so this can sit next to the JSON-only routes above without a
 * multipart parser. `express.raw` here is scoped to this one route; the
 * `express.json` limit above only ever matches JSON requests and leaves this
 * one alone.
 */
app.post(
  "/sessions/:id/send-media",
  express.raw({ type: () => true, limit: `${Number(process.env.BRIDGE_MAX_MEDIA_MB || 16)}mb` }),
  async (req, res) => {
    const { jid, kind, mimeType, fileName, caption } = req.query;
    const buffer = req.body;
    if (!jid || !Buffer.isBuffer(buffer) || !buffer.length) {
      return res.status(400).json({ detail: "jid and file bytes are required" });
    }
    try {
      await manager.sendMedia(req.params.id, jid, kind || "document", buffer, {
        mimeType,
        fileName,
        caption,
      });
      res.json({ status: "sent" });
    } catch (err) {
      res.status(502).json({ detail: err.message });
    }
  },
);

app.get("/sessions", (_req, res) => {
  res.json({ sessions: [...manager.sockets.keys()] });
});

/**
 * Restore links after a restart.
 *
 * The API knows which sessions were linked; the bridge holds no state of its
 * own across restarts. Without this, a redeploy or a free-tier sleep would
 * leave every linked account silently dead until someone re-scanned.
 */
// Each session start pulls a full history sync (sessions.js: syncFullHistory)
// into this process's memory. Kicking every linked account off at once on a
// redeploy — which this used to do, with no await between them — spikes CPU
// and memory right as the Python API in the same container is also cold-
// starting, on a Render instance sized for one thing running hard at a time,
// not several. Staggering costs nothing (accounts were already dead since
// the last restart) and keeps the herd from arriving together.
const _RESUME_STAGGER_MS = 2000;

async function resumeLinkedSessions() {
  try {
    const { rows } = await pool.query(
      `SELECT id FROM whatsapp_web_sessions
       WHERE status IN ('linked', 'disconnected') AND linked_at IS NOT NULL`,
    );
    for (const [i, row] of rows.entries()) {
      if (i > 0) await sleep(_RESUME_STAGGER_MS);
      manager.start(row.id).catch((err) =>
        log.warn({ sessionId: row.id, err: err.message }, "resume failed"),
      );
    }
    if (rows.length) log.info({ count: rows.length }, "resuming linked sessions");
  } catch (err) {
    log.error({ err: err.message }, "could not resume sessions");
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const server = app.listen(PORT, HOST, async () => {
  log.info({ host: HOST, port: PORT }, "whatsapp bridge listening");
  await warmDatabase();
  await resumeLinkedSessions();
});

async function shutdown(signal) {
  log.info({ signal }, "shutting down");
  server.close();
  await manager.shutdown();
  await pool.end().catch(() => {});
  process.exit(0);
}

// Node's default for an unhandled rejection is to kill the process. That is the
// wrong trade here: nearly every async path in this service touches Postgres or
// a WhatsApp socket, and a managed database that briefly refuses a connection
// (Neon suspends when idle) would otherwise take the bridge down for good. The
// API keeps serving either way, so the failure is invisible — it shows up only
// as "the bridge isn't responding" the next time someone tries to pair.
process.on("unhandledRejection", (reason) => {
  log.error({ err: reason instanceof Error ? reason.message : String(reason) }, "unhandled rejection");
});
process.on("uncaughtException", (err) => {
  log.error({ err: err.message }, "uncaught exception");
});

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
