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
            q      = params.get('q',[''])[0]
            diff   = params.get('diff',[''])[0]
            vat    = params.get('vat',[''])[0]
            stock  = params.get('stock',[''])[0]
            page   = int(params.get('page',['1'])[0])
            limit  = min(int(params.get('limit',['50'])[0]), 200)
            offset = (page - 1) * limit

            query = sb.table("latest_snapshots").select("*")
            if q:     query = query.or_(f"short_title.ilike.%{q}%,sku_id.ilike.%{q}%,competitor_name.ilike.%{q}%")
            def num(k, d):
                try: return float(params.get(k,[d])[0])
                except Exception: return float(d)
            RED, AMB, PAR = num('red',10), num('amb',5), num('par',1)
            if diff == 'crit': query = query.lte("diff_pct_normalised", -RED)
            elif diff == 'warn': query = query.gt("diff_pct_normalised", -RED).lte("diff_pct_normalised", -AMB)
            elif diff == 'watch': query = query.gt("diff_pct_normalised", -AMB).lt("diff_pct_normalised", -PAR)
            elif diff == 'par': query = query.gte("diff_pct_normalised", -PAR).lte("diff_pct_normalised", PAR)
            elif diff == 'cheap': query = query.gt("diff_pct_normalised", PAR)
            if vat:   query = query.eq("competitor_vat", vat)
            if stock == 'oos':    query = query.eq("availability","out_of_stock")
            elif stock == 'unavail': query = query.eq("availability","unavailable")
            elif stock == 'in':   query = query.eq("availability","in_stock")

            result = query.order("diff_pct_normalised", nullsfirst=False).range(offset, offset+limit-1).execute()
            body = json.dumps({"data": result.data or [], "page": page, "limit": limit})
        except Exception as e:
            body = json.dumps({"error": str(e)})
        self.wfile.write(body.encode())
