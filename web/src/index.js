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
// leave three million account reads for recording, storage overhead and other applications.
// reserve before reading; a restart forfeits the unused reservation instead of resetting the allowance
const HISTORY_DAILY_READ_BUDGET = 2000000;
const HISTORY_READ_RESERVATION = 10000;
const MAXIMUM_RAW_HISTORY_ROWS = 350000;
const EXPORT_CHUNK_MILLISECONDS = 3600000;
const MAXIMUM_EXPORT_CHUNK_ROWS = 10000;
// leave room below sqlite's 2 MB row limit for keys and row metadata
const MAXIMUM_EXPORT_PART_BYTES = 1024 * 1024;

function packHistoryRows(rows) {
  if (!rows.length) return [];
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();
  const bytes = encoder.encode(JSON.stringify(rows.map((row) => [row.recorded_at, row.count, row.people])));
  const payloads = [];
  // a streaming decoder preserves unicode characters split across byte boundaries
  const partBytes = MAXIMUM_EXPORT_PART_BYTES - 4;
  for (let offset = 0; offset < bytes.length; offset += partBytes) {
    payloads.push(decoder.decode(bytes.subarray(offset, offset + partBytes), { stream: offset + partBytes < bytes.length }));
  }
  return payloads;
}

function historyLimitReached(error) {
  return /rows.?read|SQLITE_FULL|daily.*limit|free.*limit|history read budget/i.test(String(error));
}

// one durable object fans camera updates out to every open browser over hibernatable websockets
export class LunchLine extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    // memory only: lost if the object hibernates, which only happens after the camera stops sending
    this.latestUpdate = null;
    this.lastRecordedAt = 0;
    this.recentHistory = null;
    this.recentHistoryUnavailable = false;
    this.recentHistoryRetryAt = 0;
    this.firstRecordedAt = null;
    this.rolledUpThrough = null;
    this.minuteHistoryReady = false;
    this.readBudget = null;
    this.readBudgetRemaining = 0;
    this.historyReadRetryAfter = 0;
    this.historyRowsRead = 0;
    this.exportChunksReady = false;
    this.historyBudgetReconciliation = null;
    this.chartTiles = new Map();
    // the free plan's daily rows-read quota makes every sql call throw, even CREATE TABLE IF NOT EXISTS.
    // a throw here would kill the object and with it the live feed (11/20), so storage failures only stop recording
    try {
      this.#reconcileHistoryBudget(env);
      const sql = this.ctx.storage.sql;
      sql.exec("CREATE TABLE IF NOT EXISTS history (recorded_at INTEGER PRIMARY KEY, count INTEGER NOT NULL, people TEXT NOT NULL)");
      sql.exec("CREATE TABLE IF NOT EXISTS feedback (submitted_at INTEGER NOT NULL, message TEXT NOT NULL)");
      // one summary row per minute of readings, so the 6 h to 7 d charts read 1/30 of the rows (see rollUpMinutes)
      this.ensureMinuteHistory();
    } catch (error) {
      console.error(`lunch line storage unavailable at startup: ${error}`);
    }
  }

  #reconcileHistoryBudget(env) {
    if (!env.HISTORY_READ_BUDGET_RECONCILIATION) return;
    // operator-supplied account-wide read upper bound, including a documented analytics-lag allowance.
    // this is a dated, one-time repair for unused reservations forfeited by the former large exports
    const baseline = JSON.parse(env.HISTORY_READ_BUDGET_RECONCILIATION);
    const day = Math.floor(Date.now() / 86400000);
    if (!Number.isSafeInteger(baseline.observedAt) || baseline.observedAt <= 0) throw new Error("invalid history read reconciliation timestamp");
    if (baseline.day !== day || Date.now() < baseline.observedAt || Date.now() - baseline.observedAt > 1800000) return;
    if (!Number.isSafeInteger(baseline.accountReadsUpperBound) || baseline.accountReadsUpperBound <= 0 || baseline.accountReadsUpperBound >= HISTORY_DAILY_READ_BUDGET) {
      throw new Error("invalid history read reconciliation upper bound");
    }
    const key = "history-read-budget-reconciliation-v1";
    this.historyBudgetReconciliation = this.ctx.storage.kv.get(key);
    if (this.historyBudgetReconciliation?.day === day) return;
    const saved = this.ctx.storage.kv.get("history-read-budget");
    if (saved?.day !== day || saved.reserved <= baseline.accountReadsUpperBound) return;
    this.ctx.storage.transactionSync(() => {
      this.ctx.storage.kv.put("history-read-budget", { day, reserved: baseline.accountReadsUpperBound });
      this.historyBudgetReconciliation = { ...baseline, previousReserved: saved.reserved };
      this.ctx.storage.kv.put(key, this.historyBudgetReconciliation);
    });
  }

  reserveHistoryReads(maximumRowsRead) {
    const day = Math.floor(Date.now() / 86400000);
    if (Date.now() < this.historyReadRetryAfter) throw new Error("history read budget or storage unavailable");
    if (this.readBudget?.day !== day) {
      const saved = this.ctx.storage.kv.get("history-read-budget");
      this.readBudget = saved?.day === day ? saved : { day, reserved: 0 };
      this.readBudgetRemaining = 0;
    }
    if (this.readBudgetRemaining < maximumRowsRead) {
      const reservation = Math.max(HISTORY_READ_RESERVATION, maximumRowsRead - this.readBudgetRemaining);
      if (this.readBudget.reserved + reservation > HISTORY_DAILY_READ_BUDGET) {
        throw new Error("history read budget reached");
      }
      const next = { day, reserved: this.readBudget.reserved + reservation };
      this.ctx.storage.kv.put("history-read-budget", next);
      this.readBudget = next;
      this.readBudgetRemaining += reservation;
    }
  }

  historySql(maximumRowsRead, statement, ...bindings) {
    const day = Math.floor(Date.now() / 86400000);
    try {
      this.reserveHistoryReads(maximumRowsRead);
      const cursor = this.ctx.storage.sql.exec(statement, ...bindings);
      const rows = cursor.toArray();
      this.historyRowsRead += cursor.rowsRead;
      this.readBudgetRemaining -= cursor.rowsRead;
      if (cursor.rowsRead > maximumRowsRead) {
        // an unexpected query plan must stop further reads, never silently outrun its reservation
        this.historyReadRetryAfter = (day + 1) * 86400000;
        throw new Error(`history read budget estimate exceeded: ${cursor.rowsRead} reads, maximum ${maximumRowsRead}`);
      }
      return rows;
    } catch (error) {
      if (historyLimitReached(error)) this.historyReadRetryAfter = (day + 1) * 86400000;
      throw error;
    }
  }

  history(sinceMilliseconds) {
    const recentSince = Date.now() - RECENT_HISTORY_MILLISECONDS;
    if (sinceMilliseconds >= recentSince) {
      if (this.recentHistory === null || (this.recentHistoryUnavailable && Date.now() >= this.recentHistoryRetryAt)) {
        try {
          const saved = this.queryHistory(recentSince);
          // recovery must retain readings that only reached memory while recording was blocked
          const byTime = new Map((this.recentHistory ?? []).concat(saved).map((row) => [row.recorded_at, row]));
          this.recentHistory = [...byTime.values()].sort((left, right) => left.recorded_at - right.recorded_at);
          this.recentHistoryUnavailable = false;
        } catch (error) {
          // database blocked (daily read quota): the chart runs from memory, filled by live updates since the object started
          console.error(`lunch line history read failed, serving memory only: ${error}`);
          if (!historyLimitReached(error)) throw error;
          if (this.recentHistory === null) this.recentHistory = [];
          this.recentHistoryUnavailable = true;
          this.recentHistoryRetryAt = Date.now() + 60000;
        }
      }
      this.recentHistory = this.recentHistory.filter((row) => row.recorded_at >= recentSince);
      return this.recentHistory.filter((row) => row.recorded_at >= sinceMilliseconds);
    }
    return this.queryHistory(sinceMilliseconds);
  }

  queryHistory(sinceMilliseconds) {
    const limit = Date.now() - sinceMilliseconds <= RECENT_HISTORY_MILLISECONDS ? 10000 : MAXIMUM_RAW_HISTORY_ROWS;
    return this.historySql(limit * 2 + 32,
      "SELECT recorded_at, count, people FROM history WHERE recorded_at >= ? ORDER BY recorded_at LIMIT ?",
      sinceMilliseconds, limit);
  }

  exportHistory(sinceMilliseconds) {
    const before = this.historyRowsRead;
    const diagnostics = { chunksBuilt: 0, chunksReused: 0, rowsRead: 0, previousReserved: this.historyBudgetReconciliation?.previousReserved };
    if (sinceMilliseconds >= Date.now() - RECENT_HISTORY_MILLISECONDS) {
      const rows = this.history(sinceMilliseconds);
      diagnostics.rowsRead = this.historyRowsRead - before;
      return { rows, partial: this.recentHistoryUnavailable, diagnostics };
    }
    const currentHour = Math.floor(Date.now() / EXPORT_CHUNK_MILLISECONDS) * EXPORT_CHUNK_MILLISECONDS;
    // the unfinished hour shares the recent cache, which camera messages extend without another query
    const tail = this.history(currentHour);
    if (this.recentHistoryUnavailable) throw new Error("history read budget or storage unavailable");
    this.ensureExportChunks();
    const rows = [];
    for (let from = Math.floor(sinceMilliseconds / EXPORT_CHUNK_MILLISECONDS) * EXPORT_CHUNK_MILLISECONDS;
      from < currentHour; from += EXPORT_CHUNK_MILLISECONDS) {
      const chunk = this.exportChunk(from);
      diagnostics[chunk.reused ? "chunksReused" : "chunksBuilt"] += 1;
      for (const row of chunk.rows) if (row.recorded_at >= sinceMilliseconds) rows.push(row);
      if (rows.length > MAXIMUM_RAW_HISTORY_ROWS) throw new Error("history export exceeds bounded row count");
    }
    rows.push(...tail);
    if (rows.length > MAXIMUM_RAW_HISTORY_ROWS) throw new Error("history export exceeds bounded row count");
    diagnostics.rowsRead = this.historyRowsRead - before;
    return { rows, partial: false, diagnostics };
  }

  ensureExportChunks() {
    if (this.exportChunksReady) return;
    if (Date.now() < this.historyReadRetryAfter) throw new Error("history read budget or storage unavailable");
    this.ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS history_export_chunks_v1 (chunk_start INTEGER NOT NULL, part INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (chunk_start, part))");
    this.exportChunksReady = true;
  }

  exportChunk(from) {
    const to = from + EXPORT_CHUNK_MILLISECONDS;
    if (to > Date.now()) throw new Error("cannot seal an unfinished history hour");
    const cached = this.historySql(20034,
      "SELECT part, payload FROM history_export_chunks_v1 WHERE chunk_start = ? ORDER BY part LIMIT ?",
      from, MAXIMUM_EXPORT_CHUNK_ROWS + 1);
    if (cached.length) {
      const header = JSON.parse(cached[0].payload);
      if (cached[0].part !== 0 || header.to !== to || header.parts !== cached.length - 1) {
        throw new Error("incomplete persistent history chunk");
      }
      for (let index = 1; index < cached.length; index += 1) {
        if (cached[index].part !== index) throw new Error("missing persistent history chunk part");
      }
      const tuples = header.parts ? JSON.parse(cached.slice(1).map((part) => part.payload).join("")) : [];
      const rows = tuples.map(([recorded_at, count, people]) => ({ recorded_at, count, people }));
      if (rows.length !== header.rows) throw new Error("persistent history chunk row count mismatch");
      return { rows, reused: true };
    }
    const rows = this.historySql(20032,
      "SELECT recorded_at, count, people FROM history WHERE recorded_at >= ? AND recorded_at < ? ORDER BY recorded_at LIMIT ?",
      from, to, MAXIMUM_EXPORT_CHUNK_ROWS);
    if (rows.length === MAXIMUM_EXPORT_CHUNK_ROWS) throw new Error("history hour exceeds bounded export size");
    const payloads = packHistoryRows(rows);
    // reserve outside the transaction so a rollback cannot undo the durable read reservation
    this.reserveHistoryReads(32 * (payloads.length + 1));
    this.ctx.storage.transactionSync(() => {
      const header = JSON.stringify({ to, rows: rows.length, parts: payloads.length });
      for (const [part, payload] of [header, ...payloads].entries()) {
        this.historySql(32, "INSERT INTO history_export_chunks_v1 (chunk_start, part, payload) VALUES (?, ?, ?)", from, part, payload);
      }
    });
    return { rows, reused: false };
  }

  ensureMinuteHistory() {
    if (this.minuteHistoryReady) return;
    if (Date.now() < this.historyReadRetryAfter) throw new Error("history read budget or storage unavailable");
    this.ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS history_minutes (minute_start INTEGER PRIMARY KEY, readings INTEGER NOT NULL, count_sum REAL NOT NULL, count_low REAL NOT NULL, count_high REAL NOT NULL, recorded_through INTEGER NOT NULL)");
    this.minuteHistoryReady = true;
  }

  rollUpMinutes() {
    this.ensureMinuteHistory();
    if (this.rolledUpThrough === null) {
      const latest = this.historySql(32, "SELECT MAX(minute_start) AS latest FROM history_minutes")[0].latest;
      const first = latest ?? this.historySql(32, "SELECT MIN(recorded_at) AS first FROM history")[0].first;
      if (first === null) return;
      this.rolledUpThrough = Math.floor(first / 60000) * 60000;
    }
    const currentMinute = Math.floor(Date.now() / 60000) * 60000;
    // backfill one bounded hour at a time, retaining progress if today's read allowance runs out
    while (this.rolledUpThrough <= currentMinute) {
      const end = Math.min(this.rolledUpThrough + 3600000, currentMinute + 60000);
      const rows = this.historySql(20032,
        "SELECT recorded_at, count FROM history WHERE recorded_at >= ? AND recorded_at < ? ORDER BY recorded_at LIMIT 10000",
        this.rolledUpThrough, end);
      if (rows.length === 10000) throw new Error("history hour exceeds bounded rollup size");
      const minutes = new Map();
      for (const row of rows) {
        const start = Math.floor(row.recorded_at / 60000) * 60000;
        let summary = minutes.get(start);
        if (!summary) minutes.set(start, (summary = { readings: 0, sum: 0, low: row.count, high: row.count }));
        summary.readings += 1;
        summary.sum += row.count;
        summary.low = Math.min(summary.low, row.count);
        summary.high = Math.max(summary.high, row.count);
        summary.through = row.recorded_at;
      }
      for (const [start, summary] of minutes) {
        this.historySql(32,
          "INSERT OR REPLACE INTO history_minutes (minute_start, readings, count_sum, count_low, count_high, recorded_through) VALUES (?, ?, ?, ?, ?, ?)",
          start, summary.readings, summary.sum, summary.low, summary.high, summary.through);
      }
      this.rolledUpThrough = Math.min(end, currentMinute);
      if (end > currentMinute) break;
    }
  }

  // one chart tile covering [from, to). bucketSeconds 0: every reading as [time, count].
  // otherwise [bucket start, mean, lowest, highest, readings, sum, last reading time] per bucket.
  // `first` is the time of the oldest reading, so the page knows where paging back ends
  chartTile(bucketSeconds, from, to) {
    const key = `${bucketSeconds}:${from}`;
    const cached = this.chartTiles.get(key);
    if (cached && cached.expires > Date.now()) return cached.body;
    if (this.firstRecordedAt === null) {
      try {
        this.firstRecordedAt = this.historySql(32, "SELECT MIN(recorded_at) AS first FROM history")[0].first;
      } catch (error) {
        // database blocked (daily read quota): recent tiles still come from memory below
        console.error(`lunch line first reading lookup failed: ${error}`);
      }
    }
    let points;
    let partial = false;
    if (bucketSeconds === 0) {
      const recentSince = Date.now() - RECENT_HISTORY_MILLISECONDS;
      let rows;
      if (to > recentSince) {
        rows = this.history(Math.max(from, recentSince));
        partial = this.recentHistoryUnavailable;
        if (from < recentSince) {
          try {
            const older = this.historySql(20032, "SELECT recorded_at, count FROM history WHERE recorded_at >= ? AND recorded_at < ? ORDER BY recorded_at LIMIT 10000", from, recentSince);
            rows = older.concat(rows);
          } catch (error) {
            if (!historyLimitReached(error)) throw error;
            partial = true;
          }
        }
      } else {
        rows = this.historySql(20032, "SELECT recorded_at, count FROM history WHERE recorded_at >= ? AND recorded_at < ? ORDER BY recorded_at LIMIT 10000", from, to);
      }
      points = rows.filter((row) => row.recorded_at < to).map((row) => [row.recorded_at, row.count]);
    } else {
      if (to > (this.rolledUpThrough ?? 0)) this.rollUpMinutes();
      const bucketMilliseconds = bucketSeconds * 1000; // from the CHART_TILE_SECONDS keys, never user text
      points = this.historySql(6000,
          `SELECT (minute_start / ${bucketMilliseconds}) * ${bucketMilliseconds} AS bucket_start, ROUND(SUM(count_sum) / SUM(readings), 2) AS mean, MIN(count_low) AS low, MAX(count_high) AS high, SUM(readings) AS readings, SUM(count_sum) AS count_sum, MAX(recorded_through) AS recorded_through ` +
            "FROM (SELECT * FROM history_minutes WHERE minute_start >= ? AND minute_start < ? ORDER BY minute_start LIMIT 1440) GROUP BY bucket_start ORDER BY bucket_start",
          from,
          to
        )
        .map((row) => [row.bucket_start, row.mean, row.low, row.high, row.readings, row.count_sum, row.recorded_through]);
    }
    const complete = to <= Date.now() && !partial;
    const body = { first: this.firstRecordedAt, points, partial, complete };
    if (!partial) {
      if (this.chartTiles.size >= 64) this.chartTiles.delete(this.chartTiles.keys().next().value);
      const expires = complete ? Date.now() + PAST_CHART_TILE_CACHE_SECONDS * 1000 : Math.min(to, Date.now() + HISTORY_CACHE_SECONDS * 1000);
      this.chartTiles.set(key, { body, expires });
    }
    return body;
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
      this.recentHistoryUnavailable = true;
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

const pacificHourPrefixes = new Map();

function pacificTime(milliseconds) {
  const hourStart = Math.floor(milliseconds / 3600000) * 3600000;
  let prefix = pacificHourPrefixes.get(hourStart);
  if (prefix === undefined) {
    prefix = new Date(hourStart).toLocaleString("sv-SE", { timeZone: "America/Los_Angeles" }).slice(0, 13);
    if (pacificHourPrefixes.size >= 192) pacificHourPrefixes.delete(pacificHourPrefixes.keys().next().value);
    pacificHourPrefixes.set(hourStart, prefix);
  }
  // pacific daylight-saving transitions fall on coordinated universal time (UTC) hour boundaries; minutes and seconds remain unchanged
  const secondsInHour = Math.floor((milliseconds - hourStart) / 1000);
  return `${prefix}:${String(Math.floor(secondsInHour / 60)).padStart(2, "0")}:${String(secondsInHour % 60).padStart(2, "0")}`;
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
  const { rows, partial, diagnostics } = await lunchLineFor(env).exportHistory(Date.now() - hours * 3600 * 1000);
  if (partial) throw new Error("history read budget or storage unavailable");
  const headers = { "cache-control": "no-store", "x-history-time-format": "hour-prefix-v1" };
  // response counters let deployment checks distinguish a warm edge response from actual persistent reuse
  if (diagnostics) {
    headers["x-history-storage"] = "persistent-chunks-v1";
    headers["x-history-rows-read"] = String(diagnostics.rowsRead);
    headers["x-history-chunks-built"] = String(diagnostics.chunksBuilt);
    headers["x-history-chunks-reused"] = String(diagnostics.chunksReused);
    if (diagnostics.previousReserved !== undefined) headers["x-history-reconciled-from"] = String(diagnostics.previousReserved);
  }
  if (url.searchParams.get("format") === "json") {
    const body = rows.map((row) => ({
      recorded_at: row.recorded_at,
      pacific_time: pacificTime(row.recorded_at),
      count: row.count,
      people: JSON.parse(row.people),
    }));
    return Response.json(body, { headers });
  }
  const lines = ["pacific_time,unix_milliseconds,count"];
  for (const row of rows) lines.push(`${pacificTime(row.recorded_at)},${row.recorded_at},${row.count}`);
  return new Response(lines.join("\n") + "\n", {
    headers: { ...headers, "content-type": "text/csv; charset=utf-8", "content-disposition": 'inline; filename="lunch-line-history.csv"' },
  });
}

// GET /lunch-line-monitor/history/chart?bucket=<seconds>&tile=<n>: tile n of the page's chart, covering
// [n * tile length, (n + 1) * tile length) in unix time. bucket 0 = every reading, else averaged buckets (see chartTile)
async function chartResponse(bucketSeconds, tile, env) {
  const from = tile * CHART_TILE_SECONDS[bucketSeconds] * 1000;
  const to = from + CHART_TILE_SECONDS[bucketSeconds] * 1000;
  const { first, points, partial, complete } = await lunchLineFor(env).chartTile(bucketSeconds, from, to);
  const seconds = complete ? PAST_CHART_TILE_CACHE_SECONDS : Math.min(HISTORY_CACHE_SECONDS, Math.floor((to - Date.now()) / 1000));
  return Response.json({ bucket_seconds: bucketSeconds, from, to, first, points, partial, complete }, {
    headers: { "cache-control": partial || seconds <= 0 ? "no-store" : `public, max-age=${seconds}` },
  });
}

// serves a history response from the edge cache, or builds it and caches it for cacheSeconds
async function cachedHistoryResponse(cacheUrl, cacheSeconds, ctx, buildResponse) {
  const cacheRequest = new Request(cacheUrl, { method: "GET" });
  const cached = await caches.default.match(cacheRequest);
  if (cached) return cached;
  try {
    const response = await buildResponse();
    if (response.ok && response.headers.get("cache-control") !== "no-store") {
      const cacheable = new Response(response.body, response);
      cacheable.headers.set("cache-control", response.headers.get("cache-control") ?? `public, max-age=${cacheSeconds}`);
      ctx.waitUntil(caches.default.put(cacheRequest, cacheable.clone()));
      return cacheable;
    }
    return response;
  } catch (error) {
    if (!historyLimitReached(error)) throw error;
    console.error(`history request paused: ${error}`);
    const reset = (Math.floor(Date.now() / 86400000) + 1) * 86400000;
    return new Response("count history temporarily paused to protect the daily read allowance; live camera and count remain available", {
      status: 503,
      headers: { "cache-control": "no-store", "retry-after": String(Math.ceil((reset - Date.now()) / 1000)), "x-history-pause-reason": String(error).includes("estimate exceeded") ? "query-read-estimate" : "daily-read-budget" },
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
      return cachedHistoryResponse(cacheUrl, HISTORY_CACHE_SECONDS, ctx, async () => {
        const response = await historyResponse(url, env);
        response.headers.delete("cache-control");
        return response;
      });
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
