from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
import json, os, base64

AUDIT_VIEWER = "apritchard@ukpos.com"   # the only account allowed to read the trail


def _user(sb, header):
    token = (header or "").replace("Bearer ", "").strip()
    if not token:
        return None, None
    u = sb.auth.get_user(token)          # server-side token validation
    user = getattr(u, "user", None)
    if not user:
        return None, None
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        claims = {}
    return user, claims.get("session_id")


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _sb(self):
        from supabase import create_client
        return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

    # Record a login (one row per Supabase auth session)
    def do_POST(self):
        try:
            sb = self._sb()
            user, sid = _user(sb, self.headers.get('Authorization'))
            if not user:
                return self._send(401, {"error": "unauthorised"})
            h = self.headers
            ip = (h.get('x-forwarded-for') or h.get('x-real-ip') or '').split(',')[0].strip() or None
            row = {
                "user_id": user.id, "email": (user.email or '').lower(), "session_id": sid,
                "ip": ip,
                "city": unquote(h.get('x-vercel-ip-city') or '') or None,
                "region": h.get('x-vercel-ip-country-region') or None,
                "country": h.get('x-vercel-ip-country') or None,
                "user_agent": (h.get('user-agent') or '')[:300] or None,
            }
            sb.table("login_audit").upsert(row, on_conflict="session_id", ignore_duplicates=True).execute()
            self._send(200, {"ok": True})
        except Exception as e:
            self._send(500, {"error": str(e)})

    # Read the trail — apritchard@ukpos.com only
    def do_GET(self):
        try:
            sb = self._sb()
            user, _ = _user(sb, self.headers.get('Authorization'))
            if not user:
                return self._send(401, {"error": "unauthorised"})
            if (user.email or '').lower() != AUDIT_VIEWER:
                return self._send(403, {"error": "forbidden"})
            uid = parse_qs(urlparse(self.path).query).get('user_id', [''])[0]
            if not uid:
                return self._send(400, {"error": "user_id required"})
            rows = (sb.table("login_audit").select("logged_in_at,ip,city,region,country,user_agent")
                    .eq("user_id", uid).order("logged_in_at", desc=True).limit(20).execute().data or [])
            self._send(200, {"data": rows})
        except Exception as e:
            self._send(500, {"error": str(e)})
