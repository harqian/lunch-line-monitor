import { DurableObject } from "cloudflare:workers";
import page from "./page.html";

const BASE_PATH = "/lunch-line-monitor";
const ROLE_HEADER = "x-lunch-line-role";
// one history row per 2 s keeps writes (~43k/day) under the free plan's 100k sqlite rows written per day
const RECORD_INTERVAL_MILLISECONDS = 2000;
const MAXIMUM_FEEDBACK_CHARACTERS = 2000;
// retain two extra minutes so the worker-to-object call cannot push a 90-minute request outside the cache
const RECENT_HISTORY_MILLISECONDS = 92 * 60 * 1000;
const MAXIMUM_HISTORY_HOURS = 168;
const HISTORY_CACHE_SECONDS = 300;
// the page's chart loads fixed tiles so every viewer shares the same cache entries.
// key = bucket length in seconds (0 = every reading), value = tile length in seconds. page.html has the same table
const CHART_TILE_SECONDS = { 0: 30 * 60, 60: 6 * 3600, 120: 12 * 3600, 900: 24 * 3600 };
// a tile that ended before the request never changes again
const PAST_CHART_TILE_CACHE_SECONDS = 86400;

// one durable object fans camera updates out to every open browser over hibernatable websockets
export class LunchLine extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    // memory only: lost if the object hibernates, which only happens after the camera stops sending
    this.latestUpdate = null;
    this.lastRecordedAt = 0;
    this.recentHistory = null;
    this.firstRecordedAt = null;
    this.rolledUpThrough = null;
    // the free plan's daily rows-read quota makes every sql call throw, even CREATE TABLE IF NOT EXISTS.
    // a throw here would kill the object and with it the live feed (11/20), so storage failures only stop recording
    try {
      const sql = this.ctx.storage.sql;
      sql.exec("CREATE TABLE IF NOT EXISTS history (recorded_at INTEGER PRIMARY KEY, count INTEGER NOT NULL, people TEXT NOT NULL)");
      sql.exec("CREATE TABLE IF NOT EXISTS feedback (submitted_at INTEGER NOT NULL, message TEXT NOT NULL)");
      // one summary row per minute of readings, so the 6 h to 7 d charts read 1/30 of the rows (see rollUpMinutes)
      sql.exec("CREATE TABLE IF NOT EXISTS history_minutes (minute_start INTEGER PRIMARY KEY, readings INTEGER NOT NULL, count_sum REAL NOT NULL, count_low REAL NOT NULL, count_high REAL NOT NULL)");
    } catch (error) {
      console.error(`lunch line storage unavailable at startup: ${error}`);
    }
  }

  history(sinceMilliseconds) {
    const recentSince = Date.now() - RECENT_HISTORY_MILLISECONDS;
    if (sinceMilliseconds >= recentSince) {
      if (this.recentHistory === null) {
        try {
          this.recentHistory = this.queryHistory(recentSince);
        } catch (error) {
          // database blocked (daily read quota): the chart runs from memory, filled by live updates since the object started
          console.error(`lunch line history read failed, serving memory only: ${error}`);
          this.recentHistory = [];
        }
      }
      this.recentHistory = this.recentHistory.filter((row) => row.recorded_at >= recentSince);
      return this.recentHistory.filter((row) => row.recorded_at >= sinceMilliseconds);
    }
    return this.queryHistory(sinceMilliseconds);
  }

  queryHistory(sinceMilliseconds) {
    return this.ctx.storage.sql
      .exec("SELECT recorded_at, count, people FROM history WHERE recorded_at >= ? ORDER BY recorded_at", sinceMilliseconds)
      .toArray();
  }

  // folds the readings since the last call into history_minutes, redoing the minute that was in progress.
  // each reading is read once, so this costs at most the ~43k rows/day that are written anyway.
  // the very first call summarizes the whole history table in one statement
  rollUpMinutes() {
    const sql = this.ctx.storage.sql;
    if (this.rolledUpThrough === null) {
      this.rolledUpThrough = sql.exec("SELECT MAX(minute_start) AS latest FROM history_minutes").one().latest ?? 0;
    }
    const currentMinute = Math.floor(Date.now() / 60000) * 60000;
    sql.exec(
      "INSERT OR REPLACE INTO history_minutes (minute_start, readings, count_sum, count_low, count_high) " +
        "SELECT (recorded_at / 60000) * 60000 AS minute_start, COUNT(*), SUM(count), MIN(count), MAX(count) FROM history WHERE recorded_at >= ? GROUP BY minute_start",
      this.rolledUpThrough
    );
    this.rolledUpThrough = currentMinute;
  }

  // one chart tile covering [from, to). bucketSeconds 0: every reading as [time, count].
  // otherwise one [bucket start, mean, lowest, highest] per bucket that has readings.
  // `first` is the time of the oldest reading, so the page knows where paging back ends
  chartTile(bucketSeconds, from, to) {
    const sql = this.ctx.storage.sql;
    if (this.firstRecordedAt === null) {
      try {
        this.firstRecordedAt = sql.exec("SELECT MIN(recorded_at) AS first FROM history").one().first;
      } catch (error) {
        // database blocked (daily read quota): recent tiles still come from memory below
        console.error(`lunch line first reading lookup failed: ${error}`);
      }
    }
    let points;
    if (bucketSeconds === 0) {
      const rows =
        from >= Date.now() - RECENT_HISTORY_MILLISECONDS
          ? this.history(from)
          : sql.exec("SELECT recorded_at, count FROM history WHERE recorded_at >= ? AND recorded_at < ? ORDER BY recorded_at", from, to).toArray();
      points = rows.filter((row) => row.recorded_at < to).map((row) => [row.recorded_at, row.count]);
    } else {
      if (to > (this.rolledUpThrough ?? 0)) this.rollUpMinutes();
      const bucketMilliseconds = bucketSeconds * 1000; // from the CHART_TILE_SECONDS keys, never user text
      points = sql
        .exec(
          `SELECT (minute_start / ${bucketMilliseconds}) * ${bucketMilliseconds} AS bucket_start, ROUND(SUM(count_sum) / SUM(readings), 2) AS mean, MIN(count_low) AS low, MAX(count_high) AS high ` +
            "FROM history_minutes WHERE minute_start >= ? AND minute_start < ? GROUP BY bucket_start ORDER BY bucket_start",
          from,
          to
        )
        .toArray()
        .map((row) => [row.bucket_start, row.mean, row.low, row.high]);
    }
    return { first: this.firstRecordedAt, points };
  }

  // anonymous: only the time and the text are kept
  addFeedback(message) {
    this.ctx.storage.sql.exec("INSERT INTO feedback (submitted_at, message) VALUES (?, ?)", Date.now(), message);
  }

  feedback() {
    return this.ctx.storage.sql.exec("SELECT submitted_at, message FROM feedback ORDER BY submitted_at DESC").toArray();
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
    // nothing raw is kept or broadcast (Eric Spross, 2026-11-12: no recording of identifiable people).
    // only a camera build that pixelates on the pi marks its `jpeg` with `pixel_block_size`; an older build's raw `jpeg` is dropped
    const { raw_jpeg, ...forViewers } = update;
    if (typeof update.pixel_block_size !== "number") delete forViewers.jpeg;
    this.latestUpdate = JSON.stringify(forViewers);
    for (const viewer of this.ctx.getWebSockets("viewer")) viewer.send(this.latestUpdate);
    if (update.received_at - this.lastRecordedAt < RECORD_INTERVAL_MILLISECONDS) return;
    this.lastRecordedAt = update.received_at;
    try {
      this.ctx.storage.sql.exec(
        "INSERT OR REPLACE INTO history (recorded_at, count, people) VALUES (?, ?, ?)",
        update.received_at,
        update.count,
        JSON.stringify(update.people)
      );
    } catch (error) {
      console.error(`lunch line history row not recorded: ${error}`);
      // keep the 90 minute chart working from memory while the database is blocked
      if (this.recentHistory === null) this.recentHistory = [];
    }
    if (this.recentHistory !== null) {
      const recentSince = update.received_at - RECENT_HISTORY_MILLISECONDS;
      this.recentHistory = this.recentHistory.filter((row) => row.recorded_at >= recentSince);
      this.recentHistory.push({ recorded_at: update.received_at, count: update.count, people: JSON.stringify(update.people) });
    }
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

// posts one feedback submission to #feedback in the socratica discord through a channel webhook.
// runs after the feedback is already stored, so a discord failure only shows in the worker logs
async function notifyDiscord(env, message) {
  if (!env.DISCORD_FEEDBACK_WEBHOOK_URL) throw new Error("DISCORD_FEEDBACK_WEBHOOK_URL secret is not set");
  const response = await fetch(env.DISCORD_FEEDBACK_WEBHOOK_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      // the text is anonymous public input: an embed plus no allowed mentions keeps it from pinging @everyone or anyone else
      embeds: [{ title: "new lunch line monitor feedback", description: message, timestamp: new Date().toISOString(), url: "https://nuevasocratica.com/lunch-line-monitor" }],
      allowed_mentions: { parse: [] },
    }),
  });
  if (!response.ok) throw new Error(`discord webhook answered ${response.status}: ${(await response.text()).slice(0, 300)}`);
}

function lunchLineFor(env) {
  return env.LUNCH_LINE.get(env.LUNCH_LINE.idFromName("nueva-lunch-line"));
}

// GET /lunch-line-monitor/history?hours=24[&format=json]: one row per ~2 s of camera updates
async function historyResponse(url, env) {
  const hours = Number(url.searchParams.get("hours") ?? 24);
  if (!Number.isFinite(hours) || hours <= 0 || hours > MAXIMUM_HISTORY_HOURS) {
    return new Response(`hours must be a finite number greater than 0 and at most ${MAXIMUM_HISTORY_HOURS}`, { status: 400 });
  }
  const rows = await lunchLineFor(env).history(Date.now() - hours * 3600 * 1000);
  if (url.searchParams.get("format") === "json") {
    const body = rows.map((row) => ({
      recorded_at: row.recorded_at,
      pacific_time: pacificTime(row.recorded_at),
      count: row.count,
      people: JSON.parse(row.people),
    }));
    return Response.json(body, { headers: { "cache-control": "no-store" } });
  }
  const lines = ["pacific_time,unix_milliseconds,count"];
  for (const row of rows) lines.push(`${pacificTime(row.recorded_at)},${row.recorded_at},${row.count}`);
  return new Response(lines.join("\n") + "\n", {
    headers: { "content-type": "text/csv; charset=utf-8", "content-disposition": 'inline; filename="lunch-line-history.csv"', "cache-control": "no-store" },
  });
}

// GET /lunch-line-monitor/history/chart?bucket=<seconds>&tile=<n>: tile n of the page's chart, covering
// [n * tile length, (n + 1) * tile length) in unix time. bucket 0 = every reading, else averaged buckets (see chartTile)
async function chartResponse(bucketSeconds, tile, env) {
  const from = tile * CHART_TILE_SECONDS[bucketSeconds] * 1000;
  const to = from + CHART_TILE_SECONDS[bucketSeconds] * 1000;
  const { first, points } = await lunchLineFor(env).chartTile(bucketSeconds, from, to);
  return Response.json({ bucket_seconds: bucketSeconds, from, to, first, points });
}

// serves a history response from the edge cache, or builds it and caches it for cacheSeconds
async function cachedHistoryResponse(cacheUrl, cacheSeconds, ctx, buildResponse) {
  const cacheRequest = new Request(cacheUrl, { method: "GET" });
  const cached = await caches.default.match(cacheRequest);
  if (cached) return cached;
  try {
    const response = await buildResponse();
    if (response.ok) {
      const cacheable = new Response(response.body, response);
      cacheable.headers.set("cache-control", `public, max-age=${cacheSeconds}`);
      ctx.waitUntil(caches.default.put(cacheRequest, cacheable.clone()));
      return cacheable;
    }
    return response;
  } catch (error) {
    if (!/rows.?read|SQLITE_FULL|daily.*limit|free.*limit/i.test(String(error))) throw error;
    const reset = new Date();
    reset.setUTCHours(24, 0, 0, 0);
    return new Response("count history temporarily unavailable: database daily read limit reached; live camera and count remain available", {
      status: 503,
      headers: { "cache-control": "no-store", "retry-after": String(Math.ceil((reset.getTime() - Date.now()) / 1000)) },
    });
  }
}

const API_HEADERS = { "cache-control": "no-store", "access-control-allow-origin": "*" };

function isoTime(seconds) {
  return seconds == null ? null : new Date(seconds * 1000).toISOString();
}

// GET /lunch-line-monitor/api/count (json) and /lunch-line-monitor/api/image.jpg (the pixelated frame), both public like the page.
// 503 when the camera has sent nothing for 5 s
// connects as a viewer and takes the first message: the object sends its latest update on connect, else the next camera update (~1 s).
// uses only the websocket path, so it works even while an older durable object is still running after a deploy
async function latestUpdateJson(env) {
  const request = new Request("https://lunch-line-monitor.internal/live", { headers: { Upgrade: "websocket", [ROLE_HEADER]: "viewer" } });
  const response = await lunchLineFor(env).fetch(request);
  const socket = response.webSocket;
  if (!socket) throw new Error(`durable object answered ${response.status} without a websocket`);
  socket.accept();
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      socket.close(1000, "no update");
      resolve(null);
    }, 5000);
    socket.addEventListener("message", (event) => {
      clearTimeout(timer);
      socket.close(1000, "done");
      resolve(event.data);
    });
  });
}

async function apiResponse(path, env) {
  const latestJson = await latestUpdateJson(env);
  if (latestJson === null) return new Response("no camera update yet", { status: 503, headers: API_HEADERS });
  const update = JSON.parse(latestJson);
  if (path === `${BASE_PATH}/api/image.jpg`) {
    if (typeof update.jpeg !== "string") return new Response("image hidden for privacy", { status: 404, headers: API_HEADERS });
    const bytes = Uint8Array.from(atob(update.jpeg), (character) => character.charCodeAt(0));
    return new Response(bytes, { headers: { ...API_HEADERS, "content-type": "image/jpeg" } });
  }
  const now = Date.now();
  return Response.json(
    {
      count: update.count,
      count_computed_at: isoTime(update.counted_at),
      count_age_seconds: update.counted_at == null ? null : Math.round(now / 1000 - update.counted_at),
      image_captured_at: isoTime(update.captured_at),
      image_age_seconds: Math.round((now - update.received_at) / 1000),
      image_url: `https://nuevasocratica.com${BASE_PATH}/api/image.jpg`,
      pixel_block_size: update.pixel_block_size ?? null,
      frame_width: update.frame_width,
      frame_height: update.frame_height,
      cpu_temperature_celsius: update.cpu_temperature_celsius ?? null,
      note: "computer vision estimate from a crowd density model; it undercounts dense or occluded lines, so read it as a gauge of line length",
    },
    { headers: API_HEADERS }
  );
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "");

    if (path === `${BASE_PATH}/history/chart`) {
      const bucketSeconds = Number(url.searchParams.get("bucket"));
      const tile = Number(url.searchParams.get("tile"));
      const tileMilliseconds = CHART_TILE_SECONDS[bucketSeconds] * 1000;
      if (!(bucketSeconds in CHART_TILE_SECONDS) || !Number.isInteger(tile) || tile < 0 || tile * tileMilliseconds > Date.now()) {
        return new Response(`bucket must be one of ${Object.keys(CHART_TILE_SECONDS).join(", ")} and tile a whole number that has already started`, { status: 400 });
      }
      const cacheUrl = new URL(`https://nuevasocratica.com${BASE_PATH}/history/chart`);
      cacheUrl.searchParams.set("bucket", String(bucketSeconds));
      cacheUrl.searchParams.set("tile", String(tile));
      const cacheSeconds = (tile + 1) * tileMilliseconds <= Date.now() ? PAST_CHART_TILE_CACHE_SECONDS : HISTORY_CACHE_SECONDS;
      return cachedHistoryResponse(cacheUrl, cacheSeconds, ctx, () => chartResponse(bucketSeconds, tile, env));
    }
    if (path === `${BASE_PATH}/history`) {
      const hours = Number(url.searchParams.get("hours") ?? 24);
      if (!Number.isFinite(hours) || hours <= 0 || hours > MAXIMUM_HISTORY_HOURS) return historyResponse(url, env);
      const cacheUrl = new URL(`https://nuevasocratica.com${BASE_PATH}/history`);
      cacheUrl.searchParams.set("hours", String(hours));
      if (url.searchParams.get("format") === "json") cacheUrl.searchParams.set("format", "json");
      return cachedHistoryResponse(cacheUrl, HISTORY_CACHE_SECONDS, ctx, () => historyResponse(url, env));
    }
    if (path === `${BASE_PATH}/api/count` || path === `${BASE_PATH}/api/image.jpg`) return apiResponse(path, env);

    // POST {"message": "..."} from the page's feedback box. GET lists it all, newest first, with the bearer token
    if (path === `${BASE_PATH}/feedback`) {
      if (request.method === "POST") {
        const message = String((await request.json()).message ?? "").trim();
        if (message === "" || message.length > MAXIMUM_FEEDBACK_CHARACTERS) {
          return new Response(`feedback must be 1 to ${MAXIMUM_FEEDBACK_CHARACTERS} characters`, { status: 400 });
        }
        await lunchLineFor(env).addFeedback(message);
        ctx.waitUntil(notifyDiscord(env, message));
        return new Response(null, { status: 204 });
      }
      if (!(await bearerTokenMatches(request.headers.get("Authorization"), env.INGEST_TOKEN))) return new Response("unauthorized", { status: 401 });
      const rows = await lunchLineFor(env).feedback();
      return Response.json(rows.map((row) => ({ pacific_time: pacificTime(row.submitted_at), message: row.message })), { headers: { "cache-control": "no-store" } });
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
