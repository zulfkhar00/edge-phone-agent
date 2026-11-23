"""A fake NoSwipe server (the contract in docs/automation.md of date-finder): Bearer auth, GET /runs?phoneId=,
POST /activity with per-event created / duplicate / rejected, the occurredAt window (<= 5 min ahead, <= 7 days old)
and idempotency per (userId, idempotencyKey). Counts what a real server would show a client."""

from __future__ import annotations

import time
from datetime import datetime

from aiohttp import web
from aiohttp.test_utils import TestServer

KEY = "k" * 40
BASE = "/api/automation/v1"


class FakeNoswipe:
    def __init__(self):
        self.runs: dict[str, list[dict]] = {}     # phoneId -> runs
        self.events: dict[tuple[str, str], dict] = {}   # (userId, key) -> event
        self.activity_calls = 0
        self.runs_calls: list[str] = []
        self.fail_next = 0                         # answer the next N activity calls with 503
        self.lose_response_next = 0                # store the events, then answer 503 (client timeout case)
        self.reject_codes: dict[str, str] = {}     # idempotencyKey -> code (forced rejection)
        self.server: TestServer | None = None

    # ---- helpers for tests
    @property
    def counts(self) -> dict:
        out: dict = {}
        for (_, _), e in self.events.items():
            out[e["kind"]] = out.get(e["kind"], 0) + (e.get("count", 1) if e["kind"] == "swipes" else 1)
        return out

    def run(self, user="u1", app="hinge", ref="hinge-001", should=True, **kw) -> dict:
        return {"userId": user, "app": app, "phoneId": "p", "accountRef": ref, "status": "active", "userPaused": not should,
                "effectiveStatus": "active" if should else "paused", "onboarded": True, "held": False, "shouldRun": should, **kw}

    # ---- the app
    def authed(self, req) -> bool:
        return req.headers.get("Authorization") == f"Bearer {KEY}"

    async def get_runs(self, req):
        if not self.authed(req):
            return web.json_response({"ok": False, "code": "unauthorized", "message": "no"}, status=401)
        pid = req.query.get("phoneId", "")
        self.runs_calls.append(pid)
        return web.json_response({"ok": True, "runs": self.runs.get(pid, [])})

    async def post_activity(self, req):
        if not self.authed(req):
            return web.json_response({"ok": False, "code": "unauthorized", "message": "no"}, status=401)
        self.activity_calls += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            return web.json_response({"ok": False, "code": "db", "message": "transient"}, status=503)
        body = await req.json()
        results, created, dup, rej = [], 0, 0, 0
        now = time.time()
        for i, ev in enumerate(body["events"]):
            k = ev.get("idempotencyKey", "")
            res = {"index": i, "idempotencyKey": k}
            try:
                t = datetime.fromisoformat(ev["occurredAt"].replace("Z", "+00:00")).timestamp()
            except Exception:
                t = now
            if k in self.reject_codes:
                res.update(status="rejected", code=self.reject_codes[k], message="forced")
                rej += 1
            elif len(k) < 8 or ev.get("kind") not in ("swipes", "match", "chat", "date_booked", "needs_you"):
                res.update(status="rejected", code="invalid", message="Invalid event.")
                rej += 1
            elif t > now + 300 or t < now - 7 * 86400:
                res.update(status="rejected", code="bad_time", message="occurredAt out of range")
                rej += 1
            elif (body["userId"], k) in self.events:
                res["status"] = "duplicate"
                dup += 1
            else:
                self.events[(body["userId"], k)] = {**ev, "userId": body["userId"]}
                res["status"] = "created"
                created += 1
            results.append(res)
        if self.lose_response_next > 0:
            self.lose_response_next -= 1
            return web.json_response({"ok": False, "code": "db", "message": "lost"}, status=503)
        return web.json_response({"ok": True, "created": created, "duplicates": dup, "rejected": rej, "results": results})

    def app(self) -> web.Application:
        a = web.Application()
        a.add_routes([web.get(BASE + "/runs", self.get_runs), web.post(BASE + "/activity", self.post_activity)])
        return a

    async def start(self) -> str:
        self.server = TestServer(self.app(), host="127.0.0.1")
        await self.server.start_server()
        return f"http://127.0.0.1:{self.server.port}{BASE}"

    async def stop(self) -> None:
        if self.server:
            await self.server.close()
