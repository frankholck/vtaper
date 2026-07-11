const APP_ORIGIN = "https://frankholck.github.io";
const WORKFLOW_URL = "https://api.github.com/repos/frankholck/vtaper/actions/workflows/garmin-sync.yml/dispatches";
const COOLDOWN_SECONDS = 120;

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
};
