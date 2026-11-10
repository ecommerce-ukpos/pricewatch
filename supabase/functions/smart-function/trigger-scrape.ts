import "jsr:@supabase/functions-js/edge-runtime.d.ts";

// trigger-scrape (slug: smart-function)
// Dispatches and reports GitHub Actions runs of .github/workflows/scraper.yml.
//
//  POST with no body / {}            → one full price scrape of all confirmed matches
//                                      (the Match Manager "Send all for rescrape" button).
//  POST {mode:"check", skus:[...],   → SKU-list price check from the "SKU list check" screen:
//        competitor_ids:[...]}          one "check" run per chosen competitor. Each run does
//                                       discovery for that competitor against these SKUs only,
//                                       THEN rescrapes their confirmed matches at that competitor
//                                       (one run per competitor keeps each inside GitHub's 6h limit).
//  POST {mode:"status", since:ISO}   → manually started scraper.yml runs created since `since`
//                                      (queued / in_progress / completed + conclusion), for the
//                                      "Run progress" panel. Run names come from run-name in scraper.yml.
//  Discovery never runs with force, so confirmed matches are never reset to review.
//
//  Secrets: GITHUB_PAT (fine-grained: Actions read+write on the repo), GITHUB_REPO ("owner/repo").

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
};
const MAX_SKUS = 300;
const MAX_COMPETITORS = 40;
const SKU_RE = /^[A-Za-z0-9][A-Za-z0-9._\/-]{0,63}$/;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

function ghHeaders(pat: string) {
  return {
    "Accept": "application/vnd.github+json",
    "Authorization": `Bearer ${pat}`,
    "X-GitHub-Api-Version": "2022-11-28",
    "Content-Type": "application/json",
  };
}

// Plain-English hint for the token problems we have actually hit.
function tokenHint(status: number, text: string): string {
  if (status === 401) return " — GitHub rejected the token (expired or wrong). Replace GITHUB_PAT in Supabase Edge Function secrets.";
  if (status === 403 && /personal access token/i.test(text)) {
    return " — the token is valid but lacks permission. It needs Repository permissions → Actions: Read and write on ecommerce-ukpos/pricewatch (and organisation approval if required).";
  }
  return "";
}

async function dispatch(pat: string, repo: string, inputs: Record<string, string>) {
  const res = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/scraper.yml/dispatches`,
    { method: "POST", headers: ghHeaders(pat), body: JSON.stringify({ ref: "main", inputs }) },
  );
  if (res.status === 204) return { ok: true };
  const text = await res.text();
  let msg = text;
  try { msg = JSON.parse(text).message || text; } catch { /* keep raw */ }
  return { ok: false, status: res.status, error: `${res.status} ${msg}${tokenHint(res.status, msg)}` };
}

async function listRuns(pat: string, repo: string, since: string) {
  const sinceDate = since.slice(0, 19);
  const url = `https://api.github.com/repos/${repo}/actions/workflows/scraper.yml/runs` +
    `?event=workflow_dispatch&per_page=100&created=${encodeURIComponent(">=" + sinceDate)}`;
  const res = await fetch(url, { headers: ghHeaders(pat) });
  const text = await res.text();
  if (!res.ok) {
    let msg = text;
    try { msg = JSON.parse(text).message || text; } catch { /* keep raw */ }
    return { ok: false, error: `${res.status} ${msg}${tokenHint(res.status, msg)}` };
  }
  const data = JSON.parse(text);
  const runs = (data.workflow_runs || []).map((r: any) => ({
    id: r.id,
    name: r.display_title || r.name,
    status: r.status,          // queued | in_progress | completed | waiting | requested | pending
    conclusion: r.conclusion,  // success | failure | cancelled | timed_out | null
    created_at: r.created_at,
    started_at: r.run_started_at,
    updated_at: r.updated_at,
    url: r.html_url,
  }));
  return { ok: true, runs };
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: CORS });
  if (req.method !== "POST") return json({ ok: false, error: "POST only" }, 405);

  const PAT = Deno.env.get("GITHUB_PAT");
  const REPO = Deno.env.get("GITHUB_REPO");
  if (!PAT || !REPO) return json({ ok: false, error: "Missing secrets" }, 500);

  let body: any = {};
  try {
    const text = await req.text();
    body = text ? JSON.parse(text) : {};
  } catch {
    return json({ ok: false, error: "Body must be JSON" }, 400);
  }

  // ── Run status for the progress panel ─────────────────────────────────────
  if (body && body.mode === "status") {
    const since = typeof body.since === "string" && !isNaN(Date.parse(body.since))
      ? new Date(Date.parse(body.since) - 60_000).toISOString()   // 1 min slack for clock skew
      : new Date(Date.now() - 24 * 3600_000).toISOString();
    const r = await listRuns(PAT, REPO, since);
    return r.ok ? json(r) : json(r, 502);
  }

  // ── Default: full rescrape (unchanged behaviour) ──────────────────────────
  if (!body || body.mode !== "check") {
    const r = await dispatch(PAT, REPO, { job: "scrape" });
    return r.ok ? json({ ok: true, dispatched: 1 }) : json(r, 500);
  }

  // ── SKU-list check ─────────────────────────────────────────────────────────
  const skus: string[] = [...new Set(
    (Array.isArray(body.skus) ? body.skus : []).map((s: unknown) => String(s).trim()).filter(Boolean),
  )] as string[];
  const comps: number[] = [...new Set(
    (Array.isArray(body.competitor_ids) ? body.competitor_ids : [])
      .map((c: unknown) => parseInt(String(c), 10)).filter((n: number) => Number.isInteger(n) && n > 0),
  )] as number[];

  if (!skus.length) return json({ ok: false, error: "No SKUs supplied" }, 400);
  if (skus.length > MAX_SKUS) return json({ ok: false, error: `Max ${MAX_SKUS} SKUs per check` }, 400);
  const bad = skus.filter((s) => !SKU_RE.test(s));
  if (bad.length) return json({ ok: false, error: `Invalid SKU codes: ${bad.slice(0, 10).join(", ")}` }, 400);
  if (!comps.length) return json({ ok: false, error: "Choose at least one competitor" }, 400);
  if (comps.length > MAX_COMPETITORS) return json({ ok: false, error: "Too many competitors" }, 400);

  const skuCsv = skus.join(",");
  const results: { run: string; ok: boolean; error?: string }[] = [];

  // One run per competitor: discovery, then rescrape. Stop at the first auth failure
  // instead of failing every competitor.
  for (const cid of comps) {
    const d = await dispatch(PAT, REPO, {
      job: "check", scraper_skus: skuCsv, competitor_ids: String(cid), discover_force: "false",
    });
    results.push({ run: `check:${cid}`, ok: d.ok, error: d.ok ? undefined : d.error });
    if (!d.ok && (d.status === 401 || d.status === 403)) break;
  }

  const failed = results.filter((r) => !r.ok);
  return json({
    ok: failed.length === 0,
    dispatched: results.length - failed.length,
    failed,
    error: failed.length ? `${failed.length} of ${results.length} runs failed to start: ${failed[0].error}` : undefined,
  }, failed.length === results.length ? 500 : 200);
});
