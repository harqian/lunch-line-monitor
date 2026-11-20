import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = (await readFile(new URL("../src/index.js", import.meta.url), "utf8"))
  .replace('import { DurableObject } from "cloudflare:workers";', "class DurableObject { constructor(ctx) { this.ctx = ctx; } }")
  .replace('import page from "./page.html";', 'const page = "";');
const { default: worker, LunchLine } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

function fixture(rows) {
  const queries = [];
  const sql = {
    exec(query, since, until, limit) {
      if (query.startsWith("CREATE")) return;
      queries.push({ since, until, limit, query });
      return { toArray: () => rows.filter((row) => row.recorded_at >= since && row.recorded_at <= until).slice(0, limit) };
    },
  };
  const object = new LunchLine({ storage: { sql } });
  const env = { LUNCH_LINE: {
    idFromName: (name) => name,
    get: () => object,
  } };
  return {
    queries,
    request: (search = "") => worker.fetch(new Request(`https://example.com/lunch-line-monitor/history${search}`), env),
  };
}

test("CSV and JSON preserve every row across batch boundaries", async () => {
  const now = Date.now();
  for (const size of [0, 1, 999, 1000, 1001, 2000, 2001]) {
    const rows = Array.from({ length: size }, (_, i) => ({
      recorded_at: now - (size - i) * 2000,
      count: i % 32,
      people: JSON.stringify([{ box: [1, 2, 3, 4], confidence: 0.8 }]),
    }));
    const f = fixture(rows);
    const csv = await f.request("?hours=168");
    assert.equal(csv.status, 200);
    assert.equal(csv.headers.get("cache-control"), "no-store");
    assert.match(csv.headers.get("content-disposition"), /lunch-line-history.csv/);
    const lines = (await csv.text()).trimEnd().split("\n");
    assert.equal(lines.shift(), "pacific_time,unix_milliseconds,count");
    assert.equal(lines.length, size);
    lines.forEach((line, i) => {
      const time = new Date(rows[i].recorded_at).toLocaleString("sv-SE", { timeZone: "America/Los_Angeles" });
      assert.equal(line, `${time},${rows[i].recorded_at},${rows[i].count}`);
    });
    const json = await f.request("?hours=168&format=json");
    assert.match(json.headers.get("content-type"), /application\/json/);
    assert.deepEqual(await json.json(), rows.map((row) => ({
      ...row,
      pacific_time: new Date(row.recorded_at).toLocaleString("sv-SE", { timeZone: "America/Los_Angeles" }),
      people: JSON.parse(row.people),
    })));
    assert.ok(f.queries.every(({ limit }) => limit === 1000));
    assert.ok(f.queries.filter(({ query }) => !query.includes("people")).length > 0);
  }
});

test("default and trend windows exclude older readings and concurrent new readings", async () => {
  const now = Date.now();
  const rows = [
    { recorded_at: now - 25 * 3600000, count: 1, people: "[]" },
    { recorded_at: now - 2 * 3600000, count: 2, people: "[]" },
    { recorded_at: now - 1000, count: 3, people: "[]" },
  ];
  const f = fixture(rows);
  const response = await f.request("?format=json");
  rows.push({ recorded_at: now + 60000, count: 4, people: "[]" });
  assert.deepEqual((await response.json()).map((row) => row.count), [2, 3]);
  assert.deepEqual((await (await f.request("?hours=1.5&format=json")).json()).map((row) => row.count), [3]);
});

test("invalid hours return 400 without querying history", async () => {
  for (const hours of ["0", "-1", "nope", "Infinity", ""]) {
    const f = fixture([]);
    assert.equal((await f.request(`?hours=${hours}`)).status, 400);
    assert.equal(f.queries.length, 0);
  }
});

test("Pacific timestamps retain standard time, daylight time, and midnight formatting", async () => {
  const rows = ["2026-01-01T08:00:00Z", "2026-07-01T07:00:00Z"].map((time) => ({
    recorded_at: Date.parse(time), count: 0, people: "[]",
  }));
  const f = fixture(rows);
  const response = await f.request("?hours=100000&format=json");
  assert.deepEqual((await response.json()).map((row) => row.pacific_time), ["2026-01-01 00:00:00", "2026-07-01 00:00:00"]);
});
