"""X / Threads inside the engine: plan validation, job creation, the command line a job runs, "needs login" handling,
the login pill in /api/state and the preflight. Fake adb, fake flow scripts, made-up handles."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pytest

import phones
from conftest import BASE
from plan_check import check_plan

CONFIG = BASE + """
[accounts.x.brand]
use = "noswipe"
posts_per_day = 2

[accounts.x.scratch]
use = "practice"
posts_per_day = 3

[accounts.threads.brand]
use = "noswipe"
posts_per_day = 3

[[phones]]
id = "left"
hw_serial = "HW-LEFT"
accounts = ["ig:@alpha", "x:@brand", "threads:@brand", "x:@scratch"]

[[phones]]
id = "right"
hw_serial = "HW-RIGHT"
accounts = ["ig:@beta"]
"""


# ================================================================ plan_check
TODAY = date(2026, 11, 3)
CFG = {"accounts": {"x": {"brand": {"use": "noswipe", "posts_per_day": 2}}, "threads": {"brand": {"use": "noswipe", "posts_per_day": 3}},
                    "instagram": {"alpha": {"use": "noswipe"}}}}


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "pic.jpg").write_bytes(b"\xff\xd8" + b"0" * 5000)
    (tmp_path / "videos" / "doc.pdf").write_bytes(b"%PDF" + b"0" * 5000)
    return tmp_path


def run(folder, posts, **plan):
    return check_plan({"day": "2026-11-03", "posts": posts, **plan}, folder, CFG, set(), {}, TODAY)


def tpost(**kw):
    p = {"id": "t-1", "at": "13:11", "text": "Short text post, no media at all.",
         "targets": [{"app": "x", "account": "brand"}, {"app": "threads", "account": "brand"}]}
    p.update(kw)
    return p


def test_text_only_post_for_x_and_threads_is_valid(folder):
    clean, problems = run(folder, [tpost()])
    assert problems == [] and len(clean["posts"]) == 1
    post = clean["posts"][0]
    assert post["caption"] == "Short text post, no media at all." and post["video"] is None  # `text` is accepted for `caption`
    assert [t["app"] for t in post["targets"]] == ["x", "threads"]


def test_a_very_short_text_is_fine_for_text_apps_but_not_for_video_apps(folder):
    clean, problems = run(folder, [tpost(text="gm")])
    assert problems == [] and len(clean["posts"]) == 1
    clean, problems = run(folder, [tpost(text="gm", targets=[{"app": "instagram", "account": "alpha"}])])
    assert clean["posts"] == [] and problems


def test_x_limit_280_threads_limit_500_apply_per_target(folder):
    text = "w" * 300
    clean, problems = run(folder, [tpost(text=text)])
    assert [t["app"] for t in clean["posts"][0]["targets"]] == ["threads"]
    assert "x allows 280" in problems[0]
    clean, problems = run(folder, [tpost(text="w" * 501)])
    assert clean["posts"] == [] and any("threads allows 500" in p for p in problems)


def test_image_media_ok_for_text_apps_not_for_video_apps(folder):
    clean, problems = run(folder, [tpost(media="videos/pic.jpg")])
    assert problems == [] and clean["posts"][0]["media_kind"] == "image" and clean["posts"][0]["video"] == "videos/pic.jpg"
    clean, problems = run(folder, [tpost(media="videos/pic.jpg", targets=[{"app": "instagram", "account": "alpha"}])])
    assert clean["posts"] == [] and "need a video" in problems[0]


@pytest.mark.parametrize("media", ["videos/doc.pdf", "videos/missing.jpg", "../x.jpg"])
def test_bad_media_is_dropped(folder, media):
    clean, problems = run(folder, [tpost(media=media)])
    assert clean["posts"] == [] and problems


def test_x_daily_limit_and_unknown_account(folder):
    posts = [tpost(id=f"p{i}", text=f"Distinct text number {i} goes here", targets=[{"app": "x", "account": "brand"}]) for i in range(3)]
    clean, problems = run(folder, posts)
    assert len(clean["posts"]) == 2 and "daily limit of 2" in problems[0]
    clean, problems = run(folder, [tpost(targets=[{"app": "x", "account": "nobody"}])])
    assert clean["posts"] == [] and "not in config.toml" in problems[0]


def test_text_posts_need_non_ascii_free_single_line(folder):
    for bad in ("two\nlines", "emoji \U0001F496 here"):
        clean, _ = run(folder, [tpost(text=bad)])
        assert clean["posts"] == []


def test_no_research_or_deletes_for_x_and_threads(folder):
    r = [{"app": "x", "account": "brand", "keyword": "something", "minutes": 5}]
    clean, problems = run(folder, [], research=r)
    assert clean["research"] == [] and "unknown app" in problems[0]
    d = [{"app": "x", "account": "brand", "caption": "x", "at": "12:00"}]
    clean, problems = run(folder, [], deletes=d, practice=True)
    assert clean["deletes"] == [] and "no delete flow" in problems[0]


def test_video_plans_still_need_a_vertical_video(folder):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30", "-t", "4", "-pix_fmt", "yuv420p",
                    str(folder / "videos" / "wide.mp4")], check=True)
    wide = [{"app": "instagram", "account": "alpha"}]
    clean, _ = run(folder, [tpost(media="videos/wide.mp4", targets=wide, text="A long enough caption #tag")])
    assert clean["posts"] == []  # horizontal: not for Instagram
    clean, problems = run(folder, [tpost(media="videos/wide.mp4", targets=[{"app": "x", "account": "brand"}])])
    assert problems == [] and len(clean["posts"]) == 1  # but fine for X


# ================================================================ engine
@pytest.fixture
def eng(make_engine, adb):
    e = make_engine(CONFIG)
    adb.plug("HW-LEFT")
    adb.plug("HW-RIGHT")
    for r in e.RUNNERS:
        r.adb = adb
    return e


def add(eng, **kw):
    con = eng.db()
    kw.setdefault("run_at", time.time() - 1)
    kw.setdefault("label", "t")
    jid = eng.add_job(con, plan="p", post_key="p/x/x", phone_id="left", **kw)
    con.commit()
    return jid


def job(eng, jid):
    return dict(eng.db().execute("select * from jobs where id=?", (jid,)).fetchone())


def test_x_and_threads_accounts_parse_and_are_automated(eng):
    assert phones.owner_of(eng.PHONES, "x", "brand").id == "left"
    assert phones.owner_of(eng.PHONES, "threads", "BRAND").id == "left"
    st = eng.build_state()
    accts = {(a["app"], a["account"]): a for a in st["phones"][0]["accounts"]}
    for k in (("x", "brand"), ("threads", "brand")):
        assert accts[k]["automated"] is True and accts[k]["login"] == "unknown" and accts[k]["used"]["posts"] == 0
    assert accts[("x", "brand")]["limits"]["posts_per_day"] == 2


def test_plan_import_makes_post_jobs_with_text_and_media(eng, monkeypatch, tmp_path):
    d = eng.INBOX / "d1"
    (d / "videos").mkdir(parents=True)
    (d / "videos" / "pic.jpg").write_bytes(b"\xff\xd8" + b"0" * 5000)
    plan = {"day": "2026-11-20", "stats_offsets_min": [60],
            "posts": [{"id": "t1", "at": "13:00", "text": "Hello from the rig, a text post.", "media": "videos/pic.jpg",
                       "targets": [{"app": "x", "account": "brand"}, {"app": "threads", "account": "brand"}]},
                      {"id": "t2", "at": "14:00", "text": "Plain text only post here.", "targets": [{"app": "x", "account": "brand"}]}]}
    (d / "plan.json").write_text(json.dumps(plan))
    monkeypatch.setattr(eng, "check_plan", lambda raw, folder, cfg, existing, per_day: (
        {**raw, "posts": [{**p, "caption": p["text"], "video": p.get("media")} for p in raw["posts"]]}, []))
    eng.import_plans()
    jobs = [dict(r) for r in eng.db().execute("select * from jobs where kind='post' order by run_at, app")]
    assert [(j["app"], j["phone_id"]) for j in jobs] == [("threads", "left"), ("x", "left"), ("x", "left")]
    a = json.loads(jobs[1]["args"])
    assert a["text"] == a["caption"] == "Hello from the rig, a text post." and a["media"].endswith("pic.jpg") and "video" not in a
    assert json.loads(jobs[2]["args"])["media"] is None


def test_command_for_x_and_threads_posts(eng):
    r = eng.RUNNER_BY_ID["left"]
    x = job(eng, add(eng, kind="post", app="x", account="brand", args={"text": "hi there", "caption": "hi there", "media": None}))
    cmd = r.command(x, None)
    assert cmd[1:] == ["x_post.py", "hi there", "--account", "brand"]
    t = job(eng, add(eng, kind="post", app="threads", account="brand", args={"text": "hi", "caption": "hi", "media": "/m/p.jpg"}))
    assert r.command(t, None)[1:] == ["threads_post.py", "hi", "--account", "brand", "--media", "/m/p.jpg"]
    s = job(eng, add(eng, kind="stats", app="x", account="brand", label="1h", args={"caption": "hi there", "offset_min": 60}))
    assert r.command(s, None)[1:5] == ["stats.py", "x", "hi there", "--account"]


def script(eng, monkeypatch, code):
    monkeypatch.setattr(eng.Runner, "command", lambda self, j, minutes: [sys.executable, "-c", code])


async def test_needs_login_fails_at_once_alerts_and_shows_in_the_dashboard(eng, monkeypatch):
    left = eng.RUNNER_BY_ID["left"]
    script(eng, monkeypatch, "import sys; print('ABORTED: needs login: X @brand: the app shows its log-in screen'); sys.exit(1)")
    jid = add(eng, kind="post", app="x", account="brand", args={"text": "hello world", "caption": "hello world", "media": None})
    await left.step()
    j = job(eng, jid)
    assert j["status"] == "failed" and j["attempts"] == 1  # no retry in 5 min: nothing a retry can fix
    alerts = [dict(r) for r in eng.db().execute("select * from alerts")]
    assert len(alerts) == 1 and alerts[0]["kind"] == "login" and "x @brand needs login" in alerts[0]["text"]
    assert "Take over" in alerts[0]["text"]
    accts = {(a["app"], a["account"]): a for a in eng.build_state()["phones"][0]["accounts"]}
    assert accts[("x", "brand")]["login"] == "needs_login"
    assert eng.db().execute("select count(*) from jobs where kind='stats'").fetchone()[0] == 0  # nothing was posted


async def test_successful_post_marks_login_ok_and_schedules_stats(eng, monkeypatch):
    left = eng.RUNNER_BY_ID["left"]
    script(eng, monkeypatch, "print('RESULT {\"verified\": true}')")
    jid = add(eng, kind="post", app="threads", account="brand", args={"text": "hello world", "caption": "hello world",
                                                                    "media": None, "stats_offsets_min": [60]})
    await left.step()
    assert job(eng, jid)["status"] == "done"
    assert eng.db().execute("select count(*) from jobs where kind='stats' and app='threads'").fetchone()[0] == 1
    accts = {(a["app"], a["account"]): a for a in eng.build_state()["phones"][0]["accounts"]}
    assert accts[("threads", "brand")]["login"] == "ok" and accts[("threads", "brand")]["used"]["posts"] == 1


async def test_failure_after_post_tap_is_unverified_for_x_too(eng, monkeypatch):
    left = eng.RUNNER_BY_ID["left"]
    script(eng, monkeypatch, "import sys; print('  tap Post at (10,20)'); print('ABORTED: not on the X profile'); sys.exit(1)")
    jid = add(eng, kind="post", app="x", account="brand", args={"text": "hello world", "caption": "hello world", "media": None})
    await left.step()
    assert job(eng, jid)["status"] == "unverified"  # never re-posted


async def test_daily_post_limit_covers_x(eng, monkeypatch):
    left = eng.RUNNER_BY_ID["left"]
    script(eng, monkeypatch, "print('RESULT {}')")
    ids = [add(eng, kind="post", app="x", account="brand", args={"text": f"post {i} here", "caption": f"post {i} here", "media": None,
                                                                 "stats_offsets_min": [60]}) for i in range(3)]
    for _ in range(3):
        await left.step()
    assert [job(eng, i)["status"] for i in ids] == ["done", "done", "skipped"]  # posts_per_day = 2


def test_startup_warns_when_a_phone_lists_x_without_a_limits_section(make_engine):
    cfg = CONFIG.replace('accounts = ["ig:@beta"]', 'accounts = ["ig:@beta", "x:@ghost"]')
    eng = make_engine(cfg)
    eng.startup_checks()
    texts = [r["text"] for r in eng.db().execute("select text from alerts")]
    assert any("x @ghost" in t and "[accounts.x.ghost]" in t for t in texts)


# ================================================================ preflight
def load_preflight(make_engine, monkeypatch):
    make_engine(CONFIG)
    spec = importlib.util.spec_from_file_location("preflight_social", Path(__file__).parent.parent / "preflight.py")
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)
    monkeypatch.setenv("PHONE_ID", "left")
    return pf


def test_preflight_checks_x_and_threads_logins(make_engine, monkeypatch, tmp_path):
    pf = load_preflight(make_engine, monkeypatch)
    pf.ROOT = tmp_path  # engine.db of the throwaway engine lives here
    seen = []

    class R:
        def __init__(self, ok):
            self.returncode = 0 if ok else 1
            self.stdout = "  X now on @brand\n" if ok else "ABORTED: needs login: Threads @brand: the app shows its log-in screen\n"

    monkeypatch.setattr(pf.subprocess, "run", lambda cmd, **kw: seen.append(cmd[2:]) or R(cmd[2] != "threads"))
    problems, info = [], {}
    pf.check_accounts(problems, info)
    assert ["x", "brand"] in seen and ["threads", "brand"] in seen and ["x", "scratch"] not in seen  # practice accounts are not checked
    assert info["x@brand"] == "ok" and info["threads@brand"] == "FAILED"
    assert len(problems) == 1 and "threads @brand needs login on phone left" in problems[0] and "Take over" in problems[0]
    states = {r[0]: json.loads(r[1]) for r in __import__("sqlite3").connect(tmp_path / "engine.db").execute("select k, v from state")}
    assert states["login:x:brand"]["status"] == "ok" and states["login:threads:brand"]["status"] == "needs_login"


def test_preflight_wants_x_installed_only_on_phones_that_own_x_accounts(make_engine, monkeypatch):
    pf = load_preflight(make_engine, monkeypatch)
    packages = "package:com.zhiliaoapp.musically\npackage:com.instagram.android\n"  # no X, no Threads installed

    def fake_adb(*args, timeout=20):
        return {"get-state": "device"}.get(args[0], packages if args[:2] == ("shell", "pm") else "")

    monkeypatch.setattr(pf, "adb", fake_adb)
    problems = []
    pf.check_phone(problems, {})
    assert any("x is not installed" in p for p in problems) and any("threads is not installed" in p for p in problems)
    monkeypatch.setenv("PHONE_ID", "right")  # this phone has no X / Threads accounts
    problems = []
    pf.check_phone(problems, {})
    assert problems == []
