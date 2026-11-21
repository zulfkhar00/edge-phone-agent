"""Several phones in one engine: jobs go to the phone that owns the account, every phone runs by itself, and one
phone's trouble (unplugged, taken over, crashing) never reaches the others. Fake adb, fake flow scripts."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import phones
from conftest import OLD_CONFIG, TWO_PHONES


def post_args(**kw):
    return {"video": "v.mp4", "caption": "a caption", "audience": "everyone", "stats_offsets_min": [60], **kw}


def add(eng, kind="post", app="instagram", account="alpha", phone_id=None, **kw):
    con = eng.db()
    kw.setdefault("run_at", time.time() - 1)
    kw.setdefault("args", post_args() if kind == "post" else {"caption": "a caption", "offset_min": 60})
    kw.setdefault("label", "x")
    if phone_id:
        kw["phone_id"] = phone_id
    jid = eng.add_job(con, plan="p", kind=kind, app=app, account=account, post_key="p/x/ig", **kw)
    con.commit()
    return jid


def status(eng, jid):
    return dict(eng.db().execute("select * from jobs where id=?", (jid,)).fetchone())


@pytest.fixture
def two(make_engine, adb):
    eng = make_engine(TWO_PHONES)
    adb.plug("HW-LEFT")
    adb.plug("HW-RIGHT", "USB-RIGHT")
    for r in eng.RUNNERS:
        r.adb = adb  # Runner.adb is the one place the engine talks to adb
    return eng, eng.RUNNER_BY_ID["left"], eng.RUNNER_BY_ID["right"], adb


def script(eng, monkeypatch, code: str):
    monkeypatch.setattr(eng.Runner, "command", lambda self, job, minutes: [sys.executable, "-c", code])


# ---------------------------------------------------------------- config at startup
def test_engine_refuses_an_account_on_two_phones(make_engine):
    bad = TWO_PHONES.replace('accounts = ["ig:@beta", "tt:@gamma"]', 'accounts = ["ig:@beta", "ig:@alpha"]')
    with pytest.raises(phones.ConfigError, match="two phones"):
        make_engine(bad)


def test_old_config_is_one_phone_1(make_engine):
    eng = make_engine(OLD_CONFIG)
    assert [p.id for p in eng.PHONES] == ["phone-1"]
    assert eng.RUNNER is eng.RUNNERS[0] and eng.RUNNER.serial == "HW-LEFT"
    assert eng.wifi_addr(eng.PHONES[0]) == "10.0.0.11:5555"


def test_each_phone_has_its_own_mirror_port(two):
    eng, left, right, _ = two
    assert left.mirror.port != right.mirror.port
    assert left.mirror is not right.mirror
    assert right.serial == "USB-RIGHT"  # usb_serial wins over hw_serial


# ---------------------------------------------------------------- database
def test_migration_adds_phone_id_and_is_idempotent(make_engine, tmp_path):
    old = sqlite3.connect(tmp_path / "old.db")
    old.executescript("""
      create table jobs (id integer primary key, plan text, kind text, app text, account text, run_at real, not_after real,
        args text, status text default 'waiting', attempts integer default 0, started_at real, ended_at real,
        result text, error text, shot text, post_key text, label text, created_at real);
      create table alerts (id integer primary key, at real, kind text, text text, emailed integer default 0, seen integer default 0);
      insert into jobs (plan, kind, app, account, args) values ('p','post','instagram','alpha','{}'),
        ('p','post','instagram','beta','{}'), ('p','post','instagram','stranger','{}');
      insert into alerts (at, kind, text) values (1, 'x', 'old alert');
    """)
    old.commit()
    old.close()
    eng = make_engine(TWO_PHONES)
    eng.DB = tmp_path / "old.db"
    for _ in range(3):  # idempotent
        con = eng.db()
    for table in ("jobs", "alerts"):
        assert [r[1] for r in con.execute(f"pragma table_info({table})")].count("phone_id") == 1
    assert eng.backfill_phone_ids() == 3
    got = [r[0] for r in eng.db().execute("select phone_id from jobs order by id")]
    assert got == ["left", "right", "left"]  # the owner; the unowned account goes to the first phone
    assert eng.backfill_phone_ids() == 0
    assert eng.db().execute("select count(*) from alerts").fetchone()[0] == 1  # old rows kept


# ---------------------------------------------------------------- plan import
def write_plan(eng, name, plan):
    d = eng.INBOX / name
    d.mkdir(parents=True)
    (d / "plan.json").write_text(json.dumps(plan))


def import_with(eng, monkeypatch, plan, name="d1"):
    monkeypatch.setattr(eng, "check_plan", lambda raw, folder, cfg, existing, per_day: (raw, []))
    write_plan(eng, name, plan)
    eng.import_plans()
    return [dict(r) for r in eng.db().execute("select * from jobs order by id")]


PLAN = {"day": "2026-11-20", "stats_offsets_min": [60],
        "posts": [{"id": "p1", "at": "13:00", "video": "videos/v.mp4", "caption": "one",
                   "targets": [{"app": "instagram", "account": "alpha"}, {"app": "instagram", "account": "beta"},
                               {"app": "tiktok", "account": "gamma"}]}],
        "research": [{"app": "tiktok", "account": "alpha", "keyword": "k", "minutes": 10}],
        "deletes": [{"app": "tiktok", "account": "gamma", "caption": "old", "at": "23:00"}]}


def test_plan_jobs_go_to_the_phone_that_owns_the_account(two, monkeypatch):
    eng = two[0]
    jobs = import_with(eng, monkeypatch, PLAN)
    by = {(j["kind"], j["app"], j["account"]): j["phone_id"] for j in jobs}
    assert by == {("post", "instagram", "alpha"): "left", ("post", "instagram", "beta"): "right",
                  ("post", "tiktok", "gamma"): "right", ("research", "tiktok", "alpha"): "left",
                  ("delete", "tiktok", "gamma"): "right"}


def test_plan_with_account_no_phone_owns_alerts_and_skips_only_that(make_engine, monkeypatch):
    eng = make_engine(TWO_PHONES.replace('accounts = ["ig:@beta", "tt:@gamma"]', 'accounts = ["ig:@beta"]'))
    jobs = import_with(eng, monkeypatch, PLAN)
    assert {(j["app"], j["account"]) for j in jobs if j["kind"] == "post"} == {("instagram", "alpha"), ("instagram", "beta")}
    assert not [j for j in jobs if j["kind"] == "delete"]  # gamma's clean-up has no phone
    alerts = [dict(r) for r in eng.db().execute("select * from alerts")]
    assert len(alerts) == 1 and "no phone owns tiktok @gamma" in alerts[0]["text"]
    assert eng.db().execute("select count(*) from plans where name='d1' and day='2026-11-20'").fetchone()[0] == 1  # imported


def test_old_config_plan_goes_to_phone_1(make_engine, monkeypatch):
    eng = make_engine(OLD_CONFIG)
    jobs = import_with(eng, monkeypatch, PLAN)
    assert jobs and {j["phone_id"] for j in jobs} == {"phone-1"}
    assert not eng.db().execute("select 1 from alerts").fetchone()


def test_quotas_are_per_phone(two):
    eng = two[0]
    assert eng.acct_cfg("instagram", "beta")["posts_per_day"] == 1  # right's quota
    assert eng.acct_cfg("instagram", "alpha")["posts_per_day"] == 4  # left has none


def test_preflight_is_queued_for_every_phone_once(two):
    eng = two[0]
    eng.schedule_preflight()
    eng.schedule_preflight()
    rows = eng.db().execute("select phone_id from jobs where kind='preflight' order by phone_id").fetchall()
    assert [r[0] for r in rows] == ["left", "right"]


def test_orphan_jobs_of_a_removed_phone_are_skipped(two):
    eng = two[0]
    gone = add(eng, phone_id="removed-phone")
    kept = add(eng, phone_id="left")
    eng.orphan_jobs()
    assert status(eng, gone)["status"] == "skipped" and "no longer in config" in status(eng, gone)["error"]
    assert status(eng, kept)["status"] == "waiting"


# ---------------------------------------------------------------- queues are per phone
def test_a_phone_only_picks_its_own_jobs(two):
    eng, left, right, _ = two
    j_left = add(eng, account="alpha", phone_id="left")
    assert right.pick(time.time()) == (None, None)
    job, _ = left.pick(time.time())
    assert job["id"] == j_left


def test_jobs_without_phone_id_belong_to_the_first_phone(two):
    eng, left, right, _ = two
    jid = add(eng, account="alpha")  # a job written before multi-phone
    assert right.pick(time.time()) == (None, None)
    assert left.pick(time.time())[0]["id"] == jid


def test_research_scheduling_does_not_cross_phones(two):
    eng, left, right, _ = two
    add(eng, kind="post", account="alpha", phone_id="left", run_at=time.time() + 120)  # left has a post in 2 min
    rid = add(eng, kind="research", app="instagram", account="beta", phone_id="right",
              args={"keyword": "k", "minutes": 10}, not_after=time.time() + 3600)
    job, minutes = right.pick(time.time())  # right's gap is not shortened by left's post
    assert job["id"] == rid and minutes == 10


# ---------------------------------------------------------------- running jobs
async def test_concurrent_stats_jobs_on_two_phones(two, monkeypatch):
    eng, left, right, _ = two
    script(eng, monkeypatch, "import os,time,json; t=time.time(); time.sleep(0.6); "
                             "print('RESULT '+json.dumps({'id':os.environ['PHONE_ID'],'serial':os.environ['PHONE_SERIAL'],'t0':t,'t1':time.time()}))")
    a = add(eng, kind="stats", app="instagram", account="alpha", phone_id="left")
    b = add(eng, kind="stats", app="instagram", account="beta", phone_id="right")
    await asyncio.gather(left.step(), right.step())
    ra, rb = (json.loads(status(eng, j)["result"]) for j in (a, b))
    assert (ra["id"], ra["serial"]) == ("left", "HW-LEFT")
    assert (rb["id"], rb["serial"]) == ("right", "USB-RIGHT")
    assert ra["t0"] < rb["t1"] and rb["t0"] < ra["t1"]  # they really overlapped
    assert status(eng, a)["status"] == status(eng, b)["status"] == "done"
    assert status(eng, a)["phone_id"] == "left"


async def test_unplugged_mid_job_fails_that_phone_only(two, monkeypatch):
    eng, left, right, adb = two
    flag = Path(eng.ROOT) / "cable-pulled"
    script(eng, monkeypatch,
           "import os,sys,time\n"
           f"flag = {str(flag)!r}\n"
           "if os.environ['PHONE_ID'] == 'left':\n"
           "    while not os.path.exists(flag): time.sleep(0.05)\n"
           "    sys.exit(1)\n"
           "time.sleep(0.3); print('RESULT ok')\n")
    a = add(eng, kind="stats", account="alpha", phone_id="left")
    b = add(eng, kind="stats", app="instagram", account="beta", phone_id="right")
    t_left = asyncio.create_task(left.step())
    t_right = asyncio.create_task(right.step())
    while left.job is None:
        await asyncio.sleep(0.02)
    adb.unplug("HW-LEFT")
    flag.touch()
    await asyncio.gather(t_left, t_right)
    assert status(eng, a)["status"] == "failed" and "unplugged mid-job" in status(eng, a)["error"]
    assert status(eng, b)["status"] == "done"  # the other phone carried on
    assert left.phone_ok is False and right.phone_ok is True
    alerts = [r["phone_id"] for r in eng.db().execute("select phone_id from alerts")]
    assert alerts == ["left"]


async def test_unplugged_after_tapping_post_is_unverified_not_retried(two, monkeypatch):
    eng, left, right, adb = two
    adb.unplug("HW-LEFT")
    script(eng, monkeypatch, "import sys; print('  tap Post at (10,20)', flush=True); sys.exit(1)")
    jid = add(eng, kind="post", account="alpha", phone_id="left")
    job = eng.db().execute("select * from jobs where id=?", (jid,)).fetchone()
    await left.run(job, None)  # (check_phone would refuse to start it: this is the cable going mid-run)
    s = status(eng, jid)
    assert s["status"] == "unverified" and "unplugged" in s["error"]
    assert eng.db().execute("select count(*) from jobs where kind='stats' and phone_id='left'").fetchone()[0] == 1


async def test_flow_failure_with_phone_still_there_retries_as_before(two, monkeypatch):
    eng, left, *_ = two
    script(eng, monkeypatch, "import sys; sys.exit(3)")
    jid = add(eng, kind="stats", account="alpha", phone_id="left")
    await left.step()
    s = status(eng, jid)
    assert s["status"] == "waiting" and s["run_at"] > time.time() + 200 and s["attempts"] == 1


async def test_phone_not_connected_leaves_job_waiting_and_alerts_once(two):
    eng, left, right, adb = two
    adb.unplug("HW-LEFT")
    jid = add(eng, kind="stats", account="alpha", phone_id="left")
    for _ in range(3):
        await left.step()
    assert status(eng, jid)["status"] == "waiting" and left.phone_ok is False
    rows = eng.db().execute("select text, phone_id from alerts").fetchall()
    assert len(rows) == 1 and rows[0]["phone_id"] == "left" and "HW-LEFT" in rows[0]["text"]
    adb.plug("HW-LEFT")  # cable back: the job runs
    await left.step()
    assert left.phone_ok is True


async def test_failure_screenshots_are_matched_per_phone(two, monkeypatch):
    eng, left, right, _ = two
    runs = Path(eng.ROOT) / "runs"
    script(eng, monkeypatch,
           "import os,sys; open(f'runs/fail-0101-000000-{os.environ[\"PHONE_ID\"]}.png','w').write('x'); "
           "open('runs/fail-0101-000000-other.png','w').write('x'); sys.exit(2)")
    a = add(eng, kind="stats", account="alpha", phone_id="left")
    await left.step()
    assert status(eng, a)["shot"] == "fail-0101-000000-left.png"
    assert (runs / "fail-0101-000000-other.png").exists()


# ---------------------------------------------------------------- take over / pause
async def test_take_over_pauses_one_phone_only(two, monkeypatch):
    eng, left, right, _ = two
    script(eng, monkeypatch, "print('RESULT ok')")
    a = add(eng, kind="stats", account="alpha", phone_id="left")
    b = add(eng, kind="stats", app="instagram", account="beta", phone_id="right")
    await left.take_over()
    assert left.paused and not right.paused
    assert left.status() == "manual" and right.status() == "idle"
    await asyncio.gather(left.step(), right.step())
    assert status(eng, a)["status"] == "waiting"  # left is yours
    assert status(eng, b)["status"] == "done"  # right kept working
    await left.resume()
    await left.step()
    assert status(eng, a)["status"] == "done"


async def test_auto_resume_is_per_phone(two):
    eng, left, right, _ = two
    await left.take_over()
    await right.take_over()
    eng.set_state("touched:left", time.time() - 3600)  # left forgotten, right just used
    right.touch()
    await left.auto_resume()
    await right.auto_resume()
    assert not left.paused and right.paused


async def test_take_over_stops_research_of_that_phone_only(two, monkeypatch):
    eng, left, right, _ = two
    script(eng, monkeypatch, "import time; time.sleep(30)")
    a = add(eng, kind="research", app="instagram", account="alpha", phone_id="left", args={"keyword": "k", "minutes": 10})
    b = add(eng, kind="research", app="instagram", account="beta", phone_id="right", args={"keyword": "k", "minutes": 10})
    ta, tb = asyncio.create_task(left.step()), asyncio.create_task(right.step())
    while left.job is None or right.job is None:
        await asyncio.sleep(0.02)
    await left.take_over()
    await ta  # left's research was stopped at once
    assert status(eng, a)["status"] == "done" and "taken over" in status(eng, a)["error"]
    assert right.job is not None and status(eng, b)["status"] == "running"  # right is still going
    right.proc.kill()
    await tb


# ---------------------------------------------------------------- failure isolation
async def test_one_runner_crashing_does_not_stop_the_others(two, monkeypatch):
    eng, left, right, _ = two
    script(eng, monkeypatch, "print('RESULT ok')")
    monkeypatch.setattr(left, "pick", lambda now: (_ for _ in ()).throw(RuntimeError("boom")))
    b = add(eng, kind="stats", app="instagram", account="beta", phone_id="right")
    tasks = [asyncio.create_task(r.loop()) for r in (left, right)]
    for r, t in zip((left, right), tasks):
        r.task = t
    for _ in range(100):
        await asyncio.sleep(0.05)
        if status(eng, b)["status"] == "done":
            break
    assert status(eng, b)["status"] == "done"
    assert left.alive and right.alive  # the crashing phone's loop keeps going too
    assert any(r["phone_id"] == "left" and "boom" in r["text"] for r in eng.db().execute("select * from alerts"))
    for t in tasks:
        t.cancel()


# ---------------------------------------------------------------- API
@pytest.fixture
async def client(two):
    eng = two[0]
    app = web.Application()
    app.add_routes(eng.routes())
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_api_state_has_phones_with_heartbeat_and_keeps_old_fields(two, client):
    eng, left, right, _ = two
    add(eng, kind="stats", account="alpha", phone_id="left")
    add(eng, kind="stats", app="instagram", account="beta", phone_id="right")
    left.beat = time.time() - 2
    t = asyncio.create_task(asyncio.sleep(10))
    left.task = t
    st = await (await client.get("/api/state")).json()
    for old in ("paused", "pausing", "phone_ok", "running", "tail", "jobs", "alerts", "accounts", "mirror_error", "now"):
        assert old in st  # what the current dashboard reads
    assert [p["id"] for p in st["phones"]] == ["left", "right"]
    pl, pr = st["phones"]
    assert pl["name"] == "Left phone" and pl["heartbeat_age_s"] is not None and 1 < pl["heartbeat_age_s"] < 10 and pl["alive"]
    assert pr["heartbeat"] is None and pr["alive"] is False
    assert [j["account"] for j in pl["jobs"]] == ["alpha"] and [j["account"] for j in pr["jobs"]] == ["beta"]
    assert {a["account"] for a in pl["accounts"]} == {"alpha"} and {a["account"] for a in pr["accounts"]} == {"beta", "gamma"}
    assert len(st["jobs"]) == 2  # the top level keeps every job
    t.cancel()


async def test_api_phones_list(client):
    d = await (await client.get("/api/phones")).json()
    assert [(p["id"], p["serial"], p["status"]) for p in d["phones"]] == [("left", "HW-LEFT", "idle"), ("right", "USB-RIGHT", "idle")]


async def test_per_phone_takeover_and_resume_endpoints(two, client):
    eng, left, right, _ = two
    assert (await client.post("/api/phones/right/takeover")).status == 200
    assert right.paused and not left.paused
    st = await (await client.get("/api/phones/right/state")).json()
    assert st["paused"] is True and st["id"] == "right"
    assert (await client.post("/api/phones/right/resume")).status == 200
    assert not right.paused


async def test_old_endpoints_are_aliases_for_the_first_phone(two, client):
    eng, left, right, _ = two
    assert (await client.post("/api/takeover")).status == 200
    assert left.paused and not right.paused
    assert (await (await client.get("/api/state")).json())["paused"] is True  # old dashboard sees phone-1's state
    assert (await client.post("/api/resume")).status == 200
    assert not left.paused


async def test_phone_query_param_selects_the_phone_like_the_new_dashboard_sends(two, client):
    eng, left, right, _ = two
    assert (await client.post("/api/takeover?phone=right")).status == 200
    assert right.paused and not left.paused
    r = await client.post("/api/input", json={"t": "tap", "x": 1, "y": 2, "phone": "left"})
    assert r.status == 409  # left is not taken over, even though right is
    r = await client.post("/api/input", json={"t": "tap", "x": 1, "y": 2, "phone": "right"})
    assert r.status == 200
    assert ("-s", "USB-RIGHT", "shell", "input", "tap", "1", "2") in two[3].calls  # only fake adb ran, on the right phone


async def test_unknown_phone_is_404(client):
    assert (await client.post("/api/phones/nope/takeover")).status == 404
    assert (await client.post("/api/takeover?phone=nope")).status == 404


async def test_old_config_serves_old_api_unchanged(make_engine, adb):
    eng = make_engine(OLD_CONFIG)
    eng.RUNNER.adb = adb
    adb.plug("HW-LEFT")
    app = web.Application()
    app.add_routes(eng.routes())
    async with TestClient(TestServer(app)) as c:
        st = await (await c.get("/api/state")).json()
        assert [p["id"] for p in st["phones"]] == ["phone-1"] and st["paused"] is False
        assert (await c.post("/api/takeover")).status == 200
        assert eng.RUNNER.paused
        assert (await c.get("/api/state")).status == 200


# ---------------------------------------------------------------- preflight is per phone
def test_preflight_checks_only_its_own_phones_accounts(make_engine, tmp_path, monkeypatch):
    import importlib.util
    make_engine(TWO_PHONES)  # writes config.toml and sets ENGINE_CONFIG
    spec = importlib.util.spec_from_file_location("preflight_under_test", Path(__file__).parent.parent / "preflight.py")
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)
    monkeypatch.setenv("PHONE_ID", "right")
    monkeypatch.setenv("PHONE_SERIAL", "USB-RIGHT")
    assert pf.this_phone().id == "right" and pf.serial() == "USB-RIGHT"
    seen = []

    class R:
        returncode, stdout = 0, "now on"

    monkeypatch.setattr(pf.subprocess, "run", lambda cmd, **kw: seen.append(cmd[2:]) or R())
    problems, info = [], {}
    pf.check_accounts(problems, info)
    assert seen == [["instagram", "beta"]]  # gamma is "practice"; alpha lives on the other phone
    monkeypatch.setenv("PHONE_ID", "left")
    seen.clear()
    pf.check_accounts(problems, info)
    assert seen == [["instagram", "alpha"], ["tiktok", "alpha"]]
