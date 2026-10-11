const APP_ORIGIN = "https://frankholck.github.io";
const WORKFLOW_URL = "https://api.github.com/repos/frankholck/vtaper/actions/workflows/garmin-sync.yml/dispatches";
const COOLDOWN_SECONDS = 120;

// The Garmin In Focus email is started from here on time, by the cron triggers
// in wrangler.jsonc, because GitHub's own timers start hours late or not at all.
const IN_FOCUS_URL = "https://api.github.com/repos/frankholck/vtaper/actions/workflows/garmin-in-focus.yml/dispatches";
// Send times as minutes after midnight GMT (Dubai is GMT+4 all year).
const IN_FOCUS_SEND_TIMES = { "07:30": 3 * 60 + 30, "14:00": 10 * 60, "22:30": 18 * 60 + 30 };

// The send time that is due within ten minutes of `date`, or null.
function inFocusSlot(date) {
  const minute = date.getUTCHours() * 60 + date.getUTCMinutes();
  for (const [slot, goal] of Object.entries(IN_FOCUS_SEND_TIMES)) {
    if (goal - minute >= 0 && goal - minute <= 10) return slot;
  }
  return null;
}

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": APP_ORIGIN,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

function json(body, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      ...corsHeaders(),
      ...extraHeaders,
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin");

    if (request.method === "OPTIONS") {
      if (origin !== APP_ORIGIN) return new Response(null, { status: 403 });
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    if (url.pathname !== "/sync" || request.method !== "POST") {
      return json({ ok: false, message: "Not found" }, 404);
    }

    if (origin !== APP_ORIGIN) {
      return new Response(JSON.stringify({ ok: false, message: "Forbidden" }), {
        status: 403,
        headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
      });
    }

    if (!env.GITHUB_TOKEN) {
      return json({ ok: false, message: "Garmin collection is not configured yet." }, 503);
    }

    const cooldownKey = new Request("https://vtaper.internal/garmin-sync-cooldown");
    const cooldown = await caches.default.match(cooldownKey);
    if (cooldown) {
      return json(
        { ok: false, message: "Garmin collection was just started. Please wait two minutes." },
        429,
        { "Retry-After": String(COOLDOWN_SECONDS) },
      );
    }

    const response = await fetch(WORKFLOW_URL, {
      method: "POST",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "vtaper-garmin-relay",
      },
      body: JSON.stringify({ ref: "main" }),
    });

    if (!response.ok) {
      console.error("GitHub workflow dispatch failed", response.status, await response.text());
      return json({ ok: false, message: "GitHub could not start Garmin collection." }, 502);
    }

    await caches.default.put(
      cooldownKey,
      new Response("started", { headers: { "Cache-Control": `s-maxage=${COOLDOWN_SECONDS}` } }),
    );

    return json({ ok: true, message: "Garmin collection started." }, 202);
  },

  // Runs on the cron triggers, two minutes before each send time. The
  // workflow then waits for the send time itself and sends on the minute.
  async scheduled(controller, env) {
    const slot = inFocusSlot(new Date(controller.scheduledTime));
    if (!slot) {
      console.error("In Focus: no send time is due at this trigger time");
      return;
    }
    if (!env.GITHUB_TOKEN) {
      throw new Error("In Focus: GITHUB_TOKEN is not configured");
    }

    let status = 0;
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      const response = await fetch(IN_FOCUS_URL, {
        method: "POST",
        headers: {
          "Accept": "application/vnd.github+json",
          "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "vtaper-garmin-relay",
        },
        body: JSON.stringify({ ref: "main", inputs: { slot } }),
      });
      status = response.status;
      if (response.ok) {
        console.log(`In Focus: started the ${slot} email`);
        return;
      }
      console.error("In Focus: GitHub did not start the email", status, await response.text());
      // A wrong token or request does not get better by trying again.
      if (status < 500 && status !== 429) break;
      await new Promise((resolve) => setTimeout(resolve, 4000));
    }
    throw new Error(`In Focus: could not start the ${slot} email (HTTP ${status})`);
  },
};
