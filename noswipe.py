"""NoSwipe integration: the rig runs the dating accounts NoSwipe creates for its clients.

Contract: docs/automation.md in zulfkhar00/date-finder. Two calls only:
  GET  {base_url}/runs?phoneId=<id>   what this phone should run (we act only on shouldRun=true)
  POST {base_url}/activity            what the phone really did (swipes, match, chat, date_booked, needs_you)

Parts:
  NoswipeCfg    the [noswipe] section (off by default; the API key is read from an environment variable, never the file)
  Client        the two HTTP calls (aiohttp); remembers the server clock skew from the Date header
  Outbox        sqlite file shared by the engine and the session processes: events wait here until NoSwipe accepted them.
                Retries with backoff; every event has a deterministic idempotency key, so a replay is a "duplicate" on
                the server and never a second count.
  Integration   the engine side: poll runs, turn them into `dating_session` jobs, stop work the moment a run stops,
                flush the outbox, keep the numbers the dashboard shows.

Nothing here invents activity: events are only enqueued by dating.py for things the phone UI reported.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import aiohttp

import phones as phonecfg

APPS = ("tinder", "hinge", "bumble")
KINDS = ("swipes", "match", "chat", "date_booked", "needs_you")
MAX_AHEAD_S = 5 * 60            # the server refuses occurredAt more than 5 min ahead
MAX_AGE_S = 7 * 24 * 3600       # ... or older than 7 days
AGE_MARGIN_S = 3600
BATCH = 200                     # events per POST (server limit)
BACKOFF_BASE_S, BACKOFF_MAX_S = 30, 3600
BAD_TIME_TRIES = 3
DEAD_CODES = {"invalid", "no_account"}     # retrying cannot help
DEFAULT_CAPS = {"tinder": 100, "hinge": 25, "bumble": 60}   # swipes per account per client-local day (conservative)


class ConfigError(phonecfg.ConfigError):
    pass


class NoswipeError(Exception):
    def __init__(self, message: str, status: int = 0, code: str = "", retryable: bool = True):
        super().__init__(message)
        self.status, self.code, self.retryable = status, code, retryable


# ================================================================ config
@dataclass
class NoswipeCfg:
    enabled: bool = False
    base_url: str = "https://www.noswipe.app/api/automation/v1"
    api_key_env: str = "NOSWIPE_AUTOMATION_KEY"
    poll_every_min: float = 2
    stale_after_min: float = 10        # no good poll for this long: no new sessions, running ones are stopped
    session_every_min: float = 180     # per account: pause between the end of a session and the next job
    session_minutes: float = 20        # time budget of one session
    default_timezone: str = "America/Los_Angeles"   # used when a run carries no timezone
    quiet_hours: str = "23:00-08:00"   # client-local; no sessions and no swiping inside
    swipe_caps: dict = field(default_factory=lambda: dict(DEFAULT_CAPS))
    flush_every_s: float = 30
    pace_s: tuple[float, float] = (4.0, 14.0)    # pause between swipe batches (random inside the range)
    batch_size: tuple[int, int] = (4, 12)        # swipes per batch (random inside the range)

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "").strip()

    @classmethod
    def from_cfg(cls, cfg: dict) -> "NoswipeCfg":
        raw = cfg.get("noswipe") or {}
        if not isinstance(raw, dict):
            raise ConfigError("[noswipe] must be a table")
        c = cls()
        for k in ("enabled", "base_url", "api_key_env", "default_timezone", "quiet_hours"):
            if k in raw:
                setattr(c, k, raw[k])
        for k in ("poll_every_min", "stale_after_min", "session_every_min", "session_minutes", "flush_every_s"):
            if k in raw:
                setattr(c, k, float(raw[k]))
        if "swipe_caps" in raw:
            c.swipe_caps = {**DEFAULT_CAPS, **{str(a): int(n) for a, n in dict(raw["swipe_caps"]).items()}}
        if "pace_s" in raw:
            c.pace_s = (float(raw["pace_s"][0]), float(raw["pace_s"][1]))
        if "batch_size" in raw:
            c.batch_size = (int(raw["batch_size"][0]), int(raw["batch_size"][1]))
        c.enabled = bool(c.enabled)
        c.base_url = str(c.base_url).rstrip("/")
        u = urlparse(c.base_url)
        if u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")):
            raise ConfigError(f"[noswipe] base_url must be https (got {c.base_url!r}): the key travels in it")
        if c.poll_every_min < 1:
            raise ConfigError("[noswipe] poll_every_min must be at least 1")
        parse_quiet(c.quiet_hours)  # raises on a bad value
        for a in c.swipe_caps:
            if a not in APPS or c.swipe_caps[a] < 1:
                raise ConfigError(f"[noswipe] swipe_caps: {a!r} must be one of {APPS} with a cap of at least 1")
        return c


def parse_quiet(spec: str) -> tuple[int, int] | None:
    """'23:00-08:00' -> (minute of day start, end). '' / 'off' -> None. Raises ConfigError."""
    if not spec or spec.strip().lower() == "off":
        return None
    try:
        a, b = spec.split("-")
        h1, m1 = (int(x) for x in a.split(":"))
        h2, m2 = (int(x) for x in b.split(":"))
        if not (0 <= h1 < 24 and 0 <= h2 < 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
            raise ValueError
        return h1 * 60 + m1, h2 * 60 + m2
    except ValueError:
        raise ConfigError(f"[noswipe] quiet_hours {spec!r}: expected \"HH:MM-HH:MM\" or \"off\"") from None


# ================================================================ idempotency + time guards
def make_key(user_id: str, app: str, kind: str, ref: str) -> str:
    """Deterministic: the same real-world event always yields the same key (so a replay is a duplicate server-side)."""
    h = hashlib.sha256(f"{user_id}|{app}|{kind}|{ref}".encode()).hexdigest()[:32]
    return f"rig-{kind.replace('_', '')}-{h}"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def guard_time(occurred: float, server_now: float) -> tuple[float, str]:
    """Fit occurredAt into the window NoSwipe accepts, judged by the SERVER's clock estimate.
    Returns (time to send, "ok" | "clamped" | "expired"). Never sends a time in the server's future."""
    if occurred < server_now - MAX_AGE_S + AGE_MARGIN_S:
        return occurred, "expired"
    ceiling = server_now - 2  # never ahead of the server's clock (it tolerates 5 min; we use none of it)
    if occurred > ceiling:
        return ceiling, "clamped"
    return occurred, "ok"


# ================================================================ HTTP client
class Client:
    def __init__(self, base_url: str, key: str, timeout: float = 20, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.base_url, self._key, self.timeout = base_url.rstrip("/"), key, timeout
        self.skew_s: float | None = None   # server clock minus ours, from the last response's Date header

    def server_now(self, local_now: float) -> float:
        return local_now + (self.skew_s or 0.0)

    async def _call(self, method: str, path: str, **kw) -> dict:
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as s:
                async with s.request(method, self.base_url + path, headers=headers, **kw) as r:
                    d = r.headers.get("Date")
                    if d:
                        try:
                            self.skew_s = parsedate_to_datetime(d).timestamp() - self.clock()
                        except (TypeError, ValueError):
                            pass
                    try:
                        body = await r.json(content_type=None)
                    except Exception:
                        body = {}
                    status = r.status
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as ex:
            raise NoswipeError(f"network: {type(ex).__name__}", retryable=True) from None
        if status == 200 and isinstance(body, dict) and body.get("ok", True):
            return body
        code = str((body or {}).get("code", "")) if isinstance(body, dict) else ""
        msg = str((body or {}).get("message", "")) if isinstance(body, dict) else ""
        # 401/503 are configuration problems on either side: retry slowly, someone must fix them. 400/404: this request is wrong.
        raise NoswipeError(f"HTTP {status} {code} {msg}".strip(), status, code, retryable=status not in (400, 404))

    async def runs(self, phone_id: str) -> list[dict]:
        body = await self._call("GET", "/runs", params={"phoneId": phone_id, "limit": "500"})
        runs = body.get("runs")
        if not isinstance(runs, list):
            raise NoswipeError("bad /runs answer", retryable=True)
        return [r for r in runs if isinstance(r, dict)]

    async def activity(self, user_id: str, events: list[dict]) -> list[dict]:
        body = await self._call("POST", "/activity", json={"userId": user_id, "events": events})
        res = body.get("results")
        if not isinstance(res, list):
            raise NoswipeError("bad /activity answer", retryable=True)
        return res


# ================================================================ outbox
class Outbox:
    """On-disk queue of events not yet accepted by NoSwipe. Shared by the engine and the session processes (sqlite)."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._ready = False

    def _con(self) -> sqlite3.Connection:
        if not self._ready:
            self._ready = True
            self._init()
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        return con

    def _init(self) -> None:
        with sqlite3.connect(self.path, timeout=15) as con:
            con.executescript("""
            create table if not exists events (
              id integer primary key, key text unique not null, user_id text not null, app text not null, kind text not null,
              payload text not null, occurred_at real not null, created_at real not null,
              status text not null default 'pending', attempts integer not null default 0, next_try real not null default 0,
              last_error text, sent_at real, account_ref text);
            create index if not exists events_due on events (status, next_try);
            create table if not exists swipes (account_ref text, day text, n integer, primary key (account_ref, day));
            """)

    def add(self, user_id: str, app: str, kind: str, ref: str, occurred_at: float, fields: dict | None = None,
            account_ref: str = "", now: float | None = None) -> str | None:
        """Queue one event. Returns its key, or None when that key is already queued (a replay: nothing changes)."""
        if kind not in KINDS or app not in APPS:
            raise ValueError(f"bad event {kind}/{app}")
        key = make_key(user_id, app, kind, ref)
        payload = {"kind": kind, "app": app, "idempotencyKey": key, **(fields or {})}
        with self._con() as con:
            cur = con.execute("insert or ignore into events (key, user_id, app, kind, payload, occurred_at, created_at, account_ref) "
                              "values (?,?,?,?,?,?,?,?)",
                              (key, user_id, app, kind, json.dumps(payload), occurred_at, now or time.time(), account_ref))
            return key if cur.rowcount else None

    def add_swipes(self, user_id: str, app: str, account_ref: str, count: int, day: str, ref: str, occurred_at: float) -> bool:
        """Queue a swipes event and add it to today's per-account count in ONE transaction (the daily cap reads this)."""
        key = make_key(user_id, app, "swipes", ref)
        payload = {"kind": "swipes", "app": app, "idempotencyKey": key, "count": int(count)}
        with self._con() as con:
            cur = con.execute("insert or ignore into events (key, user_id, app, kind, payload, occurred_at, created_at, account_ref) "
                              "values (?,?,?,?,?,?,?,?)",
                              (key, user_id, app, "swipes", json.dumps(payload), occurred_at, time.time(), account_ref))
            if not cur.rowcount:
                return False
            con.execute("insert into swipes (account_ref, day, n) values (?,?,?) on conflict(account_ref, day) do update set n=n+excluded.n",
                        (account_ref, day, int(count)))
            return True

    def swipes_today(self, account_ref: str, day: str) -> int:
        with self._con() as con:
            row = con.execute("select n from swipes where account_ref=? and day=?", (account_ref, day)).fetchone()
        return row["n"] if row else 0

    def due(self, now: float, limit: int = 1000) -> list[sqlite3.Row]:
        with self._con() as con:
            return con.execute("select * from events where status='pending' and next_try<=? order by occurred_at, id limit ?",
                               (now, limit)).fetchall()

    def mark(self, key: str, status: str, error: str | None = None, now: float | None = None, retry_in: float | None = None) -> None:
        with self._con() as con:
            if status == "pending":  # a failed try: back off
                con.execute("update events set attempts=attempts+1, last_error=?, next_try=? where key=?",
                            (error, (now or time.time()) + (retry_in or 0), key))
            else:
                con.execute("update events set status=?, last_error=?, sent_at=?, attempts=attempts+1 where key=?",
                            (status, error, now or time.time(), key))

    def counts(self, now: float | None = None) -> dict:
        now = now or time.time()
        with self._con() as con:
            c = {r["status"]: r["n"] for r in con.execute("select status, count(*) n from events group by status")}
            oldest = con.execute("select min(created_at) m from events where status='pending'").fetchone()["m"]
            errs = con.execute("select kind, app, last_error, attempts from events where last_error is not null and "
                               "status in ('pending','dead') order by id desc limit 5").fetchall()
        return {"pending": c.get("pending", 0), "sent": c.get("sent", 0), "dead": c.get("dead", 0),
                "oldest_pending_age_s": round(now - oldest) if oldest else None,
                "errors": [dict(e) for e in errs]}

    def prune(self, now: float, keep_days: float = 14) -> None:
        with self._con() as con:
            con.execute("delete from events where status in ('sent','dead') and coalesce(sent_at, created_at) < ?", (now - keep_days * 86400,))
            day = datetime.fromtimestamp(now - 3 * 86400).strftime("%Y-%m-%d")
            con.execute("delete from swipes where day < ?", (day,))


def backoff(attempts: int, rng: random.Random | None = None) -> float:
    base = min(BACKOFF_BASE_S * 2 ** max(0, attempts), BACKOFF_MAX_S)
    return base * (0.75 + 0.5 * (rng or random).random())


async def flush(outbox: Outbox, client: Client, now: float | None = None, clock: Callable[[], float] = time.time) -> dict:
    """Send every due event. Per event: created/duplicate -> sent; rejected invalid/no_account/bad_time -> dead (bad_time
    retried a few times first, with the server-clock correction); network/5xx/401/503 -> stay pending with backoff."""
    now = now if now is not None else clock()
    client.clock = clock
    out = {"sent": 0, "duplicate": 0, "dead": 0, "retry": 0, "expired": 0}
    by_user: dict[str, list[sqlite3.Row]] = {}
    for r in outbox.due(now):
        by_user.setdefault(r["user_id"], []).append(r)
    for user_id, rows in by_user.items():
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            send, sent_rows = [], []
            for r in chunk:
                t, how = guard_time(r["occurred_at"], client.server_now(now))
                if how == "expired":
                    outbox.mark(r["key"], "dead", "older than 7 days: NoSwipe cannot accept it", now)
                    out["expired"] += 1
                    continue
                ev = json.loads(r["payload"])
                ev["occurredAt"] = iso(t)
                send.append(ev)
                sent_rows.append(r)
            if not send:
                continue
            try:
                results = await client.activity(user_id, send)
            except NoswipeError as ex:
                for r in sent_rows:
                    if ex.retryable:
                        outbox.mark(r["key"], "pending", str(ex), now, backoff(r["attempts"]))
                        out["retry"] += 1
                    else:
                        outbox.mark(r["key"], "dead", str(ex), now)
                        out["dead"] += 1
                continue
            by_key = {x.get("idempotencyKey"): x for x in results if isinstance(x, dict)}
            for r in sent_rows:
                x = by_key.get(r["key"])
                st = (x or {}).get("status")
                if st in ("created", "duplicate"):
                    outbox.mark(r["key"], "sent", None, now)
                    out["sent" if st == "created" else "duplicate"] += 1
                elif st == "rejected":
                    code = x.get("code", "")
                    why = f"rejected {code}: {x.get('message', '')}".strip()
                    if code in DEAD_CODES or (code == "bad_time" and r["attempts"] + 1 >= BAD_TIME_TRIES):
                        outbox.mark(r["key"], "dead", why, now)
                        out["dead"] += 1
                    else:
                        outbox.mark(r["key"], "pending", why, now, backoff(r["attempts"]))
                        out["retry"] += 1
                else:  # not answered: try again, the key makes it safe
                    outbox.mark(r["key"], "pending", "no result for this event", now, backoff(r["attempts"]))
                    out["retry"] += 1
    return out


# ================================================================ engine side
def account_ref(run: dict) -> str:
    return str(run.get("accountRef") or f"{run.get('app')}-{str(run.get('userId', ''))[:8]}")


def run_wanted(run: dict) -> bool:
    return bool(run.get("shouldRun") is True and run.get("app") in APPS and run.get("userId"))


class Integration:
    """What the engine keeps about NoSwipe: last poll per phone, the runs it assigned, errors, the outbox."""

    def __init__(self, cfg: NoswipeCfg, phones: list[phonecfg.PhoneCfg], outbox: Outbox, client: Client | None = None):
        self.cfg, self.phones, self.outbox = cfg, phones, outbox
        self.client = client
        self.runs: dict[str, list[dict]] = {p.id: [] for p in phones}      # last good answer per phone
        self.last_poll: dict[str, float] = {}                              # last attempt
        self.last_ok: dict[str, float] = {}                                # last successful answer
        self.errors: deque = deque(maxlen=8)
        self._next_poll = 0.0
        self._next_flush = 0.0

    # ---- setup
    @property
    def key_missing(self) -> bool:
        return self.cfg.enabled and not self.cfg.api_key

    @property
    def active(self) -> bool:
        return self.cfg.enabled and not self.key_missing

    def error(self, now: float, text: str) -> None:
        self.errors.append({"at": now, "text": text})

    def ensure_client(self) -> Client | None:
        if self.client is None and self.active:
            self.client = Client(self.cfg.base_url, self.cfg.api_key)
        return self.client

    # ---- freshness: a stale list must never keep an account swiping
    def fresh(self, phone_id: str, now: float) -> bool:
        return now - self.last_ok.get(phone_id, 0) <= self.cfg.stale_after_min * 60

    def wanted(self, phone_id: str, now: float) -> dict[tuple[str, str], dict]:
        """(app, accountRef) -> run, for runs that may work now on this phone (empty when the list is stale)."""
        if not self.active or not self.fresh(phone_id, now):
            return {}
        return {(r["app"], account_ref(r)): r for r in self.runs.get(phone_id, []) if run_wanted(r)}

    # ---- polling
    async def poll(self, now: float) -> None:
        client = self.ensure_client()
        if client is None:
            return
        self._next_poll = now + self.cfg.poll_every_min * 60
        for p in self.phones:
            self.last_poll[p.id] = now
            try:
                self.runs[p.id] = await client.runs(p.noswipe_id)
                self.last_ok[p.id] = now
            except NoswipeError as ex:
                self.error(now, f"poll {p.id}: {ex}")

    def poll_due(self, now: float) -> bool:
        return self.active and now >= self._next_poll

    def flush_due(self, now: float) -> bool:
        return self.active and now >= self._next_flush

    async def flush(self, now: float) -> dict:
        client = self.ensure_client()
        self._next_flush = now + self.cfg.flush_every_s
        if client is None:
            return {}
        res = await flush(self.outbox, client, now)
        if res.get("retry") or res.get("dead"):
            self.error(now, f"outbox: {res['retry']} to retry, {res['dead']} dead")
        self.outbox.prune(now)
        return res

    # ---- runs -> jobs
    def sync_jobs(self, con: sqlite3.Connection, now: float) -> list[int]:
        """Make the jobs table match the runs. Creates a `dating_session` per wanted run (one open at a time, spaced by
        session_every_min), skips waiting sessions whose run stopped, and returns ids of RUNNING sessions that must stop now."""
        stop: list[int] = []
        for p in self.phones:
            want = self.wanted(p.id, now)
            rows = con.execute("select id, app, account, status from jobs where kind='dating_session' and status in ('waiting','running') "
                               "and (phone_id=? or (phone_id is null and ?))", (p.id, p is self.phones[0])).fetchall()
            for j in rows:
                if (j["app"], j["account"]) in want:
                    continue
                if j["status"] == "waiting":
                    con.execute("update jobs set status='skipped', error=? where id=?",
                                ("NoSwipe stopped this run (paused, unassigned or list stale)", j["id"]))
                else:
                    stop.append(j["id"])
            open_ = {(j["app"], j["account"]) for j in rows}
            for (app, ref), r in want.items():
                if (app, ref) in open_:
                    continue
                last = con.execute("select max(coalesce(ended_at, created_at)) from jobs where kind='dating_session' and app=? and account=? "
                                   "and phone_id=?", (app, ref, p.id)).fetchone()[0]
                if last and now - last < self.cfg.session_every_min * 60:
                    continue
                args = {"userId": r["userId"], "accountRef": ref, "timezone": r.get("timezone") or self.cfg.default_timezone,
                        "budget_min": self.cfg.session_minutes}
                con.execute("insert into jobs (plan, kind, app, account, run_at, not_after, args, status, label, created_at, phone_id) "
                            "values ('noswipe','dating_session',?,?,?,?,?,'waiting','dating',?,?)",
                            (app, ref, now, now + self.cfg.session_every_min * 60, json.dumps(args), now, p.id))
        con.commit()
        return stop

    # ---- dashboard
    def view(self, phone: phonecfg.PhoneCfg, now: float) -> dict:
        c = self.cfg
        runs = [{"app": r.get("app"), "accountRef": account_ref(r), "status": r.get("effectiveStatus") or r.get("status"),
                 "shouldRun": bool(r.get("shouldRun")), "onboarded": r.get("onboarded"), "held": r.get("held")}
                for r in self.runs.get(phone.id, [])]
        return {"enabled": c.enabled, "key_missing": self.key_missing, "phone_id": phone.noswipe_id,
                "last_poll": self.last_poll.get(phone.id), "last_ok": self.last_ok.get(phone.id),
                "stale": c.enabled and not self.fresh(phone.id, now), "runs": runs,
                "outbox": self.outbox.counts(now) if c.enabled else None,
                "errors": list(self.errors)[-5:]}
