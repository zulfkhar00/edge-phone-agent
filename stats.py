"""Read one of our posts' stats on the phone and save a snapshot to stats.db.

A post is found by its caption: the newest posts on the profile are opened one by one until the caption matches.

TikTok   live:     views / likes / comments / saves / shares from the post screen (text, real time)
         detailed: ••• -> Analytics -> "Post analysis" (a web view: read with OCR). TikTok refreshes it about
                   once a day ("Updated on <date>"), so it is only worth reading from the 24 h snapshot on.
Instagram         "<N> views" on the reel -> Reel insights (native text): views, viewers, average watch time,
                   skip rate, like/share/save/repost/comment rates, sources.

X, Threads    views / likes / replies / reposts read from the post's row on our profile (see social.read_stats; the
                  numbers come from the row's accessibility labels, missing ones stay empty; Threads shows views only in
                  its own insights screen, which is not read yet)

Usage:
  python stats.py x "first words of the post"
  python stats.py tiktok "I interrupted them and wanted to disappear" [--detailed]
  python stats.py instagram "I interrupted them and wanted to disappear"
  options: --account <handle> (switch first), --label 1h (which snapshot this is)
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from phone import SERIAL, Abort, Phone
from research import count

DB = Path(__file__).parent / "stats.db"
PKGS = {"tiktok": "com.zhiliaoapp.musically", "instagram": "com.instagram.android",
        "x": "com.twitter.android", "threads": "com.instagram.barcelona"}
TILE_SPOTS = [(119, 795), (359, 795), (599, 795), (119, 1115), (359, 1115), (599, 1115)]  # TikTok grid, newest first


def save(app: str, caption: str, data: dict, account: str | None = None, label: str | None = None) -> None:
    con = sqlite3.connect(DB)
    con.execute("create table if not exists snapshots (id integer primary key, app text, caption text, taken_at text, data text)")
    cols = [r[1] for r in con.execute("pragma table_info(snapshots)")]
    for c in ("account", "label"):
        if c not in cols:
            con.execute(f"alter table snapshots add column {c} text")
    con.execute("insert into snapshots (app, caption, taken_at, data, account, label) values (?,?,?,?,?,?)",
                (app, caption, time.strftime("%Y-%m-%d %H:%M:%S"), json.dumps(data), account, label))
    con.commit()


def seconds(text: str) -> int | None:
    """'0h:09m:03s' / '4s' / '1m 5s' / '12.3s' -> seconds."""
    if not text:
        return None
    t = text.replace("O", "0").replace("o", "0")
    parts = dict((u, float(v)) for v, u in re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", t))
    if not parts:
        return None
    return round(parts.get("h", 0) * 3600 + parts.get("m", 0) * 60 + parts.get("s", 0))


def pct(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text or "")
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------- OCR
def ocr(png: str) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Text + box (x, y, w, h, origin top-left, 0..1). macOS Vision on the Mac; tesseract on the Linux box."""
    if sys.platform == "darwin":
        from ocrmac import ocrmac
        out = []
        for text, _conf, (x, y, w, h) in ocrmac.OCR(png, recognition_level="accurate").recognize():
            out.append((text, (x, 1 - y - h, w, h)))  # Vision boxes start bottom-left
        return out
    import subprocess
    tsv = subprocess.run(["tesseract", png, "-", "tsv"], capture_output=True, text=True).stdout
    out = []
    for row in tsv.splitlines()[1:]:
        f = row.split("\t")
        if len(f) == 12 and f[11].strip():
            out.append((f[11], (int(f[6]) / 720, int(f[7]) / 1612, int(f[8]) / 720, int(f[9]) / 1612)))
    return out


def value_under(items, label: str) -> str:
    """The text directly below a label (TikTok's metric cards: label on top, value under it)."""
    lab = next((b for t, b in items if t.strip().lower() == label.lower()), None)
    if lab is None:
        return ""
    below = [(t, b) for t, b in items if 0 < b[1] - lab[1] < 0.06 and abs(b[0] - lab[0]) < 0.05]
    return min(below, key=lambda tb: tb[1][1])[0] if below else ""


def value_right(items, label: str) -> str:
    """The text on the same row, to the right (TikTok's traffic sources: name ... 93.0%)."""
    lab = next((b for t, b in items if t.strip().lower() == label.lower()), None)
    if lab is None:
        return ""
    row = [(t, b) for t, b in items if abs(b[1] - lab[1]) < 0.015 and b[0] > lab[0] + 0.2]
    return row[0][0] if row else ""


# ---------------------------------------------------------------- TikTok
def tt_open_post(p: Phone, caption: str) -> None:
    p.d.app_start(PKGS["tiktok"], stop=True)
    ns = p.wait("TikTok home", "Home", "Profile", timeout=30)
    p.tap_node(next(n for n in ns if n["label"] == "Profile" and n["box"][1] > p.h * 0.88), "Profile")
    time.sleep(4)
    for tab in ("Videos", "Private videos"):  # "Only me" posts live under Private videos
        t = p.find(tab)
        if t is None:
            continue
        p.tap_node(t, f"{tab} tab")
        time.sleep(3)
        for i, (x, y) in enumerate(TILE_SPOTS):
            p.tap(x, y, f"{tab} tile {i + 1}")
            time.sleep(4)
            p.wake()
            if p.find(re.compile(re.escape(caption[:30]))):
                return
            if p.find("Profile menu"):  # no tile there (fewer posts than spots): still on the profile
                break
            p.d.press("back")
            time.sleep(2)
    p.fail(f"no post with caption {caption[:30]!r} among the newest {len(TILE_SPOTS)}")


def tt_live(p: Phone) -> dict:
    ns = p.nodes()
    lab = lambda pat: next((n["label"] for n in ns if re.match(pat, n["label"])), "")
    fav = next((n for n in ns if n["label"].startswith("Add or remove this video from Favorites")), None)
    saves = next((n["label"] for n in ns if fav and re.fullmatch(r"[\d.,]+[KM]?", n["label"])
                  and fav["box"][1] < n["box"][1] < fav["box"][3] + 30), "")
    return {
        "views": count(lab(r"^\d[\d.,]*[KM]? views?$")),
        "likes": count(lab(r"^Like video\.")),
        "comments": count(lab(r"^Read or add comments\.")) or 0,
        "saves": count(saves) or 0,
        "shares": count(lab(r"^Share video\.")) or 0,
    }


def tt_detailed(p: Phone) -> dict:
    # The ••• button on our own post has no label; the music disc just below it does nothing useful, so tap the
    # exact spot and confirm the share sheet (with "Analytics") opened before going on.
    ns = p.nodes()
    fav = next((n for n in ns if n["label"].startswith("Add or remove this video from Favorites")), None)
    sound = next((n for n in ns if n["box"][0] >= 600 and re.match(r"^(Original sound|Sound:)", n["label"])), None)
    if fav is None or sound is None:
        p.fail("could not place the ••• button (no Favorites / sound buttons)")
    # ••• sits in the right column between the saves count and the sound disc; banners shift it up or down
    y = (fav["box"][3] + 30 + sound["box"][1]) // 2
    p.tap(667, y, "••• (more)")
    time.sleep(2.5)
    if not p.find("Analytics"):
        p.d.press("back")
        p.fail("the ••• menu with Analytics did not open")
    p.tap_node(p.find("Analytics"), "Analytics")
    time.sleep(7)
    shots = []
    for i in range(3):
        p.wake()
        path = str(Path(__file__).parent / "runs" / f"tt_analysis_{i}.png")
        p.d.screenshot(path)
        shots.append(path)
        p.d.swipe(360, 1400, 360, 500, 0.8)
        time.sleep(2.5)
    pages = [ocr(s) for s in shots]  # positions only make sense within one screenshot

    def under(label: str) -> str:
        return next((v for v in (value_under(pg, label) for pg in pages) if v), "")

    def right(label: str) -> str:
        return next((v for v in (value_right(pg, label) for pg in pages) if v), "")

    joined = " ".join(t for pg in pages for t, _ in pg)
    upd = re.search(r"Updated on ([\d/]+)", joined)
    sources = {k: pct(right(k)) for k in ("For You", "Search", "Personal profile", "Following", "Sound", "Other")}
    data = {
        "updated_on": upd.group(1) if upd else None,
        "post_views": count(under("Post views")),
        "total_play_time_s": seconds(under("Total play time")),
        "average_watch_time_s": seconds(under("Average watch time")),
        "watched_full_video_pct": pct(under("Watched full video")),
        "new_followers": count(under("New followers")),
        "photos_viewed": under("Photos viewed") or None,
        "traffic_sources_pct": {k: v for k, v in sources.items() if v is not None},
    }
    p.d.press("back")
    time.sleep(2)
    return data


# ---------------------------------------------------------------- Instagram
def ig_open_post(p: Phone, caption: str) -> None:
    import instagram_upload as ig
    p.d.app_start(PKGS["instagram"], stop=True)
    p.wait("Instagram home", "Home", "Profile", timeout=30)
    ig.open_profile_reels(p)
    tiles = sorted((n for n in p.nodes() if n["label"].startswith("Reel by ")), key=lambda n: (n["box"][1], n["box"][0]))
    for i, t in enumerate(tiles[:6]):
        p.tap_node(t, f"reel tile {i + 1}")
        time.sleep(4)
        p.wake()
        if p.find(re.compile(re.escape(caption[:30]))):
            return
        p.d.press("back")
        time.sleep(2)
    p.fail(f"no reel with caption {caption[:30]!r} among the newest 6")


def ig_insights(p: Phone) -> dict:
    # "<N> views" under the reel; a brand-new reel has none yet, only an "Insights" button (bottom right)
    # Layouts seen: "<N> views", an "Insights" button (brand-new reel), or a "<N> • View insights" bar.
    # Never tap "Boost reel" next to it (paid promotion).
    views = p.find(re.compile(r"^\d[\d.,]*[KM]? views?$")) or p.find("Insights") or p.find(re.compile(r"View insights$"))
    if views and "Boost" in views["label"]:
        p.fail("refusing to tap a Boost button")
    p.tap_node(views, "views / Insights (opens insights)")
    p.wait("Reel insights", "Reel insights")
    seen: list[str] = []
    for _ in range(6):
        for n in p.nodes():
            if n["label"] and (not seen or n["label"] != seen[-1]):
                seen.append(n["label"])
        p.wake()
        p.d.swipe(360, 1350, 360, 450, 0.6)
        time.sleep(2)

    def after(label: str) -> str:
        return seen[seen.index(label) + 1] if label in seen and seen.index(label) + 1 < len(seen) else ""

    data = {
        "views": count(after("Views")),
        "viewers": count(after("Viewers")),
        "average_watch_time_s": seconds(after("Average watch time")),
        "follows": count(after("Follows")),
        "skip_rate_pct": pct(after("Skip rate")),
        "share_rate_pct": pct(after("Share rate")),
        "like_rate_pct": pct(after("Like rate")),
        "save_rate_pct": pct(after("Save rate")),
        "repost_rate_pct": pct(after("Repost rate")),
        "comment_rate_pct": pct(after("Comment rate")),
        "sources_pct": {k: pct(after(k)) for k in ("Reels tab", "Explore", "Profile", "Feed", "Stories", "Other")
                        if pct(after(k)) is not None},
    }
    p.d.press("back")
    time.sleep(1.5)
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("app", choices=PKGS)
    ap.add_argument("caption", help="start of the post's caption")
    ap.add_argument("--detailed", action="store_true", help="TikTok: also read Post analysis (updates daily)")
    ap.add_argument("--account", help="switch to this already-logged-in account first")
    ap.add_argument("--label", help="snapshot name, e.g. 1h / 24h")
    a = ap.parse_args()
    p = Phone(SERIAL)
    p.wake()
    try:
        if a.account:
            import accounts
            accounts.switch(p, a.app, a.account)
        if a.app in ("x", "threads"):
            import social
            spec = social.SPECS[a.app]
            if not a.account:
                raise Abort("x / threads stats need --account (the handle is checked before reading)")
            data = social.read_stats(p, spec, a.caption)
        elif a.app == "tiktok":
            tt_open_post(p, a.caption)
            data = {"live": tt_live(p)}
            if a.detailed:
                data["detailed"] = tt_detailed(p)
        else:
            ig_open_post(p, a.caption)
            data = ig_insights(p)
    except Abort as e:
        print(f"ABORTED: {e}", flush=True)
        sys.exit(1)
    save(a.app, a.caption, data, a.account, a.label)
    print(json.dumps(data, indent=1))
    print("RESULT " + json.dumps(data), flush=True)


if __name__ == "__main__":
    main()
