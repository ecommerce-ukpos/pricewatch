from http.server import BaseHTTPRequestHandler
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
            comps = sb.table("competitors").select("*").eq("active", True).order("name").execute().data or []
            body = json.dumps({"data": comps})
        except Exception as e:
            body = json.dumps({"error": str(e)})
        self.wfile.write(body.encode())

    def do_PATCH(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        try:
            from supabase import create_client
            sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
            competitor_id = self.path.rstrip('/').split('/')[-1]
            length = int(self.headers.get('Content-Length', 0))
            updates = json.loads(self.rfile.read(length))
            result = sb.table("competitors").update(updates).eq("id", competitor_id).execute()
            body = json.dumps({"ok": True, "data": result.data})
        except Exception as e:
            body = json.dumps({"error": str(e)})
        self.wfile.write(body.encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, PATCH, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
