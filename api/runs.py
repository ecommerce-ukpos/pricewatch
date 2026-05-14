from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
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
            params = parse_qs(urlparse(self.path).query)
            limit = min(int(params.get('limit',['20'])[0]), 100)
            rows = sb.table("sync_runs").select("*").order("started_at", desc=True).limit(limit).execute().data or []
            body = json.dumps({"data": rows})
        except Exception as e:
            body = json.dumps({"error": str(e)})
        self.wfile.write(body.encode())
