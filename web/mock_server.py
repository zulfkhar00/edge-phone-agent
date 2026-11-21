"""Serve dashboard.html with sample data, no phone needed (for design work and screenshots).

Usage: python3 web/mock_server.py [single|multi] [PORT] [HTML]
Keys ending in _dt in the fixture are seconds from now ("run_at_dt": 3600 -> run_at = now + 1 h).
"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
FIX = sys.argv[1] if len(sys.argv) > 1 else "single"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8099
HTML = Path(sys.argv[3]) if len(sys.argv) > 3 else HERE / "dashboard.html"


def absolute(o, now):
    if isinstance(o, dict):
        return {(k[:-3] if k.endswith("_dt") else k): (now + v if k.endswith("_dt") else absolute(v, now)) for k, v in o.items()}
    if isinstance(o, list):
        return [absolute(x, now) for x in o]
    return o


class H(BaseHTTPRequestHandler):
    def send(self, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send(HTML.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self.send(json.dumps(absolute(json.loads((HERE / "fixtures" / f"{FIX}.json").read_text()), time.time())))
        elif path == "/api/results":
            self.send(json.dumps({"posts": [], "research": []}))
        else:
            self.send_error(404)

    def do_POST(self):
        self.send('{"ok": true}')

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
