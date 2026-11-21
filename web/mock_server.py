"""Serve dashboard.html with sample data, no phone needed (for design work and screenshots).

Usage: python3 web/mock_server.py [single|multi] [PORT] [HTML]
Keys ending in _dt in the fixtures are seconds from now ("run_at_dt": 3600 -> run_at = now + 1 h).
The stats / research / trends endpoints run the real insights.py over web/fixtures/insights.json.
"""
import colorsys
import json
import sys
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import insights  # noqa: E402

absolute = insights.resolve_offsets


def fixture_data(now):
    d = absolute(json.loads((HERE / "fixtures" / "insights.json").read_text()), now)
    return d["posts"], d["research"]


def api(path, query, now):
    """The same JSON the engine serves for /api/insights/stats, /api/insights/research and /api/trends."""
    posts, research = fixture_data(now)
    q = lambda k, d=None: (query.get(k) or [d])[0] or d
    if path == "/api/insights/stats":
        return insights.stats_report(posts, now, int(q("days", 7)), q("app"), q("account"))
    if path == "/api/insights/research":
        days = q("days", "14")
        return insights.research_report(research, now, None if days == "all" else int(days), q("app"))
    if path == "/api/trends":
        return insights.trends(research, now, int(q("days", 14)), q("app"), int(q("limit", 10)))
    return None


def thumb_svg(post_id):
    h = (zlib.crc32(str(post_id).encode()) % 360) / 360
    c1 = "#%02x%02x%02x" % tuple(int(x * 255) for x in colorsys.hsv_to_rgb(h, .35, .95))
    c2 = "#%02x%02x%02x" % tuple(int(x * 255) for x in colorsys.hsv_to_rgb((h + .08) % 1, .55, .75))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 90 160"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient></defs>'
            f'<rect width="90" height="160" fill="url(#g)"/><circle cx="45" cy="80" r="16" fill="#fff" fill-opacity=".55"/>'
            f'<path d="M40 72l16 8-16 8z" fill="#fff"/></svg>')


class H(BaseHTTPRequestHandler):
    def send(self, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        now = time.time()
        if path == "/":
            self.send(HTML.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self.send(json.dumps(absolute(json.loads((HERE / "fixtures" / f"{FIX}.json").read_text()), now)))
        elif path == "/api/results":
            self.send(json.dumps({"posts": [], "research": []}))
        elif path.startswith("/api/post/") and path.endswith("/thumb.jpg"):
            self.send(thumb_svg(path.split("/")[3]), "image/svg+xml")
        elif (data := api(path, parse_qs(u.query), now)) is not None:
            self.send(json.dumps(data))
        else:
            self.send_error(404)

    def do_POST(self):
        self.send('{"ok": true}')

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    FIX = sys.argv[1] if len(sys.argv) > 1 else "single"
    PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8099
    HTML = Path(sys.argv[3]) if len(sys.argv) > 3 else HERE / "dashboard.html"
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
