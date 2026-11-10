import "jsr:@supabase/functions-js/edge-runtime.d.ts";

// trigger-scrape (slug: smart-function)
// Dispatches GitHub Actions runs of .github/workflows/scraper.yml.
//
//  POST with no body / {}            → one full price scrape of all confirmed matches
//                                      (the Match Manager "Send all for rescrape" button).
//  POST {mode:"check", skus:[...],   → SKU-list price check from the "SKU list check" screen:
//        competitor_ids:[...]}          1 × scrape run   — rescrape existing confirmed matches for
//                                                         these SKUs × chosen competitors
//                                       N × discover run — one run per chosen competitor, matching
//                                                         its product pages against these SKUs only
//                                                         (one per competitor keeps each run well
//                                                         inside GitHub's 6-hour job limit).
//  Discovery never runs with force, so confirmed matches are never reset to review.
//
//  Secrets: GITHUB_PAT (actions:write on the repo), GITHUB_REPO ("owner/repo").

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

async function dispatch(pat: string, repo: string, inputs: Record<string, string>) {
  const res = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/scraper.yml/dispatches`,
    {
      method: "POST",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${pat}`,
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs }),
    },
  );
  if (res.status === 204) return { ok: true };
  return { ok: false, status: res.status, error: await res.text() };
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

  const scrape = await dispatch(PAT, REPO, {
    job: "scrape", scraper_skus: skuCsv, competitor_ids: comps.join(","),
  });
  results.push({ run: "scrape", ok: scrape.ok, error: scrape.ok ? undefined : `${scrape.status} ${scrape.error}` });

  for (const cid of comps) {
    const d = await dispatch(PAT, REPO, {
      job: "discover", scraper_skus: skuCsv, competitor_ids: String(cid), discover_force: "false",
    });
    results.push({ run: `discover:${cid}`, ok: d.ok, error: d.ok ? undefined : `${d.status} ${d.error}` });
  }

  const failed = results.filter((r) => !r.ok);
  return json({
    ok: failed.length === 0,
    dispatched: results.length - failed.length,
    failed,
    error: failed.length ? `${failed.length} of ${results.length} runs failed to start: ${failed[0].error}` : undefined,
  }, failed.length === results.length ? 500 : 200);
});
