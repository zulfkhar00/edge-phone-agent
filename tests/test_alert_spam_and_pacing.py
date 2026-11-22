"""Alert spam and posting pace: a stats read that cannot find its post is recorded once (not retried, one digest alert
a day), a missing plan is one alert a day, and a backlog after downtime never turns into a burst of posts."""

# ruff: noqa: F811
from __future__ import annotations

import importlib.util
import sys
import time
from datetime import datetime
from pathlib import Path

from conftest import TWO_PHONES
from test_multiphone_engine import add, script, status, two  # noqa: F401  (fixture `two`)


def sql(eng, query, *args):
    con = eng.db()
    con.execute(query, args)
    con.commit()

REPO = Path(__file__).resolve().parent.parent


def alerts(eng, kind=None):
    q = "select * from alerts" + (" where kind=?" if kind else "")
    return [dict(r) for r in eng.db().execute(q, (kind,) if kind else ())]


# ---------------------------------------------------------------- stats: post not found
NOT_FOUND = "import sys; print('NOT_FOUND: no reel with this caption among 25 reels scanned (screenshot: runs/x.png)'); sys.exit(3)"


async def test_not_found_is_terminal_not_retried_and_one_digest_alert(two, monkeypatch):
    eng, left, _, _ = two
    script(eng, monkeypatch, NOT_FOUND)
    a = add(eng, kind="stats", app="instagram", account="alpha", phone_id="left", label="24h",
            args={"caption": "first caption", "offset_min": 1440})
    b = add(eng, kind="stats", app="instagram", account="alpha", phone_id="left", label="48h",
            args={"caption": "first caption", "offset_min": 2880})
    c = add(eng, kind="stats", app="instagram", account="alpha", phone_id="left", label="24h",
            args={"caption": "second caption", "offset_min": 1440})
    for _ in range(3):
        await left.step()
    for j in (a, b, c):
        s = status(eng, j)
        assert s["status"] == "not_found" and s["attempts"] == 1  # one run, no retry
        assert "25 reels scanned" in s["error"]
    got = alerts(eng)
    assert len(got) == 1 and got[0]["kind"] == "stats"
    assert "first caption" in got[0]["text"] and "second caption" in got[0]["text"]  # all in the one digest


async def test_not_found_does_not_show_under_needs_attention(two, monkeypatch):
    eng, left, _, _ = two
    script(eng, monkeypatch, NOT_FOUND)
    add(eng, kind="stats", app="instagram", account="alpha", phone_id="left")
    await left.step()
    html = (REPO / "web" / "dashboard.html").read_text()
    assert '["failed", "unverified"].includes(j.status)' in html  # not_found is not in that list


async def test_real_stats_failure_still_retries_then_alerts(two, monkeypatch):
    eng, left, _, _ = two
    script(eng, monkeypatch, "print('ABORTED: expected Instagram home but did not see it (screenshot: runs/y.png)'); raise SystemExit(1)")
    j = add(eng, kind="stats", app="instagram", account="alpha", phone_id="left")
    await left.step()
    assert status(eng, j)["status"] == "waiting" and not alerts(eng)  # first failure: retry, quiet
    sql(eng, "update jobs set run_at=? where id=?", time.time() - 1, j)
    await left.step()
    assert status(eng, j)["status"] == "failed" and len(alerts(eng, "job")) == 1


async def test_daily_alert_is_one_row_per_day(two):
    eng = two[0]
    eng.daily_alert("k", "x", "line one")
    eng.daily_alert("k", "x", "line two")
    eng.daily_alert("k", "x", "line two")
    rows = alerts(eng)
    assert len(rows) == 1 and rows[0]["text"] == "line one\nline two"
    eng.set_state("daily_alert:k", {"day": "2000-01-01", "id": rows[0]["id"]})  # yesterday's: a new alert today
    eng.daily_alert("k", "x", "line one")
    assert len(alerts(eng)) == 2


# ---------------------------------------------------------------- missing plan: one alert a day
PLAN_PROBLEM = "no posts planned for 2026-11-21 on phone left (did the nightly plan arrive?) Last nightly run: unknown."


async def test_missing_plan_is_one_alert_a_day_and_not_retried(two, monkeypatch):
    eng, left, _, _ = two
    script(eng, monkeypatch, f"print('ABORTED: {PLAN_PROBLEM}'); raise SystemExit(1)")
    j = add(eng, kind="preflight", app="-", account="-", phone_id="left", args={}, label="preflight")
    await left.step()
    assert status(eng, j)["status"] == "failed" and status(eng, j)["attempts"] == 1  # a retry cannot produce a plan
    eng.CFG["sync"] = {"host": "h", "expect_plans": True, "plan_deadline": "00:00"}
    eng.check_plan_arrived()  # the 11:00 warning is the same cause: it joins the same alert
    eng.check_plan_arrived()
    got = alerts(eng, "plan")
    assert len(got) == 1 and "no posts planned for" in got[0]["text"]
    assert not alerts(eng, "job")


async def test_other_preflight_problems_still_alert_next_to_a_missing_plan(two, monkeypatch):
    eng, left, _, _ = two
    script(eng, monkeypatch, f"print('ABORTED: phone left (x) is not connected over adb | {PLAN_PROBLEM}'); raise SystemExit(1)")
    j = add(eng, kind="preflight", app="-", account="-", phone_id="left", args={}, label="preflight")
    await left.step()
    assert status(eng, j)["status"] == "waiting"  # the phone problem is retried as before
    assert len(alerts(eng, "plan")) == 1 and "not connected" not in alerts(eng, "plan")[0]["text"]
    sql(eng, "update jobs set run_at=? where id=?", time.time() - 1, j)
    await left.step()
    assert status(eng, j)["status"] == "failed"
    job_alerts = alerts(eng, "job")
    assert len(job_alerts) == 1 and "not connected" in job_alerts[0]["text"] and "no posts planned" not in job_alerts[0]["text"]
    assert len(alerts(eng, "plan")) == 1  # still one plan alert


def load_preflight(make_engine, monkeypatch, tmp_path):
    eng = make_engine(TWO_PHONES)
    spec = importlib.util.spec_from_file_location("preflight_alerts_under_test", REPO / "preflight.py")
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)
    monkeypatch.setattr(pf, "ROOT", tmp_path)
    monkeypatch.setitem(pf.CFG, "sync", {"host": "h", "expect_plans": True})
    monkeypatch.setenv("PHONE_ID", "left")
    return eng, pf


def test_preflight_plan_problem_says_how_last_nights_run_went(make_engine, monkeypatch, tmp_path):
    _, pf = load_preflight(make_engine, monkeypatch, tmp_path)
    for info, expect in (({"last_nightly": "step 3 done\nexit=0"}, "finished, exit=0"),
                         ({"last_nightly": "rendering clip 4"}, "no exit line yet"),
                         ({"last_nightly": ""}, "no nightly run log"),
                         ({}, "unknown")):
        problems = []
        pf.check_plan(problems, info)
        assert len(problems) == 1
        assert problems[0].startswith("no posts planned for ") and "Last nightly run" in problems[0] and expect in problems[0]


def test_preflight_plan_check_semantics_unchanged(make_engine, monkeypatch, tmp_path):
    eng, pf = load_preflight(make_engine, monkeypatch, tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")
    problems, info = [], {}
    monkeypatch.setitem(pf.CFG, "sync", {"host": "h", "expect_plans": False})
    pf.check_plan(problems, info)
    assert problems == []  # expect_plans off: silent
    monkeypatch.setitem(pf.CFG, "sync", {"host": "h", "expect_plans": True})
    pf.check_plan(problems, info)
    assert len(problems) == 1 and info["posts_today"] == 0
    con = eng.db()
    con.execute("insert into plans (name, day) values ('n', ?)", (today,))
    eng.add_job(con, plan="n", kind="post", app="instagram", account="alpha", run_at=time.time(), args={}, phone_id="left")
    con.commit()
    problems = []
    pf.check_plan(problems, info)
    assert problems == [] and info["posts_today"] == 1


# ---------------------------------------------------------------- posting pace: no bursts after downtime
OK = "import json; print('RESULT '+json.dumps({'ok': 1}))"


def post(eng, account="alpha", late_min=10, **kw):
    return add(eng, kind="post", app="instagram", account=account, phone_id="left", run_at=time.time() - late_min * 60, **kw)


async def test_backlog_after_downtime_drains_one_post_per_gap(two, monkeypatch):
    eng, left, _, _ = two
    script(eng, monkeypatch, OK)
    ids = [post(eng, late_min=60 - i) for i in range(4)]  # four slots all overdue at once (the 02:07 cluster)
    await left.step()
    assert [status(eng, i)["status"] for i in ids] == ["done", "waiting", "waiting", "waiting"]
    assert left.pick(time.time()) == (None, None)  # nothing else may post now
    now = time.time()
    first_end = status(eng, ids[0])["ended_at"]
    for i in ids[1:]:  # every other post waits for the gap after the last published one
        assert status(eng, i)["run_at"] >= first_end + 45 * 60 - 1
    assert left.pick(now + 46 * 60)[0]["id"] == ids[1]  # and then exactly one is due


async def test_gap_is_per_account(two, monkeypatch):
    eng, left, _, _ = two
    script(eng, monkeypatch, OK)
    a1, a2 = post(eng, "alpha", 30), post(eng, "alpha", 20)
    await left.step()  # alpha posts
    job, _ = left.pick(time.time())
    assert job is None  # alpha's second post waits
    con = eng.db()
    b = eng.add_job(con, plan="p", kind="post", app="tiktok", account="alpha", run_at=time.time() - 5, phone_id="left",
                    args={"video": "v", "caption": "c", "visibility": "everyone"}, label="t", post_key="p/x/tt")
    con.commit()
    assert left.pick(time.time())[0]["id"] == b  # a different app / account is not held back
    assert status(eng, a2)["status"] == "waiting" and status(eng, a1)["status"] == "done"


async def test_stale_slots_are_skipped_with_one_digest_alert(two, monkeypatch):
    eng, left, _, _ = two
    script(eng, monkeypatch, OK)
    old = [post(eng, late_min=9 * 60), post(eng, late_min=8 * 60)]
    fresh = post(eng, late_min=5)
    job, _ = left.pick(time.time())
    assert job["id"] == fresh  # the stale ones were dropped on the way
    for i in old:
        s = status(eng, i)
        assert s["status"] == "skipped" and "slot missed" in s["error"]
    got = alerts(eng, "post")
    assert len(got) == 1 and got[0]["text"].count("its slot was missed") == 2  # one alert row, both listed


async def test_normal_day_is_not_delayed(two, monkeypatch):
    eng, left, _, _ = two
    script(eng, monkeypatch, OK)
    j = post(eng, late_min=2)
    await left.step()
    assert status(eng, j)["status"] == "done"
    k = post(eng, account="alpha", late_min=0)  # a post planned >= 45 min after the last one is fine
    sql(eng, "update jobs set ended_at=? where id=?", time.time() - 50 * 60, j)
    assert left.pick(time.time())[0]["id"] == k


async def test_pace_settings_can_be_changed_or_turned_off(two, monkeypatch):
    eng, left, _, _ = two
    monkeypatch.setitem(eng.SCHED, "post_min_gap_min", 0)
    monkeypatch.setitem(eng.SCHED, "post_max_late_min", 0)
    script(eng, monkeypatch, OK)
    a, b = post(eng, late_min=600), post(eng, late_min=590)
    await left.step()
    await left.step()
    assert status(eng, a)["status"] == status(eng, b)["status"] == "done"
    assert sys.version  # (keeps the import used)
