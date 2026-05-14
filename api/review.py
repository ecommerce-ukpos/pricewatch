from http.server import BaseHTTPRequestHandler
from datetime import datetime, timezone
import json, os

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        try:
            from supabase import create_client
            sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
            rows = sb.table("competitor_matches").select(
                "*, skus(short_title,price_ex_vat,product_url,slug), competitors(name,domain,vat_status)"
            ).eq("match_status","review").order("confidence").execute().data or []
            body = json.dumps({"data": rows})
        except Exception as e:
            body = json.dumps({"error": str(e)})
        self.wfile.write(body.encode())

    def do_POST(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        try:
            from supabase import create_client
            sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
            match_id = self.path.rstrip('/').split('/')[-1]
            length = int(self.headers.get('Content-Length', 0))
            payload = json.loads(self.rfile.read(length))
            new_status = "matched" if payload.get("decision") == "approve" else "rejected"
            sb.table("competitor_matches").update({
                "match_status": new_status,
                "human_reviewed": True,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", match_id).execute()
            body = json.dumps({"ok": True, "status": new_status})
        except Exception as e:
            body = json.dumps({"error": str(e)})
        self.wfile.write(body.encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
