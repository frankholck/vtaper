// Tests for the timed start of the Garmin In Focus email. No network:
//   node --test garmin-relay/test/scheduled.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

const { default: worker } = await import("../src/index.js");

const at = (text) => new Date(`2026-10-11T${text}Z`).getTime();

function fakeGitHub(statuses) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    const status = statuses[Math.min(calls.length - 1, statuses.length - 1)];
    return new Response(status === 204 ? null : "nope", { status });
  };
  return calls;
}

async function slotStartedAt(time) {
  const calls = fakeGitHub([204]);
  await worker.scheduled({ scheduledTime: at(time) }, { GITHUB_TOKEN: "t" });
  return calls.length ? JSON.parse(calls[0].init.body).inputs.slot : null;
}

test("each cron trigger maps to its send time", async () => {
  assert.equal(await slotStartedAt("03:28:00"), "07:30");
  assert.equal(await slotStartedAt("09:58:00"), "14:00");
  assert.equal(await slotStartedAt("18:28:00"), "22:30");
  assert.equal(await slotStartedAt("03:30:00"), "07:30");
  for (const other of ["03:31:00", "03:19:00", "12:00:00", "00:00:00", "18:45:00"]) {
    assert.equal(await slotStartedAt(other), null, other);
  }
});

test("a trigger starts the workflow for that send time", async () => {
  const calls = fakeGitHub([204]);
  await worker.scheduled({ scheduledTime: at("09:58:00"), cron: "58 9 * * *" }, { GITHUB_TOKEN: "t0ken" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://api.github.com/repos/frankholck/vtaper/actions/workflows/garmin-in-focus.yml/dispatches");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.Authorization, "Bearer t0ken");
  assert.deepEqual(JSON.parse(calls[0].init.body), { ref: "main", inputs: { slot: "14:00" } });
});

test("a passing GitHub error is tried again, a refused request is not", async () => {
  const realTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (fn) => realTimeout(fn, 0);
  try {
    let calls = fakeGitHub([502, 204]);
    await worker.scheduled({ scheduledTime: at("18:28:00") }, { GITHUB_TOKEN: "t" });
    assert.equal(calls.length, 2);

    calls = fakeGitHub([401]);
    await assert.rejects(worker.scheduled({ scheduledTime: at("18:28:00") }, { GITHUB_TOKEN: "t" }), /HTTP 401/);
    assert.equal(calls.length, 1);

    calls = fakeGitHub([503]);
    await assert.rejects(worker.scheduled({ scheduledTime: at("18:28:00") }, { GITHUB_TOKEN: "t" }), /HTTP 503/);
    assert.equal(calls.length, 3);
  } finally {
    globalThis.setTimeout = realTimeout;
  }
});

test("nothing is started without a token or outside the send times", async () => {
  let calls = fakeGitHub([204]);
  await assert.rejects(worker.scheduled({ scheduledTime: at("03:28:00") }, {}), /GITHUB_TOKEN/);
  await worker.scheduled({ scheduledTime: at("12:00:00") }, { GITHUB_TOKEN: "t" });
  assert.equal(calls.length, 0);
});

test("the app's Collect Garmin now request still works as before", async () => {
  const calls = fakeGitHub([204]);
  globalThis.caches = { default: { match: async () => undefined, put: async () => undefined } };
  const request = new Request("https://relay.example/sync", { method: "POST", headers: { Origin: "https://frankholck.github.io" } });
  const response = await worker.fetch(request, { GITHUB_TOKEN: "t" });
  assert.equal(response.status, 202);
  assert.match(calls[0].url, /garmin-sync\.yml\/dispatches$/);
  assert.deepEqual(JSON.parse(calls[0].init.body), { ref: "main" });
  const stranger = new Request("https://relay.example/sync", { method: "POST", headers: { Origin: "https://example.com" } });
  assert.equal((await worker.fetch(stranger, { GITHUB_TOKEN: "t" })).status, 403);
});
