import { DurableObject } from "cloudflare:workers";
import page from "./page.html";

const BASE_PATH = "/lunch-line-monitor";
const ROLE_HEADER = "x-lunch-line-role";
// one history row per 2 s keeps writes (~43k/day) under the free plan's 100k sqlite rows written per day
const RECORD_INTERVAL_MILLISECONDS = 2000;

// one durable object fans camera updates out to every open browser over hibernatable websockets
export class LunchLine extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    // memory only: lost if the object hibernates, which only happens after the camera stops sending
    this.latestUpdate = null;
    this.lastRecordedAt = 0;
    const sql = this.ctx.storage.sql;
    sql.exec("CREATE TABLE IF NOT EXISTS history (recorded_at INTEGER PRIMARY KEY, count INTEGER NOT NULL, people TEXT NOT NULL)");
    const columns = sql.exec("PRAGMA table_info(history)").toArray().map((column) => column.name);
    if (!columns.includes("frame_key")) sql.exec("ALTER TABLE history ADD COLUMN frame_key TEXT");
  }

  history(sinceMilliseconds) {
    return this.ctx.storage.sql
      .exec("SELECT recorded_at, count, people, frame_key FROM history WHERE recorded_at >= ? ORDER BY recorded_at", sinceMilliseconds)
      .toArray();
  }

  async fetch(request) {
    const role = request.headers.get(ROLE_HEADER);
    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server, [role]);
    if (role === "viewer" && this.latestUpdate !== null) server.send(this.latestUpdate);
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(socket, message) {
    if (!this.ctx.getTags(socket).includes("camera")) return;
    const update = JSON.parse(message);
    if (typeof update.count !== "number" || !Array.isArray(update.people)) {
      throw new Error(`malformed camera update: ${String(message).slice(0, 200)}`);
    }
    update.received_at = Date.now();
    this.latestUpdate = JSON.stringify(update);
    for (const viewer of this.ctx.getWebSockets("viewer")) viewer.send(this.latestUpdate);
    // every frame is archived (~1.6/s from the pi); list them per day at /lunch-line-monitor/frames?day=YYYY-MM-DD
    let frameKey = null;
    if (update.jpeg) {
      frameKey = `${pacificTime(update.received_at).slice(0, 10)}/${update.received_at}.jpg`;
      const jpegBytes = Uint8Array.from(atob(update.jpeg), (character) => character.charCodeAt(0));
      await this.env.FRAMES.put(frameKey, jpegBytes, {
        httpMetadata: { contentType: "image/jpeg" },
        customMetadata: { count: String(update.count), people: JSON.stringify(update.people) },
      });
    }
    if (update.received_at - this.lastRecordedAt < RECORD_INTERVAL_MILLISECONDS) return;
    this.lastRecordedAt = update.received_at;
    this.ctx.storage.sql.exec(
      "INSERT OR REPLACE INTO history (recorded_at, count, people, frame_key) VALUES (?, ?, ?, ?)",
      update.received_at,
      update.count,
      JSON.stringify(update.people),
      frameKey
    );
  }

  webSocketClose(socket, code, reason) {
    socket.close(code, reason);
  }
}

async function bearerTokenMatches(authorizationHeader, expectedToken) {
  if (!expectedToken) throw new Error("INGEST_TOKEN secret is not set");
  const encoder = new TextEncoder();
  const given = encoder.encode(authorizationHeader ?? "");
  const expected = encoder.encode(`Bearer ${expectedToken}`);
  return given.byteLength === expected.byteLength && crypto.subtle.timingSafeEqual(given, expected);
}

function pacificTime(milliseconds) {
  return new Date(milliseconds).toLocaleString("sv-SE", { timeZone: "America/Los_Angeles" });
}

function lunchLineFor(env) {
  return env.LUNCH_LINE.get(env.LUNCH_LINE.idFromName("nueva-lunch-line"));
}

// GET /lunch-line-monitor/history?hours=24[&format=json]: one row per ~2 s of camera updates
async function historyResponse(url, env) {
  const hours = Number(url.searchParams.get("hours") ?? 24);
  if (!(hours > 0)) return new Response("hours must be a positive number", { status: 400 });
  const rows = await lunchLineFor(env).history(Date.now() - hours * 3600 * 1000);
  if (url.searchParams.get("format") === "json") {
    const body = rows.map((row) => ({
      recorded_at: row.recorded_at,
      pacific_time: pacificTime(row.recorded_at),
      count: row.count,
      people: JSON.parse(row.people),
      frame: row.frame_key, // download at /lunch-line-monitor/frames/<frame> with the bearer token
    }));
    return Response.json(body, { headers: { "cache-control": "no-store" } });
  }
  const lines = ["pacific_time,unix_milliseconds,count,frame"];
  for (const row of rows) lines.push(`${pacificTime(row.recorded_at)},${row.recorded_at},${row.count},${row.frame_key ?? ""}`);
  return new Response(lines.join("\n") + "\n", {
    headers: { "content-type": "text/csv; charset=utf-8", "content-disposition": 'inline; filename="lunch-line-history.csv"', "cache-control": "no-store" },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "");

    if (path === `${BASE_PATH}/history`) return historyResponse(url, env);

    // saved frames are an archive of students' faces, so unlike the live view they need the token
    if (path === `${BASE_PATH}/frames` || path.startsWith(`${BASE_PATH}/frames/`)) {
      if (!(await bearerTokenMatches(request.headers.get("Authorization"), env.INGEST_TOKEN))) {
        return new Response("unauthorized", { status: 401 });
      }
    }

    // GET /lunch-line-monitor/frames?day=YYYY-MM-DD[&cursor=...]: up to 1000 frames per page, oldest first
    if (path === `${BASE_PATH}/frames`) {
      const day = url.searchParams.get("day");
      if (!/^\d{4}-\d{2}-\d{2}$/.test(day ?? "")) return new Response("day must be YYYY-MM-DD (pacific)", { status: 400 });
      const listing = await env.FRAMES.list({ prefix: `${day}/`, cursor: url.searchParams.get("cursor") ?? undefined, include: ["customMetadata"] });
      const frames = listing.objects.map((object) => ({
        key: object.key,
        count: Number(object.customMetadata.count),
        people: JSON.parse(object.customMetadata.people),
      }));
      return Response.json({ frames, cursor: listing.truncated ? listing.cursor : null }, { headers: { "cache-control": "no-store" } });
    }

    if (path.startsWith(`${BASE_PATH}/frames/`)) {
      const frame = await env.FRAMES.get(path.slice(`${BASE_PATH}/frames/`.length));
      if (frame === null) return new Response("no such frame", { status: 404 });
      return new Response(frame.body, { headers: { "content-type": "image/jpeg", "cache-control": "private, max-age=31536000, immutable" } });
    }

    if (path === BASE_PATH) {
      return new Response(page, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-cache" } });
    }

    if (path === `${BASE_PATH}/live` || path === `${BASE_PATH}/ingest`) {
      if (request.headers.get("Upgrade") !== "websocket") return new Response("expected a websocket upgrade", { status: 426 });
      let role = "viewer";
      if (path.endsWith("/ingest")) {
        if (!(await bearerTokenMatches(request.headers.get("Authorization"), env.INGEST_TOKEN))) {
          return new Response("unauthorized", { status: 401 });
        }
        role = "camera";
      }
      const forwarded = new Request(request);
      forwarded.headers.set(ROLE_HEADER, role); // always overwrite so a browser can't claim to be the camera
      return lunchLineFor(env).fetch(forwarded);
    }

    return new Response("not found", { status: 404 });
  },
};
