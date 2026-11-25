"""Hub bridge: the rig posts what Hub's posting queue hands it (Instagram / Threads / X rows of models set to "via rig").

Hub cannot reach this machine, so the rig pulls, like [sync] and [noswipe]:
  GET  {base_url}/api/rig/plan?phone=<id>&horizon_min=<n>[&dry=1]   what this phone should post (Hub hands each item over once,
                                                                    and keeps listing it until the result arrives)
  POST {base_url}/api/rig/result {queue_id, platform, status, permalink?, detail?}   posted | unverified | failed
Hub side: lib/rig.mjs in michaelsxan/hub. Media is a short-lived signed URL on Hub (downloaded here, never the token).

Parts:
  HubCfg       the [hub] section (off by default; the bearer token is read from an environment variable, never the file)
  Client       the HTTP calls (urllib, blocking: the engine runs it in a thread)
  Store        sqlite file (hub_rig.db): the items we took (one row per queue item x platform x attempt) and the result outbox
  Integration  poll -> download media -> write a normal plan folder into the mailbox inbox (so plan_check, quotas, scheduling and the
               posting flows are exactly the ones every other plan uses) -> watch the jobs -> report each result to Hub (retry outbox,
               idempotent: Hub treats a replay as a duplicate).

A job only counts as posted when the flow verified it on the profile (engine status done). `unverified` (Post was tapped, the
profile did not show it) is reported as such and never retried. Previews (dry_run, or ?dry=1 items) fill the compose screen up to
just before Post, cancel, and report nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import phones as phonecfg
from plan_check import IMAGE_EXT, VIDEO_EXT

APPS = ("instagram", "threads", "x")
STATUSES = ("posted", "unverified", "failed")
BACKOFF_BASE_S, BACKOFF_MAX_S = 30, 3600
MAX_DOWNLOAD_MB = 250
GIVE_UP_ON_MISSING_JOB_S = 600       # a plan we wrote that the engine never turned into a job
LATE_RECHECK_S = 3 * 86400           # a failed job the dashboard's Retry may still turn into a post


class ConfigError(phonecfg.ConfigError):
    pass


class HubError(Exception):
    def __init__(self, message: str, status: int = 0, retryable: bool = True):
        super().__init__(message)
        self.status, self.retryable = status, retryable


# ================================================================ config
@dataclass
class HubCfg:
    enabled: bool = False
    base_url: str = "https://hub.codenrap.com"
    token_env: str = "HUB_RIG_TOKEN"
    poll_every_min: float = 3
    horizon_min: float = 180        # ask for items due within this long (Hub hands an item over when it lists it)
    flush_every_s: float = 30
    dry_run: bool = False           # every item is a preview: media downloaded, compose screen filled, nothing posted or reported
    max_media_mb: float = MAX_DOWNLOAD_MB
    timeout_s: float = 30

    @property
    def token(self) -> str:
        return os.environ.get(self.token_env, "").strip()

    @classmethod
    def from_cfg(cls, cfg: dict) -> "HubCfg":
        raw = cfg.get("hub") or {}
        if not isinstance(raw, dict):
            raise ConfigError("[hub] must be a table")
        c = cls()
        for k in ("enabled", "base_url", "token_env", "dry_run"):
            if k in raw:
                setattr(c, k, raw[k])
        for k in ("poll_every_min", "horizon_min", "flush_every_s", "max_media_mb", "timeout_s"):
            if k in raw:
                setattr(c, k, float(raw[k]))
        c.enabled, c.dry_run = bool(c.enabled), bool(c.dry_run)
        c.base_url = str(c.base_url).rstrip("/")
        u = urllib.parse.urlparse(c.base_url)
        if u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")):
            raise ConfigError(f"[hub] base_url must be https (got {c.base_url!r}): the token travels in it")
        if c.poll_every_min < 0.05 or c.horizon_min < 1:
            raise ConfigError("[hub] poll_every_min must be at least 0.05 and horizon_min at least 1")
        return c


# ================================================================ text + time rules
_ASCII_FIX = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": " - ", "…": "...",
              " ": " ", "•": "-", "·": "-", "→": "->", "×": "x"}


def ascii_caption(text: str) -> str:
    """What `adb input text` can type: one line of printable ASCII. Smart punctuation is mapped, emoji and other non-ASCII are
    dropped, line breaks become single spaces, a bare % becomes the word (adb treats %s as a space)."""
    s = "".join(_ASCII_FIX.get(ch, ch) for ch in str(text))
    s = re.sub(r"(?<=\d)\s?%", " percent", s).replace("%", " percent ")
    s = s.encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip()


def local_slot(scheduled_s: float, now: float) -> tuple[str, str]:
    """(day, HH:MM) in the box's local time for a Hub slot: never in the past, inside the 09:00-23:00 window plan_check allows
    (before 09:00 -> 09:00 today, after 23:00 -> 09:00 tomorrow)."""
    dt = datetime.fromtimestamp(max(scheduled_s, now + 120))
    minutes = dt.hour * 60 + dt.minute
    if minutes < 9 * 60:
        dt = dt.replace(hour=9, minute=0)
    elif minutes > 23 * 60:
        dt = (dt + timedelta(days=1)).replace(hour=9, minute=0)
    return f"{dt:%Y-%m-%d}", f"{dt:%H:%M}"


def backoff(attempts: int) -> float:
    return min(BACKOFF_BASE_S * 2 ** max(0, attempts), BACKOFF_MAX_S)


# ================================================================ HTTP client
class Client:
    def __init__(self, base_url: str, token: str, timeout: float = 30):
        self.base_url, self._token, self.timeout = base_url.rstrip("/"), token, timeout
        self._host = urllib.parse.urlparse(self.base_url).netloc

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        url = self.base_url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}", "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as ex:
            try:
                msg = json.loads(ex.read() or b"{}").get("error", "")
            except Exception:
                msg = ""
            # 400/404/409: this request is wrong, retrying cannot help. 401/429/5xx: configuration or load, retry slowly.
            raise HubError(f"HTTP {ex.code} {msg}".strip(), ex.code, retryable=ex.code not in (400, 404, 409)) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as ex:
            raise HubError(f"network: {type(ex).__name__}", retryable=True) from None
        if not isinstance(payload, dict) or payload.get("ok") is False:
            raise HubError("bad answer from Hub", retryable=True)
        return payload

    def plan(self, phone_id: str, horizon_min: float, dry: bool = False) -> list[dict]:
        body = self._call("GET", "/api/rig/plan", {"phone": phone_id, "horizon_min": str(int(horizon_min)), **({"dry": "1"} if dry else {})})
        items = body.get("items")
        if not isinstance(items, list):
            raise HubError("bad /api/rig/plan answer")
        return [i for i in items if isinstance(i, dict)]

    def result(self, payload: dict) -> dict:
        return self._call("POST", "/api/rig/result", body=payload)

    def download(self, url: str, dest: Path, max_bytes: int) -> int:
        """Signed media URL on Hub's own host only; no token is sent (the signature is the credential)."""
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("https", "http") or u.netloc != self._host:
            raise HubError(f"media URL is not on {self._host}", retryable=False)
        n = 0
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=self.timeout) as r, dest.open("wb") as fh:
                while chunk := r.read(1 << 20):
                    n += len(chunk)
                    if n > max_bytes:
                        raise HubError(f"media is larger than {max_bytes // 1_000_000} MB", retryable=False)
                    fh.write(chunk)
        except urllib.error.HTTPError as ex:
            raise HubError(f"media HTTP {ex.code}", ex.code, retryable=ex.code not in (400, 403, 404)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            raise HubError(f"media network: {type(ex).__name__}", retryable=True) from None
        return n


# ================================================================ store
class Store:
    """Items taken from Hub + the result outbox. One sqlite file shared by the engine thread and the tests."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._ready = False

    def _con(self) -> sqlite3.Connection:
        if not self._ready:
            self._ready = True
            with sqlite3.connect(self.path, timeout=15) as c:
                c.executescript("""
                create table if not exists items (
                  key text not null, attempt integer not null, queue_id integer not null, platform text not null, app text not null,
                  account text not null, phone_id text not null, plan_name text, post_id text, state text not null, preview integer default 0,
                  detail text, created_at real not null, resolved_at real, primary key (key, attempt));
                create table if not exists results (
                  id integer primary key, key text not null, attempt integer not null, status text not null, payload text not null,
                  outcome text not null default 'pending', tries integer not null default 0, next_try real not null default 0,
                  last_error text, created_at real not null, sent_at real, unique (key, attempt, status));
                """)
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        return con

    def seen(self, key: str, attempt: int) -> bool:
        with self._con() as c:
            return c.execute("select 1 from items where key=? and attempt=?", (key, attempt)).fetchone() is not None

    def add_item(self, now: float, **f) -> None:
        f = {"plan_name": None, "post_id": None, "preview": 0, "detail": None, **f}
        with self._con() as c:
            c.execute("insert or ignore into items (key, attempt, queue_id, platform, app, account, phone_id, plan_name, post_id, state, preview, detail, created_at) "
                      "values (:key,:attempt,:queue_id,:platform,:app,:account,:phone_id,:plan_name,:post_id,:state,:preview,:detail,:now)", {**f, "now": now})

    def open_items(self) -> list[sqlite3.Row]:
        with self._con() as c:
            return c.execute("select * from items where state='imported' order by created_at").fetchall()

    def recent_failed(self, now: float) -> list[sqlite3.Row]:
        with self._con() as c:
            return c.execute("select * from items where state='failed' and plan_name is not null and resolved_at>? ", (now - LATE_RECHECK_S,)).fetchall()

    def set_state(self, key: str, attempt: int, state: str, now: float, detail: str | None = None) -> None:
        with self._con() as c:
            c.execute("update items set state=?, resolved_at=?, detail=coalesce(?, detail) where key=? and attempt=?", (state, now, detail, key, attempt))

    def enqueue(self, now: float, key: str, attempt: int, payload: dict) -> bool:
        with self._con() as c:
            cur = c.execute("insert or ignore into results (key, attempt, status, payload, created_at) values (?,?,?,?,?)",
                            (key, attempt, payload["status"], json.dumps(payload), now))
            return bool(cur.rowcount)

    def has_result(self, key: str, attempt: int, status: str | None = None) -> bool:
        with self._con() as c:
            q, a = "select 1 from results where key=? and attempt=?", [key, attempt]
            if status:
                q, a = q + " and status=?", a + [status]
            return c.execute(q, a).fetchone() is not None

    def due(self, now: float) -> list[sqlite3.Row]:
        with self._con() as c:
            return c.execute("select * from results where outcome='pending' and next_try<=? order by created_at, id", (now,)).fetchall()

    def mark(self, rid: int, outcome: str, now: float, error: str | None = None, retry_in: float = 0) -> None:
        with self._con() as c:
            if outcome == "pending":
                c.execute("update results set tries=tries+1, last_error=?, next_try=? where id=?", (error, now + retry_in, rid))
            else:
                c.execute("update results set outcome=?, last_error=?, sent_at=?, tries=tries+1 where id=?", (outcome, error, now, rid))

    def counts(self) -> dict:
        with self._con() as c:
            r = {x["outcome"]: x["n"] for x in c.execute("select outcome, count(*) n from results group by outcome")}
            i = {x["state"]: x["n"] for x in c.execute("select state, count(*) n from items group by state")}
            errs = [dict(e) for e in c.execute("select key, status, last_error, tries from results where last_error is not null and outcome in ('pending','dead') order by id desc limit 5")]
        return {"results": {"pending": r.get("pending", 0), "sent": r.get("sent", 0), "dead": r.get("dead", 0)}, "items": i, "errors": errs}

    def prune(self, now: float, keep_days: float = 30) -> None:
        with self._con() as c:
            c.execute("delete from results where outcome in ('sent','dead') and coalesce(sent_at, created_at) < ?", (now - keep_days * 86400,))
            c.execute("delete from items where state != 'imported' and coalesce(resolved_at, created_at) < ?", (now - keep_days * 86400,))


# ================================================================ the integration
def clean_error(text: str | None) -> str:
    t = re.sub(r"\(screenshot: [^)]*\)", "(screenshot on the rig)", str(text or "failed"))
    return re.sub(r"\s+", " ", t).strip()[:300]


class Integration:
    """check(raw_plan, folder) -> (clean_plan | None, problems): the engine's own plan validation with its live context.
    db() -> sqlite connection of engine.db. alert(kind, text, every_min)."""

    def __init__(self, cfg: HubCfg, phones: list[phonecfg.PhoneCfg], store: Store, inbox: Path, check: Callable, db: Callable,
                 alert: Callable, client: Client | None = None, log: Callable = print):
        self.cfg, self.phones, self.store, self.inbox = cfg, phones, store, Path(inbox)
        self.check, self.db, self.alert, self.client, self.log = check, db, alert, client, log
        self._next_poll = 0.0
        self._next_flush = 0.0
        self.last_poll: float | None = None
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.taken = 0

    # ---- setup
    @property
    def key_missing(self) -> bool:
        return self.cfg.enabled and not self.cfg.token

    @property
    def active(self) -> bool:
        return self.cfg.enabled and not self.key_missing

    def ensure_client(self) -> Client | None:
        if self.client is None and self.active:
            self.client = Client(self.cfg.base_url, self.cfg.token, self.cfg.timeout_s)
        return self.client

    # ---- the pass the engine calls every few seconds
    def tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        if not self.active:
            return
        if now >= self._next_poll:
            self.poll(now)
        self.collect(now)
        if now >= self._next_flush:
            self.flush(now)

    # ---- Hub -> plan folders
    def poll(self, now: float) -> None:
        client = self.ensure_client()
        self._next_poll = now + self.cfg.poll_every_min * 60
        self.last_poll = now
        failed: list[str] = []
        for ph in self.phones:
            if ph.accounts is not None and not any(a[0] in APPS for a in ph.accounts):
                continue
            try:
                items = client.plan(ph.id, self.cfg.horizon_min, dry=self.cfg.dry_run)
                self.last_ok = now
            except HubError as ex:
                failed.append(f"plan {ph.id}: {ex}")
                self.alert("hub", f"Hub plan poll failed ({ex}); will retry", every_min=120)
                continue
            self.take(ph, items, now)
        self.last_error = "; ".join(failed) or None

    def take(self, ph: phonecfg.PhoneCfg, items: list[dict], now: float) -> None:
        groups: dict[tuple[int, int], list[dict]] = {}
        for it in items:
            try:
                qid, attempt, platform = int(it["queue_id"]), int(it.get("attempt") or 1), str(it["platform"])
                if platform not in APPS or it.get("app") != platform or not isinstance(it.get("caption"), str):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                self.log(f"hub: ignoring a malformed plan item: {str(it)[:120]}")
                continue
            if self.store.seen(f"{qid}:{platform}", attempt):
                continue
            groups.setdefault((qid, attempt), []).append({**it, "queue_id": qid, "attempt": attempt, "platform": platform})
        for (qid, attempt), group in groups.items():
            try:
                self.import_group(ph, qid, attempt, group, now)
            except Exception as ex:  # one bad item must not stop the others (and is told to Hub, not retried blindly)
                self.log(f"hub: queue #{qid}: {ex!r}")
                for it in group:
                    self.refuse(ph, it, f"rig error: {type(ex).__name__}: {str(ex)[:120]}", now)

    def refuse(self, ph: phonecfg.PhoneCfg, it: dict, why: str, now: float) -> None:
        """Nothing was started for this item: record it and tell Hub (unless it is a preview)."""
        key, preview = f"{it['queue_id']}:{it['platform']}", bool(it.get("dry") or self.cfg.dry_run)
        self.store.add_item(now, key=key, attempt=it["attempt"], queue_id=it["queue_id"], platform=it["platform"], app=it["platform"],
                            account=str(it.get("handle") or ""), phone_id=ph.id, state="failed", preview=int(preview), detail=why[:300])
        self.store.set_state(key, it["attempt"], "failed", now, why[:300])
        if not preview:
            self.report(now, key, it["attempt"], it["queue_id"], it["platform"], "failed", why)
        self.log(f"hub: queue #{it['queue_id']} {it['platform']}: refused: {why}")

    def report(self, now: float, key: str, attempt: int, qid: int, platform: str, status: str, detail: str, permalink: str | None = None) -> None:
        payload = {"queue_id": qid, "platform": platform, "status": status, "detail": clean_error(detail)}
        if permalink:
            payload["permalink"] = permalink
        self.store.enqueue(now, key, attempt, payload)

    def import_group(self, ph: phonecfg.PhoneCfg, qid: int, attempt: int, group: list[dict], now: float) -> None:
        ok: list[dict] = []
        for it in group:
            handle = str(it.get("handle") or "").lstrip("@")
            owner = phonecfg.owner_of(self.phones, it["platform"], handle) if handle else None
            if owner is None or owner.id != ph.id:
                self.refuse(ph, it, f"no {it['platform']} account @{handle or '?'} on {ph.id} in the rig's config.toml", now)
            else:
                ok.append({**it, "handle": handle})
        if not ok:
            return
        first = ok[0]
        caption = ascii_caption(first["caption"])
        media = first.get("media") or []
        preview = any(i.get("dry") for i in ok) or self.cfg.dry_run
        problem = None
        if len(media) > 1:
            problem = "carousels / several media are not supported by the rig yet"
        elif any(i["platform"] == "instagram" for i in ok) and not (media and media[0].get("kind") == "video"):
            problem = "Instagram posts from the rig need a video"
        elif not caption:
            problem = "the caption has no typeable text after removing emoji and symbols"
        if problem:
            for it in ok:
                self.refuse(ph, it, problem, now)
            return
        name = f"hub-{qid}-a{attempt}"
        folder = self.inbox / name
        if folder.exists():
            shutil.rmtree(folder)
        (folder / "media").mkdir(parents=True)
        rel = None
        try:
            if media:
                m = media[0]
                ext = Path(urllib.parse.urlparse(m.get("name") or "").path).suffix.lower() or (".mp4" if m.get("kind") == "video" else ".jpg")
                if ext not in IMAGE_EXT | VIDEO_EXT:
                    raise HubError(f"media type {ext!r} is not an image or video", retryable=False)
                dest = folder / "media" / f"hub-{qid}{ext}"
                n = self.ensure_client().download(str(m.get("url") or ""), dest, int(self.cfg.max_media_mb * 1_000_000))
                want = first.get("sha256")
                if want and hashlib.sha256(dest.read_bytes()).hexdigest() != want:
                    raise HubError("the downloaded file does not match Hub's checksum", retryable=False)
                rel = f"media/{dest.name}"
                self.log(f"hub: queue #{qid}: downloaded {n / 1e6:.1f} MB")
        except HubError as ex:
            shutil.rmtree(folder, ignore_errors=True)
            if ex.retryable:   # a network blip: leave it unseen, the next poll tries again (Hub still lists it)
                self.log(f"hub: queue #{qid}: media download failed ({ex}); will retry")
                self.alert("hub", f"could not download media for Hub queue #{qid}: {ex}", every_min=120)
                return
            for it in ok:
                self.refuse(ph, it, str(ex), now)
            return
        day, at = local_slot(float(first.get("scheduled_at") or 0) / 1000.0, now)
        post = {"id": "p", "caption": caption, "at": at, "targets": [{"app": i["platform"], "account": i["handle"]} for i in ok]}
        if rel:
            post["video"] = rel
        raw = {"day": day, "source": "hub", **({"preview": True} if preview else {}), "posts": [post]}
        clean, problems = self.check(raw, folder)
        good = {(t["app"], t["account"]) for p in (clean or {}).get("posts", []) for t in p["targets"]}
        why = "; ".join(problems)[:300] or "rejected by the rig's plan check"
        for it in ok:
            if (it["platform"], it["handle"]) not in good:
                self.refuse(ph, it, why, now)
        keep = [i for i in ok if (i["platform"], i["handle"]) in good]
        if not keep:
            shutil.rmtree(folder, ignore_errors=True)
            return
        raw["posts"][0]["targets"] = [{"app": i["platform"], "account": i["handle"]} for i in keep]
        tmp = folder / ".plan.json.tmp"
        tmp.write_text(json.dumps(raw, indent=1))
        tmp.replace(folder / "plan.json")   # the engine globs plan.json: it only ever sees a complete plan
        for it in keep:
            self.store.add_item(now, key=f"{qid}:{it['platform']}", attempt=attempt, queue_id=qid, platform=it["platform"], app=it["platform"],
                                account=it["handle"], phone_id=ph.id, plan_name=name, post_id="p", state="imported", preview=int(preview))
        self.taken += len(keep)
        self.log(f"hub: queue #{qid} attempt {attempt}: plan {name} for {', '.join(i['platform'] + ' @' + i['handle'] for i in keep)} at {day} {at}"
                 f"{' (PREVIEW: nothing will be posted)' if preview else ''}")

    # ---- jobs -> results
    def collect(self, now: float) -> None:
        con = self.db()
        try:
            for it in self.store.open_items():
                self.resolve(con, it, now)
            for it in self.store.recent_failed(now):   # Retry on the dashboard may have turned a failure into a post
                job = self.job_of(con, it)
                if job is not None and job["status"] == "done" and not self.store.has_result(it["key"], it["attempt"], "posted"):
                    self.finish(it, job, now)
        finally:
            con.close()

    @staticmethod
    def job_of(con, it):
        return con.execute("select * from jobs where plan=? and kind='post' and app=? and account=? order by id desc limit 1",
                           (it["plan_name"], it["app"], it["account"])).fetchone()

    def resolve(self, con, it, now: float) -> None:
        job = self.job_of(con, it)
        if job is None:
            planned = con.execute("select 1 from plans where name=?", (it["plan_name"],)).fetchone()
            if planned or now - it["created_at"] > GIVE_UP_ON_MISSING_JOB_S:
                self.store.set_state(it["key"], it["attempt"], "failed", now, "the engine made no job for this plan")
                if not it["preview"]:
                    self.report(now, it["key"], it["attempt"], it["queue_id"], it["platform"], "failed", "the rig's engine made no job for this item (plan dropped at import)")
            return
        if job["status"] in ("done", "unverified", "failed", "skipped"):
            self.finish(it, job, now)

    def finish(self, it, job, now: float) -> None:
        a = json.loads(job["args"] or "{}")
        status, error = job["status"], job["error"]
        if a.get("preview"):
            self.store.set_state(it["key"], it["attempt"], "preview", now, clean_error(error or "preview finished"))
            return
        result = {}
        try:
            result = json.loads(job["result"] or "{}")
        except (TypeError, ValueError):
            pass
        if status == "done":
            state, detail, link = "posted", "verified on the profile" + (f" ({result.get('counts')})" if result.get("counts") else ""), result.get("permalink")
        elif status == "unverified":
            state, detail, link = "unverified", error or "tapped Post, could not confirm it", None
        else:
            state, detail, link = "failed", error or f"job {status}", None
        self.store.set_state(it["key"], it["attempt"], "failed" if state == "failed" else state, now, clean_error(detail))
        self.report(now, it["key"], it["attempt"], it["queue_id"], it["platform"], state, detail, link)
        self.log(f"hub: queue #{it['queue_id']} {it['platform']}: {state}")

    # ---- results -> Hub
    def flush(self, now: float) -> dict:
        client = self.ensure_client()
        self._next_flush = now + self.cfg.flush_every_s
        out = {"sent": 0, "dead": 0, "retry": 0}
        if client is None:
            return out
        for r in self.store.due(now):
            try:
                client.result(json.loads(r["payload"]))
                self.store.mark(r["id"], "sent", now)
                out["sent"] += 1
            except HubError as ex:
                if ex.retryable:
                    self.store.mark(r["id"], "pending", now, str(ex), backoff(r["tries"]))
                    out["retry"] += 1
                else:   # Hub says this answer is wrong for it (409: never handed over, 404: row deleted): nothing to retry
                    self.store.mark(r["id"], "dead", now, str(ex))
                    out["dead"] += 1
                    self.alert("hub", f"Hub refused the result for {r['key']} ({r['status']}): {ex}", every_min=0)
        if out["retry"]:
            self.last_error = f"outbox: {out['retry']} result(s) waiting to retry"
        self.store.prune(now)
        return out

    # ---- dashboard
    def view(self) -> dict:
        return {"enabled": self.cfg.enabled, "token_missing": self.key_missing, "dry_run": self.cfg.dry_run, "last_poll": self.last_poll,
                "last_ok": self.last_ok, "last_error": self.last_error, **(self.store.counts() if self.cfg.enabled else {})}
