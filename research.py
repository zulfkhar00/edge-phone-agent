"""Research job: search a keyword, watch videos for N minutes, log stats + top comments, like relevant ones.

Everything is read from the UI tree as text (see flows/*_research.md). Per video it records creator, caption,
likes, comments, shares, sound, and the top comments, into research.db. A video counts as relevant when its
caption mentions the keyword (placeholder for an LLM judgement); up to --max-likes relevant videos get a like.

Up to --max-comments relevant videos also get a short comment written by the `claude` CLI (headless, uses the
Claude subscription) from the caption and the top comments. Plain ASCII only; never emoji, links or app mentions.

Never taps Share, Save/Favorites, Follow, the emoji row, or double-taps a video.

Usage:
  python research.py tiktok "texting anxiety" --minutes 2 --max-likes 2 --max-comments 1 [--account nomoreawkwardness]
  python research.py instagram "texting anxiety" --minutes 2 --max-likes 2
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sqlite3
import sys
import time
from pathlib import Path

from phone import SERIAL, Abort, Phone

DB = Path(__file__).parent / "research.db"
PKGS = {"tiktok": "com.zhiliaoapp.musically", "instagram": "com.instagram.android"}


def count(text: str | None) -> int | None:
    """'3,287' / '220.8K' / '1.2M' -> int."""
    if not text:
        return None
    m = re.search(r"(\d[\d.,]*)\s*([KMB]?)(?![a-z])", text)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    return int(n * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)])


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.executescript("""
    create table if not exists videos (
      id integer primary key, app text, keyword text, seen_at text, creator text, caption text,
      likes integer, comments integer, shares integer, sound text, relevant integer, liked integer,
      unique(app, creator, caption));
    create table if not exists comments (
      id integer primary key, video_id integer references videos(id), author text, text text,
      likes integer, replies integer);
    """)
    cols = [r[1] for r in con.execute("pragma table_info(videos)")]
    if "commented" not in cols:
        con.execute("alter table videos add column commented text")
    if "account" not in cols:
        con.execute("alter table videos add column account text")
    return con


def relevant(caption: str, keyword: str) -> bool:
    """Placeholder until an LLM judges relevance: any keyword stem (first 4 letters) in the caption,
    so "texting anxiety" also matches "text", "anxious"."""
    stems = {w[:4] for w in re.findall(r"\w+", keyword.lower()) if len(w) > 3}
    cap = caption.lower()
    return any(st in cap for st in stems)


def save(con: sqlite3.Connection, app: str, keyword: str, v: dict, comments: list[dict]) -> int | None:
    cur = con.execute(
        "insert or ignore into videos (app, keyword, seen_at, creator, caption, likes, comments, shares, sound, relevant,"
        " liked, commented, account) values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (app, keyword, time.strftime("%Y-%m-%d %H:%M:%S"), v["creator"], v["caption"], v["likes"], v["comments"],
         v["shares"], v["sound"], int(v["relevant"]), int(v["liked"]), v.get("commented"), v.get("account")))
    if cur.rowcount == 0:  # seen before: still record what we did this time (like / comment / account)
        con.execute("update videos set liked = max(liked, ?), commented = coalesce(?, commented), account = coalesce(?, account),"
                    " seen_at = ? where app=? and creator=? and caption=?",
                    (int(v["liked"]), v.get("commented"), v.get("account"), time.strftime("%Y-%m-%d %H:%M:%S"),
                     app, v["creator"], v["caption"]))
        con.commit()
        return None
    vid = cur.lastrowid
    con.executemany("insert into comments (video_id, author, text, likes, replies) values (?,?,?,?,?)",
                    [(vid, c["author"], c["text"], c["likes"], c["replies"]) for c in comments])
    con.commit()
    return vid


# ---------------------------------------------------------------- comments we write
def claude_bin() -> str | None:
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude") or next(
        (str(c) for c in (Path.home() / ".local/bin/claude", Path.home() / ".claude/local/claude") if c.exists()), None)


def write_comment(app: str, keyword: str, v: dict, top: list[dict]) -> str | None:
    """One short, human comment for this video, or None to skip it."""
    exe = claude_bin()
    if not exe:  # the Pi has no claude CLI: Hub writes the comment with the same rules ([hub] enabled, token in the environment)
        import hub_rig
        text = hub_rig.hub_comment(app, keyword, v["caption"], v["creator"], [c["text"] for c in top])
        print(f"  no claude CLI: {'comment from Hub' if text else 'Hub gave none, skipping the comment'}", flush=True)
        return clean_comment(text) if text else None
    others = "\n".join(f"- {c['text'][:150]}" for c in top[:5]) or "(none)"
    prompt = (
        f"You are a real person scrolling {app} who relates to videos about '{keyword}'. Write ONE comment for this video.\n"
        f"Caption: {v['caption'][:400]}\nCreator: {v['creator']}\nTop comments:\n{others}\n\n"
        "Rules: max 15 words, casual lowercase, sounds like a real viewer, specific to this video, plain ASCII only, "
        "no emoji, no hashtags, no links, no @mentions, never mention any app or product, never copy another comment. "
        "If the video is unrelated, offensive, about self-harm, or you cannot write something genuine, output SKIP. "
        "Output only the comment.")
    try:
        out = subprocess.run([exe, "-p", prompt], capture_output=True, text=True, timeout=90).stdout.strip()
    except subprocess.TimeoutExpired:
        return None
    return clean_comment(out.splitlines()[-1] if out else "")


def clean_comment(out: str) -> str | None:
    out = re.sub(r"[^\x20-\x7E]", "", out.strip().strip('"')).strip()  # adb input text is ASCII only
    if not out or out.upper().startswith("SKIP") or len(out) > 150 or re.search(r"https?:|www\.|@|#", out):
        return None
    return out


def tt_post_comment(p: Phone, v: dict, text: str) -> bool:
    p.tap_node(v["_comments_node"], "comments")
    time.sleep(3.5)
    p.wait("comment field", "Add comment...")
    p.tap_node(p.find("Add comment..."), "Add comment field")
    time.sleep(2)
    p.type_text(text)
    time.sleep(1.5)
    ns = p.nodes()
    field = next((n for n in ns if n.get("class", "").endswith("EditText") and n["label"] == text), None)
    if field is None:
        p.d(focused=True).clear_text()
        p.d.press("back"); p.d.press("back")
        p.fail("the comment text did not land in the field")
    # send = the red arrow button on the toolbar row right under the field, far right (never the emoji row above)
    send = next((n for n in ns if n.get("clickable") == "true" and n["box"][0] > 550
                 and 0 <= n["box"][1] - field["box"][3] < 80 and n["label"] not in ("Mention someone", "Stickers", "Microphone")), None)
    if send is None:
        p.d(focused=True).clear_text()
        p.d.press("back"); p.d.press("back")
        p.fail("no send button next to the comment field")
    p.tap_node(send, "send comment")
    time.sleep(4)
    ok = bool(p.find(re.compile("^" + re.escape(text[:40]))))
    close = next((n for n in p.nodes() if n["label"] == "Close" and n["box"][0] > 600 and 400 < n["box"][1] < 800), None)
    if close:
        p.tap_node(close, "close comments")
    else:
        p.d.press("back")
    time.sleep(1.5)
    return ok


def ig_post_comment(p: Phone, v: dict, text: str) -> bool:
    p.tap_node(v["_comments_node"], "comments")
    time.sleep(3.5)
    field = p.by_id("layout_comment_thread_edittext_multiline") or p.by_id("comment_composer_text_view")
    p.tap_node(field, "comment field")
    time.sleep(2)
    p.type_text(text)
    time.sleep(1.5)
    ns = p.nodes()
    field = p.by_id("layout_comment_thread_edittext_multiline", ns)
    if field is None or field["label"] != text:
        p.d(focused=True).clear_text()
        p.d.press("back"); p.d.press("back")
        p.fail("the comment text did not land in the field")
    p.tap_node(p.by_id("layout_comment_thread_post_button_icon", ns), "Post comment")
    time.sleep(4)
    ok = bool(p.find(re.compile(re.escape(text[:40]))))
    p.d.press("back")
    time.sleep(1)
    if p.by_id("layout_comment_thread_edittext_multiline"):
        p.d.press("back")
        time.sleep(1)
    return ok


# ---------------------------------------------------------------- TikTok
def tt_open(p: Phone, keyword: str) -> None:
    p.d.app_start(PKGS["tiktok"], stop=True)
    ns = p.wait("TikTok home", "Home", "Profile", timeout=30)
    p.tap_node(p.by_id("k_g", ns) or p.find("Search", ns), "Search")
    time.sleep(2)
    p.type_text(keyword)
    p.adb("shell", "input", "keyevent", "KEYCODE_ENTER")
    p.wait("search results", "Top", "Videos", timeout=20)
    time.sleep(2)
    cell = next((n for n in p.nodes() if n["label"].startswith("Video by")), None)
    p.tap_node(cell, "first result")
    time.sleep(4)


def tt_read(p: Phone) -> dict | None:
    # TikTok's resource ids are obfuscated and change between sessions, so read by label and layout only.
    ns = p.nodes()
    like = next((n for n in ns if re.match(r"^(Like video\.|Video liked)", n["label"])), None)
    if like is None:
        return None
    lab = lambda pat: next((n["label"] for n in ns if re.match(pat, n["label"])), "")
    creator = lab(r".+ profile$").removesuffix(" profile")
    caption = " ".join(n["label"] for n in ns
                       if n.get("resource-id", "").endswith((":id/title", ":id/desc")) and n["box"][1] > 1150
                       and n["label"] != creator)
    return {
        "creator": creator,
        "caption": caption.strip(),
        "likes": count(like["label"]) if like["label"].startswith("Like video")
        else count(next((n["label"] for n in ns if re.fullmatch(r"[\d.,]+[KM]?", n["label"])
                         and like["box"][1] < n["box"][1] < like["box"][3] + 30 and n["box"][0] >= like["box"][0]), "")),
        "comments": count(lab(r"^Read or add comments\.")),
        "shares": count(lab(r"^Share video\.")),
        "sound": lab(r"^Sound: |^Original sound|^Contains: ").removeprefix("Sound: "),
        "liked": like["label"] == "Video liked",
        "_like_node": like,
        "_comments_node": next((n for n in ns if n["label"].startswith("Read or add comments")), None),
    }


def tt_comments(p: Phone, v: dict, limit: int = 5) -> list[dict]:
    if not v["_comments_node"] or not v["comments"]:
        return []
    p.tap_node(v["_comments_node"], "comments")
    time.sleep(3.5)
    ns = p.nodes()
    out = []
    # Top-level comments: an author line (id "title") at the comment indent, then the text right below it.
    authors = [n for n in ns if n.get("resource-id", "").endswith(":id/title") and 100 <= n["box"][0] <= 130
               and n["box"][1] > 600 and n["label"] != "Community-flagged comments"]
    for a in authors[:limit]:
        below = sorted((n for n in ns if n["box"][0] == a["box"][0] and 0 < n["box"][1] - a["box"][3] < 20 and n["label"]),
                       key=lambda n: n["box"][1])
        window = [n for n in ns if a["box"][1] < n["box"][1] < a["box"][1] + 280]
        rep = next((n["label"] for n in window if re.match(r"^View \d+ repl", n["label"])), "")
        likes = next((n["label"] for n in window if re.fullmatch(r"[\d.,]+[KM]?", n["label"]) and n["box"][0] > 500), "")
        if below:
            out.append({"author": a["label"], "text": below[0]["label"], "likes": count(likes) or 0, "replies": count(rep) or 0})
    close = next((n for n in ns if n["label"] == "Close" and n["box"][0] > 600 and 400 < n["box"][1] < 800), None)
    if close:
        p.tap_node(close, "close comments")
    else:
        p.d.press("back")
    time.sleep(1.5)
    return out


def tt_next(p: Phone) -> None:
    before = next((n["label"] for n in p.nodes() if n["label"].endswith(" profile")), None)
    # A quick fling is sometimes ignored; a slower swipe works. Verify the creator changed, else retry.
    for args in (("360", "1050", "360", "250", "400"), ("360", "900", "360", "100", "300"), ("360", "1100", "360", "200", "600")):
        p.adb("shell", "input", "swipe", *args)
        time.sleep(3)
        p.wake()
        now = next((n["label"] for n in p.nodes() if n["label"].endswith(" profile")), None)
        if now and now != before:
            return
    print("  could not move to the next video", flush=True)


# ---------------------------------------------------------------- Instagram
def ig_open(p: Phone, keyword: str) -> None:
    p.d.app_start(PKGS["instagram"], stop=True)
    p.wait("Instagram home", "Home", "Profile", timeout=30)
    p.tap_node(p.by_id("search_tab"), "Search and explore")
    time.sleep(2)
    p.tap_node(p.by_id("action_bar_search_edit_text"), "search box")
    p.type_text(keyword)
    p.adb("shell", "input", "keyevent", "KEYCODE_ENTER")
    p.wait("search results", "For you", timeout=20)
    time.sleep(3)
    tile = next((n for n in p.nodes() if n["label"].startswith("Reel by ")), None)
    p.tap_node(tile, "first reel")
    time.sleep(4)


def ig_read(p: Phone) -> dict | None:
    ns = p.nodes()
    like = p.by_id("like_count", ns)
    if like is None:
        return None
    reel = next((n["label"] for n in ns if n["label"].startswith("Reel by ")), "")
    creator = re.sub(r"^Reel by (.+?)\. Double tap.*$", r"\1", reel)
    caption = ""
    for n in ns:  # caption sits under the author row; it is the longest plain text in the lower third
        if n["box"][1] > 1250 and n["box"][1] < 1480 and len(n["label"]) > len(caption) \
                and not re.search(r"Profile picture|Follow|Add comment|Original audio|·", n["label"]):
            caption = n["label"]
    return {
        "creator": creator,
        "caption": caption,
        "likes": count(like["label"]),
        "comments": count((p.by_id("comment_count", ns) or {}).get("label")),
        "shares": count(next((n["label"] for n in ns if n["label"].startswith("Reshare number")), "")),
        "sound": next((n["label"] for n in ns if "Original audio" in n["label"] or "·" in n["label"]), ""),
        "liked": False,
        "_like_node": p.by_id("like_button", ns),
        "_comments_node": p.by_id("comment_button", ns),
    }


def ig_comments(p: Phone, v: dict, limit: int = 5) -> list[dict]:
    if not v["_comments_node"] or not v["comments"]:
        return []
    p.tap_node(v["_comments_node"], "comments")
    time.sleep(3)
    ns = p.nodes()
    out = []
    for n in ns:
        m = re.match(r"^(\S+) said (.+)$", n["label"], re.S)
        if m:
            below = [x for x in ns if 0 <= x["box"][1] - n["box"][3] < 120]
            rep = next((x["label"] for x in below if re.match(r"^View \d+ more repl", x["label"])), "")
            out.append({"author": m.group(1), "text": m.group(2), "likes": None, "replies": count(rep) or 0})
        if len(out) >= limit:
            break
    p.d.press("back")
    time.sleep(1.5)
    return out


def ig_next(p: Phone) -> None:
    p.adb("shell", "input", "swipe", "360", "1100", "360", "300", "250")


# ---------------------------------------------------------------- loop
def run(app: str, keyword: str, minutes: float, max_likes: int, max_comments: int = 0, account: str | None = None) -> None:
    p = Phone(SERIAL)
    con = db()
    p.wake()
    if account:
        import accounts
        accounts.switch(p, app, account)
    opener, reader, commenter, nexter, poster = {
        "tiktok": (tt_open, tt_read, tt_comments, tt_next, tt_post_comment),
        "instagram": (ig_open, ig_read, ig_comments, ig_next, ig_post_comment),
    }[app]
    opener(p, keyword)
    end = time.time() + minutes * 60
    seen = likes = commented = 0
    while time.time() < end:
        p.wake()
        time.sleep(random.uniform(6, 10))  # watch
        v = reader(p)
        if v is None:
            print("  not on a video screen; skipping ahead", flush=True)
            nexter(p)
            time.sleep(3)
            continue
        v["relevant"] = relevant(v["caption"], keyword)
        cm = commenter(p, v)
        if v["relevant"] and not v["liked"] and likes < max_likes and v["_like_node"] is not None:
            before = v["likes"]
            p.tap_node(v["_like_node"], "Like")
            time.sleep(1.5)
            after = reader(p)
            v["liked"] = bool(after and (after["liked"] or (after["likes"] or 0) > (before or 0)))
            likes += v["liked"]
        v["account"] = account
        if v["relevant"] and commented < max_comments and v["_comments_node"] is not None:
            text = write_comment(app, keyword, v, cm)
            if text:
                ok = poster(p, reader(p) or v, text)
                print(f"  commented {'(seen)' if ok else '(NOT seen after sending)'}: {text}", flush=True)
                v["commented"] = text
                commented += 1
        vid = save(con, app, keyword, v, cm)
        seen += 1
        print(f"  [{seen}] @{v['creator'][:24]} · {v['likes']} likes · {v['comments']} comments · "
              f"{len(cm)} top comments · relevant={v['relevant']} liked={v['liked']}{'' if vid else ' (already logged)'}",
              flush=True)
        nexter(p)
        time.sleep(2)
    print(f"done: {seen} videos, {likes} liked, {commented} commented, saved to {DB.name}", flush=True)
    print("RESULT " + json.dumps({"videos": seen, "likes": likes, "comments": commented}), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("app", choices=PKGS)
    ap.add_argument("keyword")
    ap.add_argument("--minutes", type=float, default=2)
    ap.add_argument("--max-likes", type=int, default=2)
    ap.add_argument("--max-comments", type=int, default=0)
    ap.add_argument("--account", help="switch to this already-logged-in account first")
    a = ap.parse_args()
    try:
        run(a.app, a.keyword, a.minutes, a.max_likes, a.max_comments, a.account)
    except Abort as e:
        print(f"ABORTED: {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
