"""A fake Hub (the two rig endpoints + signed media) on a local port, for tests. Real HTTP, so the client code is the real one.

    hub = FakeHub(token="t" * 32)
    hub.items = [{...plan item...}]      # what GET /api/rig/plan returns
    hub.results                          # every accepted POST /api/rig/result body, in order
    hub.fail_results = 2                 # answer the next 2 results with HTTP 500 (then recover)
    hub.result_status = 409              # answer every result with this status (Hub refusing it)
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeHub:
    def __init__(self, token: str = "t" * 32):
        self.token = token
        self.items: list[dict] = []
        self.media: dict[str, bytes] = {}      # path -> bytes
        self.results: list[dict] = []
        self.plan_calls: list[dict] = []
        self.tokens_seen: list[str] = []
        self.media_auth_seen: list[str | None] = []
        self.fail_results = 0
        self.fail_plan = 0
        self.comment_answer: str | None = None
        self.comment_calls: list[dict] = []
        self.result_status = 200
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body: dict | bytes, ctype: str = "application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _auth(self) -> bool:
                got = self.headers.get("Authorization", "")
                hub.tokens_seen.append(got)
                if got != f"Bearer {hub.token}":
                    self._send(401, {"error": "Bearer token required"})
                    return False
                return True

            def do_GET(self):
                u = urlparse(self.path)
                if u.path.startswith("/pub/media/"):
                    hub.media_auth_seen.append(self.headers.get("Authorization"))
                    blob = hub.media.get(u.path)
                    return self._send(200, blob, "application/octet-stream") if blob is not None else self._send(404, {"error": "no"})
                if u.path != "/api/rig/plan":
                    return self._send(404, {"error": "no"})
                if not self._auth():
                    return
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                hub.plan_calls.append(q)
                if hub.fail_plan:
                    hub.fail_plan -= 1
                    return self._send(503, {"error": "down"})
                mine = [i for i in hub.items if i.get("_phone", q.get("phone")) == q.get("phone")]
                self._send(200, {"ok": True, "phone": q.get("phone"), "items": [{k: v for k, v in i.items() if k != "_phone"} for i in mine]})

            def do_POST(self):
                path = urlparse(self.path).path
                if path not in ("/api/rig/result", "/api/rig/comment"):
                    return self._send(404, {"error": "no"})
                if not self._auth():
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if path == "/api/rig/comment":
                    hub.comment_calls.append(body)
                    return self._send(200, {"ok": True, "comment": hub.comment_answer})
                if hub.fail_results:
                    hub.fail_results -= 1
                    return self._send(500, {"error": "boom"})
                if hub.result_status != 200:
                    return self._send(hub.result_status, {"error": "refused"})
                hub.results.append(body)
                self._send(200, {"ok": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def media_url(self, name: str, blob: bytes) -> str:
        path = f"/pub/media/sig/{name}"
        self.media[path] = blob
        return self.url + path

    def close(self):
        self.server.shutdown()
        self.server.server_close()
