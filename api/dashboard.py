from http.server import BaseHTTPRequestHandler
import json, os, sys

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        try:
            from supabase import create_client
            sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

            all_snap = sb.table("latest_snapshots").select(
                "sku_id,competitor_id,diff_pct_normalised,diff_pct,availability"
            ).execute().data or []

            critical = sum(1 for r in all_snap if (r.get("diff_pct_normalised") or r.get("diff_pct") or 0) <= -10)
            warning  = sum(1 for r in all_snap if -10 < (r.get("diff_pct_normalised") or r.get("diff_pct") or 0) <= -5)
            cheapest = sum(1 for r in all_snap if (r.get("diff_pct_normalised") or r.get("diff_pct") or 0) > 0)
            oos      = sum(1 for r in all_snap if r.get("availability") == "out_of_stock")
            review_res = sb.table("competitor_matches").select("id", count="exact").eq("match_status","review").execute()
            review   = review_res.count or 0
            last_run = sb.table("sync_runs").select("*").order("started_at", desc=True).limit(1).execute().data
            last_run = last_run[0] if last_run else None
            alerts   = sb.table("alerts").select(
                "*, skus(short_title,product_url,slug), competitors(name,domain)"
            ).eq("dismissed", False).order("created_at", desc=True).limit(50).execute().data or []
            worst    = sb.table("worst_differentials").select("*").limit(10).execute().data or []

            body = json.dumps({
                "metrics": {"critical":critical,"warning":warning,"cheapest":cheapest,"oos":oos,"review":review},
                "last_run": last_run,
                "alerts": alerts,
                "worst": worst,
            })
        except Exception as e:
            body = json.dumps({"error": str(e)})
        self.wfile.write(body.encode())
