"""One dating session on one phone for one NoSwipe client account (job kind `dating_session`).

SKELETON. The session lifecycle is real: quiet hours (client timezone), time budget, daily swipe cap per account,
human-like pacing, stop on SIGTERM (NoSwipe paused the run / take-over), login check -> needs_you, reporting through
the on-disk outbox. The phone steps are NOT: they sit behind `DatingUI`, whose only implementation (`UnmappedUI`) raises
`NotMapped`, because Tinder / Hinge / Bumble screens have not been mapped on a real phone yet.

Absolute rule: activity is reported ONLY from what the UI object returned. With UnmappedUI a session opens nothing,
reports nothing, and ends with outcome "not_mapped". No fabricated swipes, matches or chats, ever.

  python dating.py --user-id UUID --app hinge --account-ref hinge-017 --timezone America/Los_Angeles --minutes 20
Env: PHONE_SERIAL (set by the engine), ENGINE_CONFIG. Prints `RESULT {json}` as its last line.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import noswipe

ROOT = Path(__file__).parent
APP_NAMES = {"tinder": "Tinder", "hinge": "Hinge", "bumble": "Bumble"}


class NotMapped(Exception):
    """This UI step has no screen mapping yet (needs a live phone). Never caught to 'carry on': the session ends."""


class DatingUI(Protocol):
    """Everything a session needs from the phone. Implementations drive the real app and return only what happened."""

    def open_app(self) -> None: ...
    def logged_in(self) -> bool: ...
    def swipe_batch(self, n: int) -> int:
        """Swipe up to n profiles by the client's brief; return how many were REALLY swiped (0..n)."""
    def new_matches(self) -> list[str]:
        """Opaque ids of matches that appeared since the last call (the app's own match ids)."""
    def close_app(self) -> None: ...


class UnmappedUI:
    """The only UI today: every step raises NotMapped. Replace with a mapped implementation per app."""

    def __init__(self, app: str, serial: str = ""):
        self.app, self.serial = app, serial

    def _no(self, step: str):
        raise NotMapped(f"{self.app}: {step} is not mapped on a real phone yet")

    def open_app(self) -> None: self._no("open_app")
    def logged_in(self) -> bool: self._no("logged_in")
    def swipe_batch(self, n: int) -> int: self._no("swipe_batch")
    def new_matches(self) -> list[str]: self._no("new_matches")
    def close_app(self) -> None: self._no("close_app")


def make_ui(app: str, serial: str = "") -> DatingUI:
    return UnmappedUI(app, serial)


# ---------------------------------------------------------------- time rules
def tz_of(name: str, fallback: str) -> ZoneInfo:
    for n in (name, fallback, "UTC"):
        try:
            return ZoneInfo(n)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            continue
    return ZoneInfo("UTC")


def local_now(now: float, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(now, timezone.utc).astimezone(tz)


def in_quiet_hours(now: float, tz: ZoneInfo, spec: str) -> bool:
    q = noswipe.parse_quiet(spec)
    if q is None:
        return False
    t = local_now(now, tz)
    m = t.hour * 60 + t.minute
    a, b = q
    return a <= m < b if a <= b else (m >= a or m < b)


def local_day(now: float, tz: ZoneInfo) -> str:
    return local_now(now, tz).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- the session
@dataclass
class Session:
    user_id: str
    app: str
    account_ref: str
    tz: ZoneInfo
    budget_s: float
    job_id: str = "0"
    cfg: noswipe.NoswipeCfg = field(default_factory=noswipe.NoswipeCfg)


def run_session(ui: DatingUI, s: Session, outbox: noswipe.Outbox, *, clock: Callable[[], float] = time.time,
                sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None,
                stop: Callable[[], bool] = lambda: False, log: Callable[[str], None] = print) -> dict:
    """Run one session. Returns {"outcome": ..., "swipes": n, "matches": n}. Outcomes: quiet_hours, cap_reached,
    not_mapped, needs_login, done (budget used up or nothing left), stopped."""
    rng = rng or random.Random()
    cfg = s.cfg
    res = {"outcome": "done", "swipes": 0, "matches": 0, "account": s.account_ref, "app": s.app}
    start = clock()
    cap = cfg.swipe_caps.get(s.app, noswipe.DEFAULT_CAPS[s.app])
    if in_quiet_hours(start, s.tz, cfg.quiet_hours):
        log(f"quiet hours for the client ({s.tz.key}): not starting")
        return {**res, "outcome": "quiet_hours"}
    if outbox.swipes_today(s.account_ref, local_day(start, s.tz)) >= cap:
        log(f"daily swipe cap {cap} already reached for {s.account_ref}")
        return {**res, "outcome": "cap_reached"}
    try:
        ui.open_app()
        if not ui.logged_in():
            reason = f"Sign-in needed for your {APP_NAMES[s.app]} account"
            outbox.add(s.user_id, s.app, "needs_you", f"login:{s.account_ref}:{local_day(start, s.tz)}", clock(),
                       {"reason": reason}, s.account_ref)
            log("not logged in: needs_you queued")
            return {**res, "outcome": "needs_login"}
        deadline = start + s.budget_s
        seq = 0
        while True:
            now = clock()
            if stop():
                res["outcome"] = "stopped"
                break
            if now >= deadline:
                break
            if in_quiet_hours(now, s.tz, cfg.quiet_hours):
                res["outcome"] = "quiet_hours"
                break
            left = cap - outbox.swipes_today(s.account_ref, local_day(now, s.tz))
            if left <= 0:
                res["outcome"] = "cap_reached"
                break
            want = min(left, rng.randint(*cfg.batch_size))
            n = ui.swipe_batch(want)
            if not isinstance(n, int) or n < 0 or n > want:
                raise RuntimeError(f"UI reported {n!r} swipes for a batch of {want}: refusing to report it")
            if n:
                seq += 1
                outbox.add_swipes(s.user_id, s.app, s.account_ref, n, local_day(clock(), s.tz), f"{s.account_ref}:{s.job_id}:{seq}", clock())
                res["swipes"] += n
            else:
                break  # nothing left to swipe (out of profiles / app limit): do not spin
            sleep(min(rng.uniform(*cfg.pace_s), max(0.0, deadline - clock())))
        for ref in ui.new_matches():
            if outbox.add(s.user_id, s.app, "match", str(ref), clock(), {"matchRef": str(ref)}, s.account_ref):
                res["matches"] += 1
    except NotMapped as ex:
        log(f"NOT MAPPED: {ex}. Nothing reported.")
        return {**res, "outcome": "not_mapped", "detail": str(ex)}
    finally:
        try:
            ui.close_app()
        except Exception:
            pass
    return res


# ---------------------------------------------------------------- process entry
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--app", required=True, choices=noswipe.APPS)
    ap.add_argument("--account-ref", required=True)
    ap.add_argument("--timezone", default="")
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--job-id", default="0")
    a = ap.parse_args(argv)
    cfgd = tomllib.loads(Path(os.environ.get("ENGINE_CONFIG") or ROOT / "config.toml").read_text())
    cfg = noswipe.NoswipeCfg.from_cfg(cfgd)
    stopping = {"v": False}
    signal.signal(signal.SIGTERM, lambda *_: stopping.update(v=True))  # NoSwipe paused the run, or a take-over
    outbox = noswipe.Outbox(os.environ.get("NOSWIPE_OUTBOX") or ROOT / "noswipe_outbox.db")
    s = Session(a.user_id, a.app, a.account_ref, tz_of(a.timezone, cfg.default_timezone), a.minutes * 60, a.job_id, cfg)
    res = run_session(make_ui(a.app, os.environ.get("PHONE_SERIAL", "")), s, outbox, stop=lambda: stopping["v"],
                      sleep=lambda t: _interruptible_sleep(t, stopping))
    print("RESULT " + json.dumps(res), flush=True)
    return 0


def _interruptible_sleep(t: float, stopping: dict) -> None:
    end = time.time() + t
    while time.time() < end and not stopping["v"]:
        time.sleep(min(0.5, end - time.time()))


if __name__ == "__main__":
    sys.exit(main())
