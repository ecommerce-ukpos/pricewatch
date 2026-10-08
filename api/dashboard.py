from http.server import BaseHTTPRequestHandler
import json, os, sys
from urllib.parse import urlparse, parse_qs

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

            qs  = parse_qs(urlparse(self.path).query)
            def num(k, d):
                try: return float(qs.get(k, [d])[0])
                except Exception: return float(d)
            RED, AMB, PAR = num('red', 10), num('amb', 5), num('par', 1)
            diffs = [(r.get("diff_pct_normalised") if r.get("diff_pct_normalised") is not None else r.get("diff_pct")) for r in all_snap]
            diffs = [float(d) for d in diffs if d is not None]
            critical = sum(1 for d in diffs if d <= -RED)
            warning  = sum(1 for d in diffs if -RED < d <= -AMB)
            watch    = sum(1 for d in diffs if -AMB < d < -PAR)
            parity   = sum(1 for d in diffs if -PAR <= d <= PAR)
            cheapest = sum(1 for d in diffs if d > PAR)
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
                "metrics": {"critical":critical,"warning":warning,"watch":watch,"parity":parity,"cheapest":cheapest,"oos":oos,"review":review},
                "last_run": last_run,
                "alerts": alerts,
                "worst": worst,
            })
        except Exception as e:
            body = json.dumps({"error": str(e)})
        self.wfile.write(body.encode())
