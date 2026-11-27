"""Aggregations behind the dashboard's "Post stats" and "Research" tabs and the /api/trends export.

Pure functions over plain dicts: no phone, no network, no engine import. The engine feeds them rows from engine.db,
stats.db and research.db (load_posts / load_research); web/mock_server.py and the tests feed them fixtures.

  stats_report(posts, now, days, app, account)  -> account cards with deltas vs the prior window, best posts,
                                                   series grouping and A/B comparison
  research_report(videos, now, days, app)       -> per-keyword insight, rising themes, formats that work,
                                                   promotion ideas
  trends(videos, now, ...)                      -> the JSON served at GET /api/trends

Nothing here writes anywhere.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

DAY = 86400


# ================================================================ small helpers
def resolve_offsets(o, now: float):
    """Fixture helper: keys ending in _dt are seconds from now ("posted_at_dt": -3600 -> posted_at = now - 1 h)."""
    if isinstance(o, dict):
        return {(k[:-3] if k.endswith("_dt") else k): (now + v if k.endswith("_dt") else resolve_offsets(v, now)) for k, v in o.items()}
    if isinstance(o, list):
        return [resolve_offsets(x, now) for x in o]
    return o


def to_ts(v) -> float | None:
    """A unix time, or 'YYYY-MM-DD HH:MM:SS' (local time, how the .db files store it) -> unix time."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return time.mktime(time.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def median(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return float(statistics.median(xs)) if xs else None


def mean(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def r1(x):
    return None if x is None else round(x, 1)


def trunc(s: str | None, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def delta(cur, prev) -> dict:
    """{value, prev, diff, pct}: diff is absolute, pct relative to prev (None when prev is 0 or missing)."""
    d = {"value": cur, "prev": prev, "diff": None, "pct": None}
    if cur is not None and prev is not None:
        d["diff"] = round(cur - prev, 2)
        if prev:
            d["pct"] = round((cur - prev) / prev * 100, 1)
    return d


# ================================================================ loading from the databases
def load_posts(engine_con: sqlite3.Connection, stats_db: Path, limit: int = 600) -> list[dict]:
    """Posts we made (jobs kind=post, done/unverified) with their stats snapshots. Raw: see stats_report for the maths."""
    snaps: dict[tuple, list] = {}
    if stats_db.exists():
        sc = sqlite3.connect(stats_db)
        sc.row_factory = sqlite3.Row
        cols = [r[1] for r in sc.execute("pragma table_info(snapshots)")]
        if "label" in cols:
            for r in sc.execute("select * from snapshots where label is not null order by taken_at"):
                try:
                    data = json.loads(r["data"])
                except (TypeError, ValueError):
                    continue
                snaps.setdefault((r["app"], r["account"], r["caption"]), []).append(
                    {"label": r["label"], "taken_at": r["taken_at"], "data": data})
        sc.close()
    out = []
    prev_factory = engine_con.row_factory
    engine_con.row_factory = sqlite3.Row
    try:
        for j in engine_con.execute("select * from jobs where kind='post' and status in ('done','unverified') "
                                    "order by coalesce(ended_at, started_at) desc limit ?", (limit,)):
            a = json.loads(j["args"] or "{}")
            out.append({"id": j["id"], "app": j["app"], "account": j["account"], "label": j["label"],
                        "caption": a.get("caption", ""), "posted_at": j["ended_at"] or j["started_at"], "status": j["status"],
                        "phone_id": j["phone_id"], "snapshots": snaps.get((j["app"], j["account"], a.get("caption", "")), [])})
    finally:
        engine_con.row_factory = prev_factory
    return out


def load_research(research_db: Path, limit: int = 6000) -> list[dict]:
    if not research_db.exists():
        return []
    rc = sqlite3.connect(research_db)
    rc.row_factory = sqlite3.Row
    cols = [r[1] for r in rc.execute("pragma table_info(videos)")]
    extra = ", ".join(c if c in cols else f"null as {c}" for c in ("commented", "account", "shares", "sound"))
    rows = [dict(r) for r in rc.execute(
        f"select id, app, keyword, seen_at, creator, caption, likes, comments, relevant, liked, {extra} "
        "from videos order by id desc limit ?", (limit,))]
    rc.close()
    return rows


# ================================================================ post stats
SERIES_RE = re.compile(r"^(?P<s>.+?)(?:[-_](?P<n>\d+))?(?:[-_](?P<v>[abAB]))?$")


def parse_label(label: str | None) -> dict:
    """'whites-2-b' -> series whites, part 2, variant b. 'solo' -> no series (a one-off). 'video-01' -> series video, part 1."""
    m = SERIES_RE.match((label or "").strip())
    if not m or not label:
        return {"series": None, "part": None, "variant": None}
    s, n, v = m.group("s"), m.group("n"), m.group("v")
    if n is None and v is None:
        return {"series": None, "part": None, "variant": None}
    return {"series": s.lower(), "part": int(n) if n is not None else None, "variant": v.lower() if v else None}


def label_hours(label: str | None) -> float | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([hd])", (label or "").strip().lower())
    return float(m.group(1)) * (24 if m.group(2) == "d" else 1) if m else None


def snap_metrics(app: str, data: dict) -> dict:
    """One snapshot's data -> the same metric names for both apps (None = not measured for this app)."""
    if app == "tiktok":
        live, det = data.get("live") or {}, data.get("detailed") or {}
        views = [v for v in (live.get("views"), det.get("post_views")) if v is not None]
        return {"views": max(views) if views else None, "likes": live.get("likes"), "comments": live.get("comments"),
                "shares": live.get("shares"), "saves": live.get("saves"),
                "avg_watch_s": det.get("average_watch_time_s"), "skip_pct": None,
                "follows": det.get("new_followers"), "full_watch_pct": det.get("watched_full_video_pct")}
    if app in ("x", "threads"):  # social.read_stats: counts off the post's row (replies = comments, reposts = shares)
        return {"views": data.get("views"), "likes": data.get("likes"), "comments": data.get("replies"),
                "shares": data.get("reposts"), "saves": data.get("bookmarks"), "avg_watch_s": None, "skip_pct": None,
                "follows": None, "full_watch_pct": None}
    return {"views": data.get("views"), "likes": None, "comments": None, "shares": None, "saves": None,
            "avg_watch_s": data.get("average_watch_time_s"), "skip_pct": data.get("skip_rate_pct"),
            "follows": data.get("follows"), "full_watch_pct": None}


def enrich_post(p: dict) -> dict:
    """Raw post (with snapshots) -> flat record: latest metrics, sparkline points, series/part/variant, state."""
    posted = to_ts(p.get("posted_at"))
    by_label: dict[str, dict] = {}
    for s in p.get("snapshots") or []:  # keep the newest snapshot per label
        by_label[s["label"]] = s
    points = []
    for lab, s in by_label.items():
        m = snap_metrics(p["app"], s.get("data") or {})
        taken = to_ts(s.get("taken_at"))
        hours = label_hours(lab)
        if hours is None and taken is not None and posted is not None:
            hours = round((taken - posted) / 3600, 1)
        points.append({"label": lab, "hours": hours, "taken_at": taken, **m})
    points.sort(key=lambda x: (x["hours"] is None, x["hours"] or 0))
    spark = [{"label": x["label"], "hours": x["hours"], "views": x["views"]} for x in points if x["views"] is not None]
    latest: dict = {}
    for x in points:  # latest = the last snapshot that has each metric (TikTok's detailed numbers only exist from 24 h)
        for k in ("views", "likes", "comments", "shares", "saves", "avg_watch_s", "skip_pct", "follows", "full_watch_pct"):
            if x.get(k) is not None:
                latest[k] = x[k]
    state = "unverified" if p.get("status") == "unverified" else ("ok" if "views" in latest else "no_stats")
    caption = p.get("caption") or ""
    return {"id": p.get("id"), "app": p["app"], "account": p["account"], "label": p.get("label") or "", "caption": caption,
            "caption_short": trunc(caption, 90), "posted_at": posted, "status": p.get("status"), "state": state,
            **parse_label(p.get("label")), **{k: latest.get(k) for k in ("views", "likes", "comments", "shares", "saves",
                                                                          "avg_watch_s", "skip_pct", "follows", "full_watch_pct")},
            "spark": spark, "snapshots": len(points), "thumb": f"/api/post/{p['id']}/thumb.jpg" if p.get("id") is not None else None}


def window_summary(posts: list[dict]) -> dict:
    """Totals for a list of enriched posts. Posts without stats count in `posts` but not in the maths."""
    ok = [p for p in posts if p["state"] == "ok"]
    views = [p["views"] for p in ok]
    return {"posts": len(posts), "measured": len(ok), "unverified": sum(p["state"] == "unverified" for p in posts),
            "awaiting_stats": sum(p["state"] == "no_stats" for p in posts),
            "views_total": sum(views) if ok else None, "views_median": median(views),
            "views_avg": r1(mean(views)),
            "avg_watch_s": r1(mean(p["avg_watch_s"] for p in ok)), "skip_pct": r1(mean(p["skip_pct"] for p in ok)),
            "follows": sum(p["follows"] or 0 for p in ok) if any(p["follows"] is not None for p in ok) else None}


CARD_METRICS = ("posts", "views_total", "views_median", "avg_watch_s", "skip_pct", "follows")


def account_card(app: str, account: str, cur: list[dict], prev: list[dict]) -> dict:
    c, pv = window_summary(cur), window_summary(prev)
    card = {"app": app, "account": account, "summary": c, "prior": pv,
            "deltas": {k: delta(c[k], pv[k]) for k in CARD_METRICS}}
    card["state"] = ("empty" if not cur else "unverified" if c["unverified"] == len(cur) else
                     "awaiting_stats" if not c["measured"] else "ok")
    return card


def variant_stats(posts: list[dict]) -> dict:
    s = window_summary(posts)
    return {k: s[k] for k in ("posts", "measured", "views_total", "views_avg", "avg_watch_s", "skip_pct")}


def pick_winner(a, b, tie=0.05) -> tuple[str | None, float | None]:
    """Higher number wins; within 5% is a tie; None if either side has no data. Returns (winner, lift of winner in %)."""
    if a is None or b is None:
        return None, None
    hi, lo = max(a, b), min(a, b)
    if hi == 0 or (hi - lo) / hi <= tie:
        return "tie", 0.0
    return ("a" if a > b else "b"), (round((hi - lo) / lo * 100, 1) if lo else None)


def series_groups(posts: list[dict]) -> tuple[list[dict], list[dict]]:
    """(series, ab_pairs). A series = posts sharing a label prefix (whites-1-a, whites-1-b, whites-2-a ... -> 'whites').
    One-off labels (no part number and no -a/-b) are not grouped. A/B pairs = same series + part with both variants."""
    by_series: dict[str, list[dict]] = defaultdict(list)
    for p in posts:
        if p["series"]:
            by_series[p["series"]].append(p)
    series, pairs = [], []
    for name, ps in by_series.items():
        if len(ps) < 2:
            continue
        parts: dict = defaultdict(list)
        for p in ps:
            parts[p["part"]].append(p)
        entry = {"series": name, "apps": sorted({p["app"] for p in ps}), "accounts": sorted({p["account"] for p in ps}),
                 "summary": variant_stats(ps),
                 "parts": [{"part": k, "views_total": variant_stats(v)["views_total"],
                            "posts": [{"id": p["id"], "label": p["label"], "views": p["views"], "state": p["state"]} for p in v]}
                           for k, v in sorted(parts.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))]}
        va = [p for p in ps if p["variant"] == "a"]
        vb = [p for p in ps if p["variant"] == "b"]
        if va and vb:
            sa, sb = variant_stats(va), variant_stats(vb)
            w, lift = pick_winner(sa["views_avg"], sb["views_avg"])
            entry["ab"] = {"a": sa, "b": sb, "winner": w, "lift_pct": lift,
                           "enough": sa["measured"] >= 1 and sb["measured"] >= 1}
        series.append(entry)
        for part, group in parts.items():
            ga = [p for p in group if p["variant"] == "a"]
            gb = [p for p in group if p["variant"] == "b"]
            if ga and gb:
                pa, pb = ga[0], gb[0]
                w, lift = pick_winner(pa["views"] if pa["state"] == "ok" else None, pb["views"] if pb["state"] == "ok" else None)
                brief = lambda p: {k: p[k] for k in ("id", "label", "app", "account", "views", "avg_watch_s", "skip_pct", "state")}
                pairs.append({"series": name, "part": part, "a": brief(pa), "b": brief(pb), "winner": w, "lift_pct": lift})
    series.sort(key=lambda s: -(s["summary"]["views_total"] or 0))
    pairs.sort(key=lambda x: (x["series"], x["part"] or 0))
    return series, pairs


def stats_report(posts: list[dict], now: float, days: int = 7, app: str | None = None, account: str | None = None,
                 best_n: int = 10) -> dict:
    """Everything the Post stats tab shows, for the last `days` days vs the `days` before them."""
    everything = [enrich_post(p) for p in posts]
    allp = [p for p in everything if (not app or p["app"] == app) and (not account or p["account"] == account)]
    cur = [p for p in allp if p["posted_at"] is not None and now - days * DAY <= p["posted_at"] <= now]
    prev = [p for p in allp if p["posted_at"] is not None and now - 2 * days * DAY <= p["posted_at"] < now - days * DAY]
    keys = sorted({(p["app"], p["account"]) for p in cur + prev})
    cards = [account_card(a, ac, [p for p in cur if (p["app"], p["account"]) == (a, ac)],
                          [p for p in prev if (p["app"], p["account"]) == (a, ac)]) for a, ac in keys]
    ranked = sorted((p for p in cur if p["state"] == "ok"), key=lambda p: -p["views"])
    best = [{**p, "rank": i + 1} for i, p in enumerate(ranked[:best_n])]
    series, pairs = series_groups(cur)
    reason = ("no_posts" if not allp else "none_in_window" if not cur else
              "no_stats" if not any(p["state"] == "ok" for p in cur) else None)
    return {"days": days, "now": now, "filters": {"app": app, "account": account},
            "apps": sorted({p["app"] for p in everything}), "accounts": sorted({p["account"] for p in everything}),
            "empty_reason": reason, "totals": {"current": window_summary(cur), "prior": window_summary(prev)},
            "cards": cards, "best": best, "posts": sorted(cur, key=lambda p: -(p["posted_at"] or 0)),
            "series": series, "ab_pairs": pairs}


# ================================================================ research
STOP = set("""a about after again all also am an and any are as at back be because been before being but by can cant did do does
dont down for from get got had has have he her here him his how i if in into is it its just like me more most my no not now of
off on one only or other our out over really she so some than that the their them then there these they thing this those to too
up us very was we were what when where which who why will with would you your youre ill ive im yes yeah gonna cause
video videos watch part new first time people way make made know need want much even still every ever best""".split())
GENERIC_TAGS = set("""fyp foryou foryoupage fy fypage viral viralvideo trending reels reel tiktok instagram explore explorepage
parati capcut xyzbca trend video follow like comment share dating love relationship relationships""".split())

FORMATS = [  # (id, label, regex over the lower-cased caption), in priority order: the first match is the caption's primary format
    ("pov", "POV", r"\bpov\b"),
    ("series", "Part N series", r"\bpart\s*(?:\d+|one|two|three)\b|\bpt\.?\s*\d+|\bep(?:isode)?\.?\s*\d+|\b\d+\s*/\s*\d+\b"),
    ("choice", "A or B / choice", r"\ba or b\b|\bwhich (?:one|do you|would you)\b|\bthis or that\b|\bwould you (?:rather|pick|choose|date)\b|"
                                  r"\bred flag\b|\bgreen flag\b|\boption [ab]\b|\brate (?:him|her|this|my)\b|\bvs\.?\b|\bteam [ab]\b"),
    ("storytime", "Storytime", r"\bstory ?time\b|\bso (?:this|i|my)\b|\bthe time (?:i|we|he|she)\b|\bmy ex\b|\bi (?:matched|met|went on|got ghosted|got stood)\b|"
                               r"\bhappened\b|\blet me tell you\b"),
    ("explainer", "Tips / explainer", r"\bhow to\b|\btips?\b|\bways to\b|\bsteps?\b|\breasons?\b|\brules?\b|\bguide\b|\bhacks?\b|\bmistakes?\b|"
                                      r"\bsigns?\b|\badvice\b|\b\d+ (?:things|ways|tips|signs|rules|reasons)\b|\bwhy\b|\bexplained?\b"),
    ("lines", "Openers / lines", r"\brizz\b|\bpick ?up lines?\b|\bopeners?\b|\bwhat to (?:say|text)\b|\btext(?:s|ing)? (?:him|her|them)\b|\bdm\b|\breply\b"),
    ("relatable", "Relatable / 'when you'", r"\bwhen you\b|\bme when\b|\btell me you\b|\bthat moment\b|\bnobody talks\b|\bthe way\b|\bus when\b|"
                                            r"\b(?:girls|guys|boys) when\b|\bme:\s"),
    ("question", "Question hook", r"\?"),
]
FORMAT_LABEL = {f[0]: f[1] for f in FORMATS} | {"other": "Other"}
FORMAT_RE = [(f[0], re.compile(f[2])) for f in FORMATS]

CTAS = [  # (id, label, regex)
    ("comment", "asks for a comment", r"\bcomment\b|\btell me\b|\bdrop a\b|\blet me know\b|\byour turn\b|\bwhat would you\b|\bbe honest\b"),
    ("follow", "follow for part 2", r"\bfollow (?:for|me)\b|\bpart (?:2|two)\b|\bnext part\b"),
    ("link", "link in bio", r"\blink (?:in|on) (?:my )?bio\b|\blink below\b"),
    ("save_share", "save / share / tag", r"\bsave this\b|\bshare (?:this )?with\b|\bsend this\b|\btag (?:a|your|someone)\b"),
    ("question", "ends on a question", r"\?\s*(?:#\S+\s*)*$"),
]
CTA_LABEL = {c[0]: c[1] for c in CTAS} | {"none": "no explicit CTA"}
CTA_RE = [(c[0], re.compile(c[2])) for c in CTAS]


def engagement(v: dict) -> float:
    """One number per observed video: likes + 3 x comments + 5 x shares (missing counts as 0)."""
    return (v.get("likes") or 0) + 3 * (v.get("comments") or 0) + 5 * (v.get("shares") or 0)


def classify(caption: str) -> list[str]:
    cap = (caption or "").lower()
    out = [fid for fid, rx in FORMAT_RE if rx.search(cap)]
    return out or ["other"]


def primary_format(caption: str) -> str:
    return classify(caption)[0]


def cta_style(caption: str) -> str:
    cap = (caption or "").lower()
    return next((cid for cid, rx in CTA_RE if rx.search(cap)), "none")


def hashtags(caption: str) -> list[str]:
    return [t for t in dict.fromkeys(re.findall(r"#(\w{2,})", (caption or "").lower())) if t not in GENERIC_TAGS]


def words(caption: str) -> list[str]:
    plain = re.sub(r"#\w+|@\w+|https?://\S+", " ", (caption or "").lower())
    return re.findall(r"[a-z][a-z']{2,}", plain)


def phrase_themes(caption: str) -> list[str]:
    """Bigrams of two meaningful words ('dating app', 'first date')."""
    ws = words(caption)
    return sorted({f"{a} {b}" for a, b in zip(ws, ws[1:]) if a not in STOP and b not in STOP})


def themes_of(v: dict, kinds=("hashtag", "phrase")) -> list[tuple[str, str]]:
    cap = v.get("caption") or ""
    out: list[tuple[str, str]] = []
    if "hashtag" in kinds:
        out += [(t, "hashtag") for t in hashtags(cap)]
    if "phrase" in kinds:
        out += [(t, "phrase") for t in phrase_themes(cap)]
    return out


def prep_videos(videos: list[dict], now: float, days: int | None, app: str | None) -> list[dict]:
    out = []
    for v in videos:
        t = to_ts(v.get("seen_at"))
        if app and v.get("app") != app:
            continue
        if days and t is not None and t < now - days * DAY:
            continue
        out.append({**v, "_t": t, "_eng": engagement(v)})
    return out


def video_brief(v: dict, n: int = 140) -> dict:
    return {"app": v.get("app"), "creator": v.get("creator"), "caption": trunc(v.get("caption"), n),
            "likes": v.get("likes"), "comments": v.get("comments"), "shares": v.get("shares"), "keyword": v.get("keyword")}


def keyword_report(videos: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = defaultdict(list)
    for v in videos:
        by[v.get("keyword") or "?"].append(v)
    out = []
    for kw, vs in by.items():
        creators: dict[str, dict] = {}
        for v in vs:
            c = creators.setdefault(v.get("creator") or "?", {"creator": v.get("creator") or "?", "posts": 0, "likes": 0})
            c["posts"] += 1
            c["likes"] += v.get("likes") or 0
        top_c = sorted(creators.values(), key=lambda c: (-c["posts"], -c["likes"]))[:3]
        top_cap = sorted(vs, key=lambda v: -v["_eng"])[:3]
        seen = [v["_t"] for v in vs if v["_t"]]
        out.append({"keyword": kw, "apps": sorted({v.get("app") for v in vs}), "posts_seen": len(vs),
                    "median_likes": median(v.get("likes") for v in vs), "median_comments": median(v.get("comments") for v in vs),
                    "top_creators": top_c, "top_captions": [video_brief(v, 120) for v in top_cap],
                    "top_format": Counter(primary_format(v.get("caption")) for v in vs).most_common(1)[0][0],
                    "we_liked": sum(1 for v in vs if v.get("liked")), "we_commented": sum(1 for v in vs if v.get("commented")),
                    "last_seen": max(seen) if seen else None})
    out.sort(key=lambda k: (-(k["median_likes"] or 0), -k["posts_seen"]))
    return out


def theme_table(videos: list[dict], now: float, days: int, kinds=("hashtag", "phrase")) -> dict[str, dict]:
    """theme -> {kind, videos[], cur, prev, daily[14]}: cur = distinct videos in the last `days`, prev = the `days` before."""
    table: dict[str, dict] = {}
    for v in videos:
        for theme, kind in themes_of(v, kinds):
            e = table.setdefault(theme, {"theme": theme, "kind": kind, "videos": [], "cur": 0, "prev": 0, "daily": [0] * 14})
            e["videos"].append(v)
            t = v["_t"]
            if t is None:
                continue
            age = now - t
            if age <= days * DAY:
                e["cur"] += 1
            elif age <= 2 * days * DAY:
                e["prev"] += 1
            d = int(age // DAY)
            if 0 <= d < 14:
                e["daily"][13 - d] += 1
    return table


def drop_overlaps(rows: list[dict]) -> list[dict]:
    """Rows are best-first. A theme whose videos are all already covered by a stronger theme (same videos under another
    spelling: #talkingstage / 'talking stage') adds nothing."""
    kept: list[dict] = []
    for r in rows:
        ids = r["_ids"]
        if any(ids <= k["_ids"] for k in kept):
            continue
        kept.append(r)
    return kept


def rising_themes(videos: list[dict], now: float, days: int = 7, limit: int = 12) -> list[dict]:
    """Themes (hashtags and 2-word phrases) seen more in the last `days` than the `days` before.
    Needs 2+ sightings in the recent window. `new` = not seen at all before."""
    out = []
    for e in theme_table(videos, now, days).values():
        if e["cur"] < 2 or e["cur"] <= e["prev"]:
            continue
        out.append({"_ids": {id(v) for v in e["videos"]}, "theme": e["theme"], "kind": e["kind"], "recent": e["cur"], "prior": e["prev"], "new": e["prev"] == 0,
                    "growth": round((e["cur"] + 1) / (e["prev"] + 1), 2), "median_likes": median(v.get("likes") for v in e["videos"]),
                    "daily": e["daily"]})
    out.sort(key=lambda r: (-(r["recent"] - r["prior"]), -r["growth"], r["theme"]))
    out = drop_overlaps(out)
    for r in out:
        del r["_ids"]
    return out[:limit]


def format_report(videos: list[dict]) -> list[dict]:
    """'Formats that work': every caption can match several patterns; each format gets its own engagement numbers and a lift
    over the median likes of all videos (2.0 = twice the typical video)."""
    base = median(v.get("likes") for v in videos)
    by: dict[str, list[dict]] = defaultdict(list)
    for v in videos:
        for f in classify(v.get("caption")):
            by[f].append(v)
    out = []
    for fid, vs in by.items():
        ml = median(v.get("likes") for v in vs)
        best = max(vs, key=lambda v: v["_eng"])
        out.append({"id": fid, "label": FORMAT_LABEL[fid], "posts": len(vs), "share_pct": round(100 * len(vs) / max(len(videos), 1)),
                    "median_likes": ml, "median_comments": median(v.get("comments") for v in vs),
                    "lift": round(ml / base, 2) if ml is not None and base else None, "best": video_brief(best),
                    "apps": dict(Counter(v.get("app") for v in vs))})
    out.sort(key=lambda f: (f["id"] == "other", -(f["lift"] or 0), -f["posts"]))
    return out


# ---- promotion ideas: deterministic, derived from what actually performed
PRODUCT = "NoSwipe (an AI that swipes, chats and books the dates for you; you just show up)"
FORMAT_ANGLE = {
    "pov": "POV from the moment the app does the awkward part: matches, a good reply sent, a date on the calendar. The viewer is the one who just shows up.",
    "series": "A short serial: one dating problem per part, each part ends on the next date being booked by the assistant. Number the parts.",
    "choice": "Show two openers or two dates and let the viewer pick. Reveal what the assistant would have sent and what happened.",
    "storytime": "A real-moment story told in first person: the match you almost let die, then the chat the assistant kept alive and the date that came out of it.",
    "explainer": "Three concrete things that make a first message land, shown as screen moments. The assistant does them by default.",
    "lines": "Real openers that got replies, sorted by what they do (specific, playful, low-pressure). End on the one the assistant wrote for you.",
    "relatable": "Name the exact dating-app feeling in the first second, then show the thing that removes it. No stats, no pitch.",
    "question": "Open on the question the audience is already asking, answer it with one concrete moment from using the assistant.",
    "other": "Open on one concrete dating-app moment, resolve it in 20 seconds, show the assistant doing the boring part.",
}
TOPIC_ANGLE = [  # (regex over theme + keyword, angle note)
    (r"burn ?out|fatigue|tired of|swipe", "Swipe fatigue: the feeling of opening the app out of habit, and not having to anymore."),
    (r"ai |assistant|automat", "Delegation: what you hand over (swiping, small talk, scheduling) and what stays yours (showing up)."),
    (r"first date|date idea|meet cute", "The first date as the finish line: skip the endless chat and get to a place and a time."),
    (r"situationship|talking stage|ghost", "The talking stage that never ends: a chat kept moving until someone suggests a plan."),
    (r"rizz|opener|pick ?up|text", "Openers and replies: specific beats clever; show the reply that got a yes."),
    (r"hinge|tinder|bumble", "App-specific tip people already search for; end on what the assistant does in that app."),
    (r"20s|30s|dating in", "Dating with a busy life: no time to chat all day, so the chat happens without you."),
]


def topic_angle(text: str) -> str | None:
    t = text.lower()
    return next((a for rx, a in TOPIC_ANGLE if re.search(rx, t)), None)


def hook_of(caption: str, n: int = 12) -> str:
    plain = re.sub(r"#\w+|@\w+", "", caption or "")
    first = re.split(r"(?<=[.!?:])\s+|\n", plain.strip())[0]
    return trunc(" ".join(first.split()[:n]), 90)


def suggest_angle(fmt: str, theme: str | None, keyword: str | None = None) -> str:
    base = FORMAT_ANGLE.get(fmt, FORMAT_ANGLE["other"])
    topic = topic_angle(f"{theme or ''} {keyword or ''}")
    return f"{topic} {base}" if topic else base


def promotion_ideas(videos: list[dict], formats: list[dict], limit: int = 6) -> list[dict]:
    """Ideas for what to post, each backed by a real top video: its format, hook and CTA style, plus an angle for the product.
    Formats with 2+ videos and a lift above 1 come first; each idea points at the best video of its format."""
    if not videos:
        return []
    ideas = []
    for f in formats:
        if f["id"] == "other" or f["posts"] < 2 or (f["lift"] or 0) < 1:
            continue
        vs = sorted((v for v in videos if f["id"] in classify(v.get("caption"))), key=lambda v: -v["_eng"])
        top = vs[0]
        ctas = Counter(cta_style(v.get("caption")) for v in vs[:5])
        cta = next((c for c, _ in ctas.most_common() if c != "none"), "none")
        theme = (hashtags(top.get("caption")) or [None])[0]
        ideas.append({"format": f["id"], "format_label": f["label"], "theme": theme or top.get("keyword"),
                      "hook": hook_of(top.get("caption")), "cta_style": cta, "cta_label": CTA_LABEL[cta],
                      "lift": f["lift"], "evidence": video_brief(top),
                      "angle": suggest_angle(f["id"], theme, top.get("keyword")), "product": PRODUCT,
                      "score": round((f["lift"] or 1) * math.log10(2 + top["_eng"]), 2)})
    ideas.sort(key=lambda i: -i["score"])
    return ideas[:limit]


def research_report(videos: list[dict], now: float, days: int | None = 14, app: str | None = None) -> dict:
    vs = prep_videos(videos, now, days, app)
    fmts = format_report(vs)
    recent_days = 7 if (days or 14) >= 14 else max(2, (days or 14) // 2)
    return {"days": days, "now": now, "filters": {"app": app}, "apps": sorted({v.get("app") for v in videos if v.get("app")}),
            "empty_reason": "no_research" if not videos else "none_in_window" if not vs else None,
            "totals": {"videos": len(vs), "keywords": len({v.get("keyword") for v in vs}),
                       "creators": len({v.get("creator") for v in vs}), "median_likes": median(v.get("likes") for v in vs),
                       "median_comments": median(v.get("comments") for v in vs),
                       "we_liked": sum(1 for v in vs if v.get("liked")), "we_commented": sum(1 for v in vs if v.get("commented"))},
            "keywords": keyword_report(vs), "rising": rising_themes(vs, now, recent_days), "rising_days": recent_days,
            "formats": fmts, "ideas": promotion_ideas(vs, fmts)}


# ================================================================ /api/trends
def trends(videos: list[dict], now: float, days: int = 14, app: str | None = None, limit: int = 10) -> dict:
    """The export our video generator pulls: top themes (research keywords, hashtags, 2-word phrases) by engagement score.

    score (0-100, relative to the best theme in this response):
        raw = log10(1 + mean engagement of the theme's top 5 videos) x (1 + min(recent sightings, 10) / 10) x (1.25 if rising)
        engagement of a video = likes + 3 x comments + 5 x shares
    A theme needs 2+ videos from 2+ different creators. `recent` / `prior` count videos seen in the last / the previous
    half of the window, so `trending` is "rising" when recent > prior."""
    half = max(1, days // 2)
    vs = prep_videos(videos, now, days, app)
    table = theme_table(vs, now, half)
    for kw in {v.get("keyword") for v in vs if v.get("keyword")}:  # the research keywords themselves are themes
        kv = [v for v in vs if v.get("keyword") == kw]
        e = {"theme": kw.lower(), "kind": "keyword", "videos": kv, "cur": 0, "prev": 0, "daily": [0] * 14}
        for v in kv:
            if v["_t"] is not None:
                age = now - v["_t"]
                e["cur"] += age <= half * DAY
                e["prev"] += half * DAY < age <= 2 * half * DAY
        if e["theme"] in table and table[e["theme"]]["kind"] != "keyword":
            e["videos"] = list({id(v): v for v in table[e["theme"]]["videos"] + kv}.values())
        table[e["theme"]] = e
    rows = []
    for e in table.values():
        creators = {v.get("creator") for v in e["videos"]}
        if len(e["videos"]) < 2 or len(creators) < 2:
            continue
        top = sorted(e["videos"], key=lambda v: -v["_eng"])
        rising = e["cur"] > e["prev"] and e["cur"] >= 2
        raw = math.log10(1 + mean(v["_eng"] for v in top[:5])) * (1 + min(e["cur"], 10) / 10) * (1.25 if rising else 1)
        fmt = Counter(f for v in e["videos"] for f in classify(v.get("caption")) if f != "other").most_common(1)
        fid = fmt[0][0] if fmt else "other"
        rows.append({"_ids": {id(v) for v in e["videos"]}, "theme": e["theme"], "kind": e["kind"], "raw": raw, "posts_seen": len(e["videos"]), "creators": len(creators),
                     "recent": e["cur"], "prior": e["prev"], "trending": "rising" if rising else "steady",
                     "median_likes": median(v.get("likes") for v in e["videos"]), "top_format": fid,
                     "top_format_label": FORMAT_LABEL[fid],
                     "example_posts": [video_brief(v) for v in top[:3]],
                     "suggested_angle": suggest_angle(fid, e["theme"], (top[0].get("keyword"))),
                     "hook_example": hook_of(top[0].get("caption"))})
    kind_order = {"keyword": 0, "hashtag": 1, "phrase": 2}
    rows.sort(key=lambda r: (-round(r["raw"], 6), kind_order[r["kind"]], r["theme"]))
    kept = drop_overlaps(rows)
    best = kept[0]["raw"] if kept else 1
    for i, r in enumerate(kept[:limit]):
        del r["_ids"]
        r["rank"] = i + 1
        r["score"] = round(100 * r.pop("raw") / best, 1)
    fmts = [{k: f[k] for k in ("id", "label", "posts", "median_likes", "lift")} for f in format_report(vs)]
    return {"generated_at": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"), "window_days": days,
            "filters": {"app": app}, "source": {"videos": len(vs), "keywords": len({v.get("keyword") for v in vs})},
            "product": PRODUCT, "score_note": "0-100 relative to the top theme; see README 'Trend export'",
            "trends": kept[:limit], "formats": fmts}
