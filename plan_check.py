"""Validate a plan.json before the engine turns it into jobs.

The plan comes from an unattended nightly session, so nothing in it is trusted. Each post / research session is
checked on its own: invalid ones are dropped (and reported), valid ones still run. A plan-level problem (bad day,
unreadable file) rejects the whole plan.

    from plan_check import check_plan
    clean, problems = check_plan(plan, folder, cfg, existing_captions, posted_today)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

ASCII_LINE = re.compile(r"^[\x20-\x7E]+$")  # what `adb input text` can type: printable ASCII, no newlines
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
ROUNDS = {12, 16, 20}
EARLIEST, LATEST = "09:00", "23:00"
CAPTION_KEY = 30  # stats find a post by the first 30 characters of its caption
VIDEO_APPS = ("tiktok", "instagram")  # need a vertical video (3-90 s)
TEXT_LIMITS = {"x": 280, "threads": 500}  # text apps: the post is text, media (image or video) is optional
POST_APPS = VIDEO_APPS + tuple(TEXT_LIMITS)
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}


def caption_key(caption: str) -> str:
    return caption[:CAPTION_KEY].strip().lower()


def probe(video: Path) -> dict | None:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    out = subprocess.run([exe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
                          "-of", "json", str(video)], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        return {"error": out.stderr.strip()[:120] or "unreadable"}
    j = json.loads(out.stdout or "{}")
    s = (j.get("streams") or [{}])[0]
    return {"width": s.get("width"), "height": s.get("height"), "duration": float(j.get("format", {}).get("duration", 0) or 0)}


def check_plan(plan: dict, folder: Path, cfg: dict, existing_captions: set[str], posted_today: dict[tuple, int],
               today: date | None = None) -> tuple[dict | None, list[str]]:
    problems: list[str] = []
    today = today or date.today()
    accounts = cfg.get("accounts", {})
    practice = bool(plan.get("practice"))  # test plans may use practice accounts and deletes
    hub = plan.get("source") == "hub"  # written by hub_rig.py for a Hub queue item: its accounts are the ones Hub's rig mode names

    # ---- plan level
    try:
        day = datetime.strptime(str(plan.get("day")), "%Y-%m-%d").date()
    except ValueError:
        return None, [f"day {plan.get('day')!r} is not a YYYY-MM-DD date"]
    if not (today <= day <= today + timedelta(days=2)):
        return None, [f"day {day} is not today or within the next 2 days"]

    def account_ok(app: str, account: str, where: str, apps: tuple = POST_APPS) -> dict | None:
        if app not in apps:
            problems.append(f"{where}: unknown app {app!r}")
            return None
        lim = accounts.get(app, {}).get(account)
        if lim is None:
            problems.append(f"{where}: {app} account {account!r} is not in config.toml")
            return None
        if lim.get("use") != "noswipe" and not practice and not (hub and lim.get("use") in ("hub", "practice")):
            problems.append(f"{where}: {app} @{account} is a {lim.get('use', 'unlabelled')} account, not for nightly plans")
            return None
        return lim

    def time_ok(item: dict, where: str) -> bool:
        if item.get("at") is not None:
            if not HHMM.match(str(item["at"])) or not (EARLIEST <= item["at"] <= LATEST):
                problems.append(f"{where}: time {item['at']!r} must be HH:MM between {EARLIEST} and {LATEST}")
                return False
            return True
        if item.get("round") not in ROUNDS:
            problems.append(f"{where}: round {item.get('round')!r} must be one of {sorted(ROUNDS)} (or give \"at\")")
            return False
        return True

    # ---- posts
    clean_posts, seen_ids, seen_keys = [], set(), set()
    counts = dict(posted_today)
    for i, post in enumerate(plan.get("posts") or []):
        where = f"post {post.get('id', i + 1)!r}"
        if not isinstance(post, dict) or not isinstance(post.get("id"), str) or not post["id"]:
            problems.append(f"post #{i + 1}: missing id")
            continue
        if post["id"] in seen_ids:
            problems.append(f"{where}: duplicate id")
            continue
        cap = post.get("caption", post.get("text"))  # X / Threads posts may say "text"
        raw_targets = [t for t in (post.get("targets") or []) if isinstance(t, dict)]
        needs_video = any(t.get("app") in VIDEO_APPS for t in raw_targets)
        if not isinstance(cap, str) or not ASCII_LINE.match(cap):
            problems.append(f"{where}: caption must be one line of plain ASCII (no emoji, curly quotes or line breaks)")
            continue
        lo = 10 if needs_video else 1
        if not (lo <= len(cap) <= 2000):
            problems.append(f"{where}: caption length {len(cap)} ({lo}-2000)")
            continue
        key = caption_key(cap)
        if key in seen_keys or key in existing_captions:
            problems.append(f"{where}: first {CAPTION_KEY} characters {key!r} were already used (stats could not tell the posts apart)")
            continue
        media_rel = post.get("video") or post.get("media")
        video, kind = None, None
        if media_rel is None:
            if needs_video:
                problems.append(f"{where}: video {post.get('video')!r} is missing")
                continue
        else:
            video = (folder / str(media_rel)).resolve()
            if folder.resolve() not in video.parents or not video.is_file():
                problems.append(f"{where}: video {media_rel!r} is missing")
                continue
            ext = video.suffix.lower()
            kind = "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None
            if kind is None:
                problems.append(f"{where}: media {media_rel!r} must be an image (jpg/png/webp) or a video (mp4/mov)")
                continue
            if kind == "image" and needs_video:
                problems.append(f"{where}: instagram / tiktok targets need a video, not an image")
                continue
            size = video.stat().st_size
            if kind == "image":
                if not (1_000 <= size <= 5_000_000):
                    problems.append(f"{where}: image size {size / 1e6:.1f} MB looks wrong (max 5 MB)")
                    continue
            elif not ((100_000 if needs_video else 10_000) <= size <= 250_000_000):
                problems.append(f"{where}: video size {size / 1e6:.1f} MB looks wrong")
                continue
            info = probe(video) if kind == "video" else None
            if info is not None:
                if "error" in info:
                    problems.append(f"{where}: video unreadable ({info['error']})")
                    continue
                w, h, d = info.get("width") or 0, info.get("height") or 0, info.get("duration") or 0
                if needs_video and (not (h > w > 0) or not (3 <= d <= 90)):
                    problems.append(f"{where}: video is {w}x{h}, {d:.1f}s (needs vertical, 3-90 s)")
                    continue
                if not needs_video and not (0.5 <= d <= 140):
                    problems.append(f"{where}: video is {d:.1f}s (X / Threads: 0.5-140 s)")
                    continue
        if not time_ok(post, where):
            continue
        targets = []
        for t in post.get("targets") or []:
            tw = f"{where} -> {t.get('app')} @{t.get('account')}"
            lim = account_ok(t.get("app"), t.get("account"), tw)
            if lim is None:
                continue
            if t["app"] == "instagram" and t.get("audience", "everyone") not in ("everyone", "close_friends"):
                problems.append(f"{tw}: audience {t.get('audience')!r}")
                continue
            if t["app"] == "tiktok" and t.get("visibility", "everyone") not in ("everyone", "friends", "only_me"):
                problems.append(f"{tw}: visibility {t.get('visibility')!r}")
                continue
            if t["app"] in TEXT_LIMITS and len(cap) > TEXT_LIMITS[t["app"]]:
                problems.append(f"{tw}: text is {len(cap)} characters; {t['app']} allows {TEXT_LIMITS[t['app']]}")
                continue
            ck = (t["app"], t["account"])
            if counts.get(ck, 0) >= lim.get("posts_per_day", 3):
                problems.append(f"{tw}: over the daily limit of {lim.get('posts_per_day', 3)} posts")
                continue
            counts[ck] = counts.get(ck, 0) + 1
            targets.append(t)
        if not targets:
            if not any(p.startswith(where) for p in problems):
                problems.append(f"{where}: no valid targets")
            continue
        seen_ids.add(post["id"])
        seen_keys.add(key)
        clean_posts.append({**post, "caption": cap, "video": str(media_rel) if media_rel else None, "media_kind": kind,
                            "targets": targets})

    # ---- research
    clean_research = []
    for i, r in enumerate(plan.get("research") or []):
        where = f"research #{i + 1} ({r.get('keyword')!r})"
        lim = account_ok(r.get("app"), r.get("account"), where, VIDEO_APPS)  # X / Threads have no research flow
        if lim is None:
            continue
        kw = r.get("keyword")
        if not isinstance(kw, str) or not ASCII_LINE.match(kw) or not (2 <= len(kw) <= 60):
            problems.append(f"{where}: keyword must be 2-60 plain ASCII characters")
            continue
        if not (1 <= float(r.get("minutes", 10)) <= 30):
            problems.append(f"{where}: minutes must be 1-30")
            continue
        bad = [k for k in ("not_before", "not_after") if r.get(k) is not None and not HHMM.match(str(r[k]))]
        if bad:
            problems.append(f"{where}: {', '.join(bad)} must be HH:MM")
            continue
        r = {**r, "max_likes": min(int(r.get("max_likes", 3)), lim.get("likes_per_day", 20)),
             "max_comments": min(int(r.get("max_comments", 1)), lim.get("comments_per_day", 5))}
        clean_research.append(r)

    # ---- deletes: only test plans may delete posts
    deletes = plan.get("deletes") or []
    if deletes and not practice:
        problems.append(f"{len(deletes)} delete(s) ignored: only practice plans may delete posts")
        deletes = []
    elif any(d.get("app") not in VIDEO_APPS for d in deletes):
        problems.append("deletes for apps other than instagram / tiktok ignored (no delete flow for X / Threads)")
        deletes = [d for d in deletes if d.get("app") in VIDEO_APPS]

    return {**plan, "posts": clean_posts, "research": clean_research, "deletes": deletes}, problems
