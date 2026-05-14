from http.server import BaseHTTPRequestHandler
import json, os

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        try:
            from supabase import create_client
            sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
            length = int(self.headers.get('Content-Length', 0))
            payload = json.loads(self.rfile.read(length))
            sku_id = payload.get("sku_id","")
            sku = sb.table("skus").select("sku_id").eq("sku_id", sku_id).execute().data
            if not sku:
                body = json.dumps({"error": "SKU not found"})
            else:
                body = json.dumps({"ok": True, "message": f"Lookup queued for {sku_id}"})
        except Exception as e:
            body = json.dumps({"error": str(e)})
        self.wfile.write(body.encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
