"""NoSwipe integration against a fake NoSwipe server: polling and pause, the outbox (retry, idempotency, clock skew),
and the dating-session skeleton (needs_you on no login, nothing reported when unmapped, caps, quiet hours)."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import dating
import noswipe
import phones
from conftest import TWO_PHONES
from fake_noswipe import KEY, FakeNoswipe

REPO = Path(__file__).resolve().parent.parent
LA = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("NOSWIPE_AUTOMATION_KEY", KEY)


@pytest.fixture
async def srv():
    s = FakeNoswipe()
    s.url = await s.start()
    yield s
    await s.stop()


def cfg(**kw) -> noswipe.NoswipeCfg:
    c = noswipe.NoswipeCfg(enabled=True, pace_s=(0, 0), batch_size=(5, 5), quiet_hours="off")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def client(srv, key=KEY) -> noswipe.Client:
    return noswipe.Client(srv.url, key)


def ts(y, mo, d, h, mi=0, tz=LA) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


class FakeUI:
    """A phone that really did what it says: logs in or not, swipes up to `per_batch`, has `matches` to report."""

    def __init__(self, logged_in=True, per_batch=None, matches=(), supply=10**6):
        self.is_in, self.per_batch, self.match_list, self.supply = logged_in, per_batch, list(matches), supply
        self.opened = self.closed = 0
        self.asked: list[int] = []

    def open_app(self): self.opened += 1
    def logged_in(self): return self.is_in
    def close_app(self): self.closed += 1

    def swipe_batch(self, n):
        self.asked.append(n)
        got = min(n if self.per_batch is None else self.per_batch, self.supply)
        self.supply -= got
        return got

    def new_matches(self):
        out, self.match_list = self.match_list, []
        return out


def session(c=None, **kw) -> dating.Session:
    base = dict(user_id="u1", app="hinge", account_ref="hinge-001", tz=LA, budget_s=600, job_id="7", cfg=c or cfg())
    base.update(kw)
    return dating.Session(**base)


class Clock:
    """Fake time that moves when the session sleeps."""

    def __init__(self, t: float):
        self.t = t

    def __call__(self): return self.t
    def sleep(self, s): self.t += max(s, 1.0)


def run(ui, s, outbox, clock, **kw):
    return dating.run_session(ui, s, outbox, clock=clock, sleep=clock.sleep, rng=random.Random(1), log=lambda *_: None, **kw)


# ================================================================ config
def test_noswipe_is_off_by_default():
    c = noswipe.NoswipeCfg.from_cfg({})
    assert c.enabled is False and c.api_key_env == "NOSWIPE_AUTOMATION_KEY"


def test_api_key_comes_from_the_environment_only(monkeypatch):
    c = noswipe.NoswipeCfg.from_cfg({"noswipe": {"enabled": True, "api_key_env": "MY_KEY", "api_key": "leaked"}})
    monkeypatch.delenv("MY_KEY", raising=False)
    assert c.api_key == ""            # a key written in the file is ignored
    monkeypatch.setenv("MY_KEY", " secret ")
    assert c.api_key == "secret"


@pytest.mark.parametrize("bad", [{"base_url": "http://noswipe.app/api"}, {"quiet_hours": "late"}, {"poll_every_min": 0},
                                 {"swipe_caps": {"grindr": 5}}])
def test_bad_noswipe_config_is_refused(bad):
    with pytest.raises(phones.ConfigError):
        noswipe.NoswipeCfg.from_cfg({"noswipe": bad})


def test_phone_id_defaults_to_the_phone_id_and_can_be_overridden():
    ph = phones.load_phones({"phones": [{"id": "a", "hw_serial": "H1"}, {"id": "b", "hw_serial": "H2", "noswipe_phone_id": "pi-rack-1-phone-3"}]})
    assert [p.noswipe_id for p in ph] == ["a", "pi-rack-1-phone-3"]


# ================================================================ polling, shouldRun, pause
def integ(srv, tmp_path, c=None, key=KEY, ids=(("left", ""), ("right", "rack-2"))):
    ph = phones.load_phones({"phones": [{"id": i, "hw_serial": f"H-{i}", "noswipe_phone_id": n} for i, n in ids]})
    return noswipe.Integration(c or cfg(), ph, noswipe.Outbox(tmp_path / "ob.db"), client(srv, key)), ph


def jobs_db(tmp_path):
    import sqlite3
    con = sqlite3.connect(tmp_path / "j.db")
    con.row_factory = sqlite3.Row
    con.execute("create table if not exists jobs (id integer primary key, plan text, kind text, app text, account text, run_at real, "
                "not_after real, args text, status text default 'waiting', attempts integer default 0, started_at real, ended_at real, "
                "result text, error text, shot text, post_key text, label text, created_at real, phone_id text)")
    return con


async def test_poll_asks_each_phone_by_its_noswipe_id_and_runs_only_shouldrun(srv, tmp_path):
    srv.runs["left"] = [srv.run(ref="hinge-001"), srv.run(user="u2", ref="hinge-002", should=False)]
    srv.runs["rack-2"] = [srv.run(user="u3", app="tinder", ref="tinder-009")]
    i, _ = integ(srv, tmp_path)
    now = time.time()
    await i.poll(now)
    assert sorted(srv.runs_calls) == ["left", "rack-2"]
    con = jobs_db(tmp_path)
    assert i.sync_jobs(con, now) == []
    got = {(r["phone_id"], r["app"], r["account"]) for r in con.execute("select * from jobs where kind='dating_session'")}
    assert got == {("left", "hinge", "hinge-001"), ("right", "tinder", "tinder-009")}  # paused hinge-002: no job


async def test_second_sync_does_not_duplicate_and_cooldown_spaces_sessions(srv, tmp_path):
    srv.runs["left"] = [srv.run()]
    i, _ = integ(srv, tmp_path, cfg(session_every_min=60))
    con, now = jobs_db(tmp_path), time.time()
    await i.poll(now)
    i.sync_jobs(con, now)
    i.sync_jobs(con, now + 5)
    assert con.execute("select count(*) from jobs").fetchone()[0] == 1
    con.execute("update jobs set status='done', ended_at=?", (now + 100,))
    i.sync_jobs(con, now + 200)            # session just ended: wait
    assert con.execute("select count(*) from jobs").fetchone()[0] == 1
    await i.poll(now + 3700)
    i.sync_jobs(con, now + 3700)           # an hour later: next session
    assert con.execute("select count(*) from jobs").fetchone()[0] == 2


async def test_pause_skips_waiting_and_stops_running_sessions(srv, tmp_path):
    srv.runs["left"] = [srv.run(ref="a"), srv.run(user="u2", ref="b")]
    i, _ = integ(srv, tmp_path)
    con, now = jobs_db(tmp_path), time.time()
    await i.poll(now)
    i.sync_jobs(con, now)
    con.execute("update jobs set status='running' where account='a'")
    srv.runs["left"] = [srv.run(ref="a", should=False), srv.run(user="u2", ref="b", should=False)]   # the client paused both
    await i.poll(now + 60)
    stop = i.sync_jobs(con, now + 60)
    rows = {r["account"]: r["status"] for r in con.execute("select * from jobs")}
    assert rows == {"a": "running", "b": "skipped"}                       # waiting one skipped at once
    assert stop == [con.execute("select id from jobs where account='a'").fetchone()[0]]   # running one: SIGTERM it


async def test_a_run_that_disappears_from_the_list_stops_too(srv, tmp_path):
    srv.runs["left"] = [srv.run()]
    i, _ = integ(srv, tmp_path)
    con, now = jobs_db(tmp_path), time.time()
    await i.poll(now)
    i.sync_jobs(con, now)
    srv.runs["left"] = []
    await i.poll(now + 60)
    i.sync_jobs(con, now + 60)
    assert con.execute("select status from jobs").fetchone()[0] == "skipped"


async def test_stale_list_means_no_work(srv, tmp_path):
    srv.runs["left"] = [srv.run()]
    i, _ = integ(srv, tmp_path, cfg(stale_after_min=10))
    con, now = jobs_db(tmp_path), time.time()
    await i.poll(now)
    i.sync_jobs(con, now)
    con.execute("update jobs set status='running'")
    assert len(i.sync_jobs(con, now + 11 * 60)) == 1       # NoSwipe unreachable for 11 min: stop the running session
    assert i.wanted("left", now + 11 * 60) == {}


async def test_wrong_key_is_reported_not_crashing_and_creates_no_jobs(srv, tmp_path):
    srv.runs["left"] = [srv.run()]
    i, _ = integ(srv, tmp_path, key="wrong")
    con, now = jobs_db(tmp_path), time.time()
    await i.poll(now)
    i.sync_jobs(con, now)
    assert con.execute("select count(*) from jobs").fetchone()[0] == 0
    assert any("401" in e["text"] for e in i.errors)
    assert i.view(i.phones[0], now)["stale"] is True


async def test_unknown_apps_and_missing_user_are_ignored(srv, tmp_path):
    srv.runs["left"] = [srv.run(app="grindr"), {"app": "hinge", "shouldRun": True}]
    i, _ = integ(srv, tmp_path)
    await i.poll(time.time())
    assert i.wanted("left", time.time()) == {}


# ================================================================ outbox: retry, idempotency
def ob(tmp_path) -> noswipe.Outbox:
    return noswipe.Outbox(tmp_path / "ob.db")


def swipes(o, n=5, ref="s1", user="u1", at=None):
    return o.add_swipes(user, "hinge", "hinge-001", n, "2026-11-22", ref, at or time.time())


async def test_events_are_delivered_and_counted_once(srv, tmp_path):
    o = ob(tmp_path)
    swipes(o, 5, "s1"); o.add("u1", "hinge", "match", "m-1", time.time(), {"matchRef": "m-1"})
    r = await noswipe.flush(o, client(srv))
    assert r["sent"] == 2 and srv.counts == {"swipes": 5, "match": 1}
    assert o.counts()["pending"] == 0 and o.counts()["sent"] == 2


async def test_same_event_enqueued_twice_is_one_event(srv, tmp_path):
    o = ob(tmp_path)
    assert swipes(o, 5, "s1") is True
    assert swipes(o, 5, "s1") is False       # replay of the same batch
    assert o.swipes_today("hinge-001", "2026-11-22") == 5     # and it does not count twice towards the cap
    await noswipe.flush(o, client(srv))
    assert srv.counts == {"swipes": 5}


def test_keys_are_deterministic_and_in_the_contract_range():
    a = noswipe.make_key("u1", "hinge", "match", "m-1")
    assert a == noswipe.make_key("u1", "hinge", "match", "m-1")
    assert a != noswipe.make_key("u2", "hinge", "match", "m-1") != noswipe.make_key("u1", "tinder", "match", "m-1")
    assert 8 <= len(a) <= 200


async def test_server_error_keeps_events_and_a_later_retry_delivers_them(srv, tmp_path, monkeypatch):
    o = ob(tmp_path)
    swipes(o, 5, "s1")
    srv.fail_next = 1
    t0 = time.time()
    r = await noswipe.flush(o, client(srv), now=t0)
    assert r["retry"] == 1 and o.counts()["pending"] == 1 and srv.counts == {}
    assert "503" in o.counts()["errors"][0]["last_error"]
    assert (await noswipe.flush(o, client(srv), now=t0 + 1))["sent"] == 0     # still backing off: not even tried
    assert srv.activity_calls == 1
    r = await noswipe.flush(o, client(srv), now=t0 + 3700)
    assert r["sent"] == 1 and srv.counts == {"swipes": 5}


def test_backoff_grows_and_is_capped():
    r = random.Random(0)
    assert noswipe.backoff(0, r) < noswipe.backoff(3, r) <= noswipe.BACKOFF_MAX_S * 1.25
    assert noswipe.backoff(30, r) <= noswipe.BACKOFF_MAX_S * 1.25


async def test_lost_response_replay_is_a_duplicate_not_a_second_count(srv, tmp_path):
    o = ob(tmp_path)
    swipes(o, 5, "s1")
    srv.lose_response_next = 1            # the server stored it, the answer never arrived
    t0 = time.time()
    await noswipe.flush(o, client(srv), now=t0)
    assert o.counts()["pending"] == 1 and srv.counts == {"swipes": 5}
    r = await noswipe.flush(o, client(srv), now=t0 + 3700)
    assert r["duplicate"] == 1 and srv.counts == {"swipes": 5}      # still 5, not 10
    assert o.counts()["pending"] == 0


async def test_network_down_keeps_events(tmp_path):
    o = ob(tmp_path)
    swipes(o, 5, "s1")
    dead = noswipe.Client("http://127.0.0.1:9/api/automation/v1", KEY, timeout=2)
    r = await noswipe.flush(o, dead)
    assert r["retry"] == 1 and o.counts()["pending"] == 1


async def test_invalid_and_no_account_are_dropped_not_retried_forever(srv, tmp_path):
    o = ob(tmp_path)
    k1 = o.add("u1", "hinge", "match", "m-1", time.time(), {"matchRef": "m-1"})
    k2 = o.add("u1", "hinge", "match", "m-2", time.time(), {"matchRef": "m-2"})
    srv.reject_codes = {k1: "no_account", k2: "invalid"}
    r = await noswipe.flush(o, client(srv))
    assert r["dead"] == 2 and o.counts()["pending"] == 0 and o.counts()["dead"] == 2
    assert {e["last_error"] for e in o.counts()["errors"]} == {"rejected no_account: forced", "rejected invalid: forced"}


async def test_events_of_different_clients_go_in_separate_requests(srv, tmp_path):
    o = ob(tmp_path)
    swipes(o, 3, "s1", "u1"); swipes(o, 4, "s1", "u2")
    await noswipe.flush(o, client(srv))
    assert srv.activity_calls == 2 and {u for (u, _) in srv.events} == {"u1", "u2"}


async def test_batches_are_at_most_200_events(srv, tmp_path):
    o = ob(tmp_path)
    for n in range(250):
        o.add("u1", "hinge", "match", f"m-{n}", time.time(), {"matchRef": f"m-{n}"})
    r = await noswipe.flush(o, client(srv))
    assert r["sent"] == 250 and srv.activity_calls == 2


# ================================================================ clock-skew guard (occurredAt)
def test_guard_time_windows():
    now = 1_000_000_000.0
    assert noswipe.guard_time(now - 60, now) == (now - 60, "ok")
    t, how = noswipe.guard_time(now + 600, now)           # our clock ran ahead
    assert how == "clamped" and t <= now
    assert noswipe.guard_time(now - 8 * 86400, now)[1] == "expired"
    assert noswipe.guard_time(now - 6.9 * 86400, now)[1] == "ok"


async def test_event_older_than_7_days_is_dropped_without_asking_the_server(srv, tmp_path):
    o = ob(tmp_path)
    swipes(o, 5, "old", at=time.time() - 8 * 86400)
    r = await noswipe.flush(o, client(srv))
    assert r["expired"] == 1 and srv.activity_calls == 0 and o.counts()["dead"] == 1


async def test_fast_local_clock_is_corrected_from_the_server_date_header(srv, tmp_path, monkeypatch):
    monkeypatch.setattr(noswipe, "backoff", lambda *_a, **_k: 0)
    fast = lambda: time.time() + 600                       # this Pi's clock is 10 minutes ahead
    o = ob(tmp_path)
    swipes(o, 5, "s1", at=fast())
    c = client(srv)
    r1 = await noswipe.flush(o, c, clock=fast)            # skew unknown yet: the server says bad_time
    assert r1["retry"] == 1 and srv.counts == {}
    assert c.skew_s is not None and -603 < c.skew_s < -597   # learned from the Date header of that answer
    r2 = await noswipe.flush(o, c, clock=fast)
    assert r2["sent"] == 1 and srv.counts == {"swipes": 5}
    sent = next(iter(srv.events.values()))
    assert datetime.fromisoformat(sent["occurredAt"].replace("Z", "+00:00")).timestamp() <= time.time()


async def test_bad_time_that_never_gets_better_is_dropped_after_a_few_tries(srv, tmp_path, monkeypatch):
    monkeypatch.setattr(noswipe, "backoff", lambda *_a, **_k: 0)
    o = ob(tmp_path)
    k = o.add("u1", "hinge", "match", "m-1", time.time(), {"matchRef": "m-1"})
    srv.reject_codes = {k: "bad_time"}
    for _ in range(noswipe.BAD_TIME_TRIES):
        await noswipe.flush(o, client(srv))
    assert o.counts()["dead"] == 1 and o.counts()["pending"] == 0


# ================================================================ sessions: login, unmapped, caps, quiet hours
async def test_not_logged_in_reports_needs_you_once_and_swipes_nothing(srv, tmp_path):
    o, ui, clk = ob(tmp_path), FakeUI(logged_in=False), Clock(ts(2026, 11, 22, 12))
    res = run(ui, session(), o, clk)
    assert res["outcome"] == "needs_login" and ui.asked == [] and ui.closed == 1
    run(ui, session(job_id="8"), o, clk)                  # next session, same day: same key, still one event
    await noswipe.flush(o, client(srv))
    (ev,) = srv.events.values()
    assert ev["kind"] == "needs_you" and 3 <= len(ev["reason"]) <= 200 and "Hinge" in ev["reason"]
    assert srv.counts == {"needs_you": 1}


def test_unmapped_ui_reports_nothing_at_all(tmp_path):
    o, clk = ob(tmp_path), Clock(ts(2026, 11, 22, 12))
    res = run(dating.UnmappedUI("hinge"), session(), o, clk)
    assert res["outcome"] == "not_mapped" and res["swipes"] == 0
    assert o.counts()["pending"] == 0 and o.counts()["sent"] == 0 and o.counts()["dead"] == 0


@pytest.mark.parametrize("step", ["open_app", "logged_in", "swipe_batch", "new_matches", "close_app"])
def test_every_unmapped_step_raises_notmapped(step):
    with pytest.raises(dating.NotMapped):
        getattr(dating.UnmappedUI("tinder"), step)(*([1] if step == "swipe_batch" else []))


def test_swipes_are_reported_exactly_as_the_ui_did_them(tmp_path):
    o, ui, clk = ob(tmp_path), FakeUI(per_batch=3, matches=["m-1", "m-2"]), Clock(ts(2026, 11, 22, 12))
    res = run(ui, session(cfg=cfg(swipe_caps={"hinge": 12})), o, clk)
    assert res["swipes"] == 12 and res["matches"] == 2     # 4 batches of 3, then the cap
    assert o.counts()["pending"] == 4 + 2
    assert o.swipes_today("hinge-001", "2026-11-22") == 12


def test_daily_cap_is_per_account_and_spans_sessions(tmp_path):
    o, clk = ob(tmp_path), Clock(ts(2026, 11, 22, 9))
    c = cfg(swipe_caps={"hinge": 20})
    r1 = run(FakeUI(), session(c), o, clk)
    assert r1["swipes"] == 20 and r1["outcome"] == "cap_reached"
    ui2 = FakeUI()
    r2 = run(ui2, session(c, job_id="8"), o, clk)
    assert r2["outcome"] == "cap_reached" and r2["swipes"] == 0 and ui2.opened == 0     # does not even open the app
    r3 = run(FakeUI(), session(c, account_ref="hinge-002", user_id="u2", job_id="9"), o, clk)   # another account: own cap
    assert r3["swipes"] == 20
    clk.t = ts(2026, 11, 23, 9)                             # next local day: fresh cap
    assert run(FakeUI(), session(c, job_id="10"), o, clk)["swipes"] == 20


def test_cap_is_never_exceeded_by_a_batch(tmp_path):
    o, ui = ob(tmp_path), FakeUI()
    run(ui, session(cfg(swipe_caps={"hinge": 12}, batch_size=(5, 5))), o, Clock(ts(2026, 11, 22, 12)))
    assert ui.asked == [5, 5, 2]


def test_the_day_is_the_clients_day_not_the_pis(tmp_path):
    o = ob(tmp_path)
    kz = ZoneInfo("Asia/Almaty")
    clk = Clock(ts(2026, 11, 22, 23, 30, LA))               # 23:30 in LA is already 12:30 next day in Almaty
    run(FakeUI(), session(cfg(swipe_caps={"hinge": 10}, quiet_hours="off"), tz=kz), o, clk)
    assert o.swipes_today("hinge-001", "2026-11-23") == 10 and o.swipes_today("hinge-001", "2026-11-22") == 0


def test_time_budget_ends_the_session(tmp_path):
    o, ui, clk = ob(tmp_path), FakeUI(per_batch=1), Clock(ts(2026, 11, 22, 12))
    c = cfg(pace_s=(30, 30), swipe_caps={"hinge": 1000}, batch_size=(5, 5))
    res = run(ui, session(c, budget_s=300), o, clk)
    assert res["outcome"] == "done" and 8 <= res["swipes"] <= 11      # ~10 batches of 30 s in 5 minutes
    assert clk.t - ts(2026, 11, 22, 12) <= 300 + 31


def test_pacing_is_jittered_not_constant(tmp_path):
    gaps = []
    clk = Clock(ts(2026, 11, 22, 12))

    def sleep(s):
        gaps.append(s); clk.t += s

    dating.run_session(FakeUI(per_batch=1), session(cfg(pace_s=(4, 14), swipe_caps={"hinge": 10}), budget_s=3600), ob(tmp_path),
                       clock=clk, sleep=sleep, rng=random.Random(3), log=lambda *_: None)
    assert len(gaps) >= 8 and len({round(g, 2) for g in gaps}) > 4 and all(4 <= g <= 14 for g in gaps)


def test_quiet_hours_use_the_client_timezone(tmp_path):
    o = ob(tmp_path)
    c = cfg(quiet_hours="23:00-08:00")
    ui = FakeUI()
    night_la = Clock(ts(2026, 11, 22, 2))                   # 02:00 in LA
    assert run(ui, session(c), o, night_la)["outcome"] == "quiet_hours" and ui.opened == 0
    same_instant_london = session(c, tz=ZoneInfo("Europe/London"))    # 10:00 in London: fine
    assert run(FakeUI(), same_instant_london, o, Clock(night_la.t))["swipes"] > 0
    assert run(FakeUI(), session(c, job_id="2", account_ref="a2"), o, Clock(ts(2026, 11, 22, 8, 0)))["swipes"] > 0      # 08:00: window over
    assert run(FakeUI(), session(c, job_id="3", account_ref="a3"), o, Clock(ts(2026, 11, 22, 22, 59)))["outcome"] != "quiet_hours"
    assert run(FakeUI(), session(c, job_id="4", account_ref="a4"), o, Clock(ts(2026, 11, 22, 23, 0)))["outcome"] == "quiet_hours"


def test_quiet_hours_beginning_mid_session_stop_the_session(tmp_path):
    o, ui = ob(tmp_path), FakeUI(per_batch=1)
    clk = Clock(ts(2026, 11, 22, 22, 58))
    c = cfg(quiet_hours="23:00-08:00", pace_s=(30, 30), swipe_caps={"hinge": 1000})
    res = run(ui, session(c, budget_s=3600), o, clk)
    assert res["outcome"] == "quiet_hours" and 1 <= res["swipes"] <= 5


def test_quiet_hours_inside_one_day_and_off(tmp_path):
    assert dating.in_quiet_hours(ts(2026, 11, 22, 13), LA, "12:00-14:00")
    assert not dating.in_quiet_hours(ts(2026, 11, 22, 15), LA, "12:00-14:00")
    assert not dating.in_quiet_hours(ts(2026, 11, 22, 3), LA, "off")
    assert dating.tz_of("Nowhere/Land", "America/Los_Angeles").key == "America/Los_Angeles"


def test_stop_flag_ends_the_session_gracefully(tmp_path):
    o, ui, clk = ob(tmp_path), FakeUI(per_batch=5), Clock(ts(2026, 11, 22, 12))
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 2

    res = run(ui, session(cfg(swipe_caps={"hinge": 1000})), o, clk, stop=stop)
    assert res["outcome"] == "stopped" and res["swipes"] == 10 and ui.closed == 1


def test_out_of_profiles_ends_the_session_instead_of_spinning(tmp_path):
    ui = FakeUI(supply=7)
    res = run(ui, session(cfg(swipe_caps={"hinge": 100})), ob(tmp_path), Clock(ts(2026, 11, 22, 12)))
    assert res["swipes"] == 7 and res["outcome"] == "done"


def test_a_ui_that_over_reports_is_not_believed(tmp_path):
    class Liar(FakeUI):
        def swipe_batch(self, n): return n + 50

    o = ob(tmp_path)
    with pytest.raises(RuntimeError):
        run(Liar(), session(), o, Clock(ts(2026, 11, 22, 12)))
    assert o.counts()["pending"] == 0 and o.swipes_today("hinge-001", "2026-11-22") == 0


def test_dating_process_with_the_real_unmapped_ui_prints_result_and_reports_nothing(tmp_path):
    conf = tmp_path / "config.toml"
    conf.write_text('[noswipe]\nenabled = true\nquiet_hours = "off"\n')
    outbox = tmp_path / "ob.db"
    p = subprocess.run([sys.executable, str(REPO / "dating.py"), "--user-id", "u1", "--app", "hinge", "--account-ref", "hinge-001",
                        "--minutes", "1"], capture_output=True, text=True, timeout=60, cwd=tmp_path,
                       env={**os.environ, "ENGINE_CONFIG": str(conf), "NOSWIPE_OUTBOX": str(outbox)})
    assert p.returncode == 0, p.stderr
    last = p.stdout.strip().splitlines()[-1]
    assert last.startswith("RESULT ") and json.loads(last[7:])["outcome"] in ("not_mapped", "quiet_hours")
    assert noswipe.Outbox(outbox).counts()["pending"] == 0


# ================================================================ engine wiring + dashboard
NS_CONFIG = TWO_PHONES.replace('hw_serial = "HW-LEFT"', 'hw_serial = "HW-LEFT"\nnoswipe_phone_id = "rack-left"') + """
[noswipe]
enabled = true
quiet_hours = "off"
"""


@pytest.fixture
async def eng(make_engine, srv, monkeypatch):
    monkeypatch.setenv("NOSWIPE_AUTOMATION_KEY", KEY)
    e = make_engine(NS_CONFIG)
    e.NSCFG.base_url = srv.url
    e.NS = None
    return e


async def test_engine_tick_polls_creates_jobs_and_the_runner_picks_them(eng, srv):
    srv.runs["rack-left"] = [srv.run(ref="hinge-001")]
    await eng.noswipe_tick(time.time())
    left = eng.RUNNER_BY_ID["left"]
    job, minutes = left.pick(time.time() + 1)
    assert job["kind"] == "dating_session" and job["phone_id"] == "left" and minutes == 20
    cmd = left.command(job, minutes)
    assert cmd[1:3] == ["dating.py", "--user-id"] and "hinge-001" in cmd and "--minutes" in cmd
    assert eng.RUNNER_BY_ID["right"].pick(time.time() + 1) == (None, None)     # other phone: nothing


async def test_runner_will_not_start_a_session_once_the_run_is_paused(eng, srv):
    srv.runs["rack-left"] = [srv.run(ref="hinge-001")]
    now = time.time()
    await eng.noswipe_tick(now)
    srv.runs["rack-left"] = [srv.run(ref="hinge-001", should=False)]
    eng.NS._next_poll = 0
    await eng.noswipe_tick(now + 60)
    assert eng.RUNNER_BY_ID["left"].pick(now + 61) == (None, None)
    assert eng.db().execute("select status from jobs where kind='dating_session'").fetchone()[0] == "skipped"


async def test_dating_session_does_not_run_over_the_next_post(eng, srv):
    srv.runs["rack-left"] = [srv.run()]
    now = time.time()
    await eng.noswipe_tick(now)
    con = eng.db()
    eng.add_job(con, plan="p", kind="post", app="instagram", account="alpha", run_at=now + 8 * 60, args={}, label="x", phone_id="left")
    con.commit()
    assert eng.RUNNER_BY_ID["left"].pick(now + 1)[0] is None        # 8 min gap - 4 min margin < 5: wait


async def test_dashboard_state_carries_the_noswipe_panel(eng, srv):
    srv.runs["rack-left"] = [srv.run(ref="hinge-001"), srv.run(user="u2", ref="hinge-002", should=False)]
    now = time.time()
    await eng.noswipe_tick(now)
    eng.ns().outbox.add("u1", "hinge", "match", "m-1", now, {"matchRef": "m-1"})
    st = eng.build_state()
    v = next(p for p in st["phones"] if p["id"] == "left")["noswipe"]
    assert v["enabled"] and v["phone_id"] == "rack-left" and v["last_poll"] and not v["stale"]
    assert [(r["accountRef"], r["shouldRun"]) for r in v["runs"]] == [("hinge-001", True), ("hinge-002", False)]
    assert v["outbox"]["pending"] == 1
    assert next(p for p in st["phones"] if p["id"] == "right")["noswipe"]["phone_id"] == "right"


async def test_tick_flushes_the_outbox_to_noswipe(eng, srv):
    now = time.time()
    eng.ns().outbox.add("u1", "hinge", "match", "m-1", now, {"matchRef": "m-1"})
    await eng.noswipe_tick(now)
    assert srv.counts == {"match": 1}


def test_disabled_by_default_changes_nothing(make_engine, monkeypatch):
    e = make_engine(TWO_PHONES)
    assert e.NSCFG.enabled is False
    st = e.build_state()
    assert st["noswipe"] == {"enabled": False} and all(p["noswipe"]["enabled"] is False and p["noswipe"]["outbox"] is None for p in st["phones"])
    assert not (Path(e.ROOT) / "noswipe_outbox.db").exists()          # nothing created on disk
    import asyncio
    asyncio.run(e.noswipe_tick(time.time()))                          # a no-op
    assert e.RUNNER_BY_ID["left"].pick(time.time()) == (None, None)


async def test_enabled_without_the_env_key_stays_idle_and_says_so(make_engine, monkeypatch, srv):
    monkeypatch.delenv("NOSWIPE_AUTOMATION_KEY", raising=False)
    e = make_engine(NS_CONFIG)
    e.NSCFG.base_url = srv.url
    await e.noswipe_tick(time.time())
    assert srv.runs_calls == []
    v = e.build_state()["phones"][0]["noswipe"]
    assert v["enabled"] and v["key_missing"]
