"""Hub bridge (hub_rig.py) against a fake Hub over real HTTP, inside a throwaway engine: poll -> download -> plan folder -> the
engine's own import -> jobs -> results back (retry outbox, idempotent), previews, refusals, config rules. No phone is touched."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time

import pytest

import hub_rig
import phones
from conftest import BASE
from fake_hub import FakeHub

TOKEN = "t" * 32
CONFIG = BASE + """
[accounts.instagram.cutpress]
use = "practice"
posts_per_day = 2

[accounts.threads.cutpress]
use = "practice"
posts_per_day = 3

[accounts.x.cutpress]
use = "practice"
posts_per_day = 3

[[phones]]
id = "left"
hw_serial = "HW-LEFT"
accounts = ["ig:@alpha", "ig:@cutpress", "threads:@cutpress"]

[[phones]]
id = "right"
hw_serial = "HW-RIGHT"
accounts = ["ig:@beta", "x:@cutpress"]
"""


@pytest.fixture
def hub():
    h = FakeHub(TOKEN)
    yield h
    h.close()


@pytest.fixture
def rig(make_engine, hub, tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_RIG_TOKEN", TOKEN)
    eng = make_engine(CONFIG)
    alerts: list[tuple[str, str]] = []

    def build(**cfg):
        c = hub_rig.HubCfg(enabled=True, base_url=hub.url, poll_every_min=0.05, flush_every_s=0, **cfg)
        return hub_rig.Integration(c, eng.PHONES, hub_rig.Store(tmp_path / "hub_rig.db"), eng.INBOX, eng.check_plan_now, eng.db,
                                   lambda kind, text, every_min=30: alerts.append((kind, text)), log=lambda *_: None)

    r = build()
    r.eng, r.alerts, r.build = eng, alerts, build
    return r


def vertical_video(tmp_path) -> bytes:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg needed for a real vertical video")
    out = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=25", "-t", "4", "-b:v", "900k", "-pix_fmt", "yuv420p",
                    "-y", str(out)], check=True)
    return out.read_bytes()


def item(hub, qid=101, platform="threads", handle="cutpress", caption="One sentence in, a thirty second animated video out. cutpress.app",
         media=None, attempt=1, minutes=10, **extra):
    d = {"key": f"{qid}:{platform}", "queue_id": qid, "platform": platform, "app": platform, "handle": handle, "caption": caption, "kind": "text" if not media else "video",
         "scheduled_at": int((time.time() + minutes * 60) * 1000), "attempt": attempt, "media": media or [], "sha256": None, "dry": False,
         "_phone": "left", **extra}
    hub.items.append(d)
    return d


def jobs(rig, kind="post"):
    con = rig.eng.db()
    try:
        return [dict(r) for r in con.execute("select * from jobs where kind=? order by id", (kind,))]
    finally:
        con.close()


def set_job(rig, job_id, status, **f):
    con = rig.eng.db()
    con.execute(f"update jobs set status=?{''.join(', ' + k + '=?' for k in f)} where id=?", (status, *f.values(), job_id))
    con.commit()
    con.close()


# ================================================================ pure rules
def test_hub_is_off_by_default_and_the_token_only_comes_from_the_environment(monkeypatch):
    c = hub_rig.HubCfg.from_cfg({})
    assert c.enabled is False and c.base_url == "https://hub.codenrap.com" and c.token_env == "HUB_RIG_TOKEN"
    c = hub_rig.HubCfg.from_cfg({"hub": {"enabled": True, "token": "leaked-in-the-file"}})
    monkeypatch.delenv("HUB_RIG_TOKEN", raising=False)
    assert c.token == ""
    monkeypatch.setenv("HUB_RIG_TOKEN", " secret ")
    assert c.token == "secret"


@pytest.mark.parametrize("bad", [{"base_url": "http://hub.example.com"}, {"poll_every_min": 0}, {"horizon_min": 0}])
def test_bad_hub_config_is_refused(bad):
    with pytest.raises(phones.ConfigError):
        hub_rig.HubCfg.from_cfg({"hub": bad})


def test_localhost_http_is_allowed_for_tests_only():
    assert hub_rig.HubCfg.from_cfg({"hub": {"base_url": "http://127.0.0.1:9/"}}).base_url == "http://127.0.0.1:9"


@pytest.mark.parametrize("raw, want", [
    ("It’s “free” — try it …", 'It\'s "free" - try it ...'),
    ("two\n\nlines   here", "two lines here"),
    ("emoji \U0001F3AC and tags #cutpress #animation", "emoji and tags #cutpress #animation"),
    ("Do you use only 10% of your brain?", "Do you use only 10 percent of your brain?"),
    ("50 % off", "50 percent off"),
])
def test_ascii_caption_is_one_typeable_line(raw, want):
    assert hub_rig.ascii_caption(raw) == want


def test_local_slot_stays_in_the_posting_window_and_never_in_the_past():
    from datetime import datetime
    now = datetime(2026, 11, 26, 12, 0).timestamp()
    assert hub_rig.local_slot(datetime(2026, 11, 26, 13, 30).timestamp(), now) == ("2026-11-26", "13:30")
    assert hub_rig.local_slot(datetime(2026, 11, 26, 11, 0).timestamp(), now)[1] == "12:02"      # overdue: in two minutes
    assert hub_rig.local_slot(datetime(2026, 11, 27, 6, 0).timestamp(), now) == ("2026-11-27", "09:00")
    assert hub_rig.local_slot(datetime(2026, 11, 26, 23, 40).timestamp(), now) == ("2026-11-27", "09:00")


# ================================================================ end to end with a fake Hub
def test_a_text_item_becomes_a_normal_plan_job_and_its_result_goes_back(rig, hub, tmp_path):
    item(hub, 101, "threads")
    rig.tick()
    assert hub.plan_calls and hub.plan_calls[0]["phone"] in ("left", "right")
    assert set(hub.tokens_seen) == {f"Bearer {TOKEN}"}
    folder = rig.eng.INBOX / "hub-101-a1"
    plan = json.loads((folder / "plan.json").read_text())
    assert plan["source"] == "hub" and plan["posts"][0]["targets"] == [{"app": "threads", "account": "cutpress"}] and "preview" not in plan
    rig.eng.import_plans()
    (job,) = jobs(rig)
    assert (job["app"], job["account"], job["phone_id"], job["status"]) == ("threads", "cutpress", "left", "waiting")
    a = json.loads(job["args"])
    assert a["text"].startswith("One sentence in") and "preview" not in a
    rig.tick(); rig.eng.import_plans()
    assert len(jobs(rig)) == 1                                            # nothing is imported twice
    set_job(rig, job["id"], "done", result=json.dumps({"verified": True, "permalink": "https://www.threads.com/@cutpress/post/abc"}), ended_at=time.time())
    rig.tick()
    assert hub.results == [{"queue_id": 101, "platform": "threads", "status": "posted", "detail": "verified on the profile",
                            "permalink": "https://www.threads.com/@cutpress/post/abc"}]
    rig.tick()
    assert len(hub.results) == 1                                          # reported once
    assert rig.store.counts()["results"] == {"pending": 0, "sent": 1, "dead": 0}


def test_video_is_downloaded_without_the_token_checked_and_posted_as_instagram_reel(rig, hub, tmp_path):
    blob = vertical_video(tmp_path)
    item(hub, 102, "instagram", media=[{"url": hub.media_url("102.mp4", blob), "name": "102.mp4", "kind": "video"}],
         sha256=hashlib.sha256(blob).hexdigest(), caption="Make this free at cutpress.app #animation #cutpress")
    rig.tick()
    assert hub.media_auth_seen == [None]                                   # the signed URL is the credential; the token never goes with it
    assert (rig.eng.INBOX / "hub-102-a1" / "media" / "hub-102.mp4").read_bytes() == blob
    rig.eng.import_plans()
    (job,) = jobs(rig)
    a = json.loads(job["args"])
    assert (job["app"], job["account"]) == ("instagram", "cutpress") and a["video"].endswith("hub-102.mp4") and a["audience"] == "everyone"
    assert not hub.results


def test_a_failed_job_is_reported_failed_with_a_clean_reason_and_unverified_stays_unverified(rig, hub):
    item(hub, 103, "threads", caption="First failing item for the rig test, cutpress.app")
    item(hub, 104, "threads", caption="Second item that may have gone out, cutpress.app")
    rig.tick(); rig.eng.import_plans()
    j1, j2 = jobs(rig)
    set_job(rig, j1["id"], "failed", error="compose screen never opened (screenshot: /home/pi/content-engine/runs/fail-1.png)")
    set_job(rig, j2["id"], "unverified", error="published but not verified: not on the profile after 150 s")
    rig.tick()
    got = {r["queue_id"]: r for r in hub.results}
    assert got[103]["status"] == "failed" and got[103]["detail"] == "compose screen never opened (screenshot on the rig)"
    assert got[104]["status"] == "unverified" and "not verified" in got[104]["detail"]
    assert not any("permalink" in r for r in hub.results)


def test_a_dashboard_retry_that_later_posts_is_reported_as_posted_after_the_failure(rig, hub):
    item(hub, 105, "threads")
    rig.tick(); rig.eng.import_plans()
    (j,) = jobs(rig)
    set_job(rig, j["id"], "failed", error="boom")
    rig.tick()
    assert [r["status"] for r in hub.results] == ["failed"]
    set_job(rig, j["id"], "done", result="{}")                             # Retry on the dashboard worked
    rig.tick()
    assert [r["status"] for r in hub.results] == ["failed", "posted"]


def test_results_are_retried_with_backoff_when_hub_is_down_and_dropped_when_hub_refuses_them(rig, hub):
    item(hub, 106, "threads")
    rig.tick(); rig.eng.import_plans()
    (j,) = jobs(rig)
    set_job(rig, j["id"], "done", result="{}")
    hub.fail_results = 2
    now = time.time()
    rig.tick(now)
    assert rig.store.counts()["results"]["pending"] == 1 and not hub.results
    rig.tick(now + 1)                                                      # still inside the backoff: not even tried
    assert hub.fail_results == 1
    rig.tick(now + 40); rig.tick(now + 200)
    assert [r["queue_id"] for r in hub.results] == [106]
    # a result Hub refuses for good (409: never handed over / 404: row gone) is dead, alerted once, never retried
    item(hub, 107, "threads", caption="Hub will refuse this one, cutpress.app")
    rig.tick(now + 300); rig.eng.import_plans()
    j2 = jobs(rig)[-1]
    set_job(rig, j2["id"], "done", result="{}")
    hub.result_status = 409
    rig.tick(now + 400)
    assert rig.store.counts()["results"]["dead"] == 1
    assert any(k == "hub" and "refused" in t for k, t in rig.alerts)


def test_an_account_the_phone_does_not_own_and_a_plan_check_rejection_are_refused_to_hub_not_imported(rig, hub):
    item(hub, 108, "instagram", handle="someoneelse", media=[{"url": hub.media_url("x.mp4", b"0" * 200_000), "name": "x.mp4", "kind": "video"}])
    item(hub, 109, "x", handle="cutpress", caption="x" * 300)             # over X's 280 characters (X lives on the other phone)
    item(hub, 110, "instagram", handle="cutpress")                          # Instagram without a video
    rig.tick()
    assert not list(rig.eng.INBOX.glob("hub-*/plan.json"))
    got = {r["queue_id"]: r for r in hub.results}
    assert "no instagram account @someoneelse on left" in got[108]["detail"]
    assert got[110]["detail"] == "Instagram posts from the rig need a video"
    assert all(r["status"] == "failed" for r in hub.results)
    # the over-long X post is asked of phone `right` (its owner), on its own poll; here it is the plan check that refuses it
    assert "109" not in got or "280" in got[109]["detail"]


def test_a_bad_checksum_or_foreign_media_url_is_refused_and_leaves_no_files(rig, hub, tmp_path):
    blob = b"0" * 200_000
    item(hub, 111, "instagram", media=[{"url": hub.media_url("a.mp4", blob), "name": "a.mp4", "kind": "video"}], sha256="0" * 64)
    item(hub, 112, "instagram", caption="Foreign media host should be refused, cutpress.app",
         media=[{"url": "http://evil.example.com/pub/media/a.mp4", "name": "a.mp4", "kind": "video"}])
    rig.tick()
    got = {r["queue_id"]: r["detail"] for r in hub.results}
    assert "checksum" in got[111] and "not on" in got[112]
    assert not list(rig.eng.INBOX.glob("hub-*"))


def test_a_missing_signed_file_is_final_but_a_network_hiccup_is_retried_on_the_next_poll(rig, hub, monkeypatch):
    item(hub, 113, "instagram", media=[{"url": hub.url + "/pub/media/sig/missing.mp4", "name": "m.mp4", "kind": "video"}])
    rig.tick()
    assert [(r["queue_id"], r["status"]) for r in hub.results] == [(113, "failed")] and "media HTTP 404" in hub.results[0]["detail"]
    blob = b"0" * 200_000
    item(hub, 118, "instagram", caption="Network hiccup while downloading, cutpress.app", media=[{"url": hub.media_url("n.mp4", blob), "name": "n.mp4", "kind": "video"}])
    real = hub_rig.Client.download
    monkeypatch.setattr(hub_rig.Client, "download", lambda *a, **k: (_ for _ in ()).throw(hub_rig.HubError("media network: URLError")))
    rig.tick(time.time() + 10)
    assert [r["queue_id"] for r in hub.results] == [113] and not list(rig.eng.INBOX.glob("hub-118*")) and not rig.store.seen("118:instagram", 1)
    monkeypatch.setattr(hub_rig.Client, "download", real)
    rig.tick(time.time() + 20)
    assert rig.store.seen("118:instagram", 1)


def test_dry_run_downloads_and_plans_a_preview_job_that_never_posts_and_reports_nothing(rig, hub, tmp_path):
    blob = vertical_video(tmp_path)
    item(hub, 114, "instagram", media=[{"url": hub.media_url("p.mp4", blob), "name": "p.mp4", "kind": "video"}], caption="Preview only, nothing is posted, cutpress.app")
    r = rig.build(dry_run=True)
    r.tick()
    assert hub.plan_calls[-1].get("dry") == "1"
    assert json.loads((rig.eng.INBOX / "hub-114-a1" / "plan.json").read_text())["preview"] is True
    rig.eng.import_plans()
    (job,) = jobs(rig)
    assert json.loads(job["args"])["preview"] is True
    cmd = rig.eng.Runner.command(type("R", (), {})(), job, None)
    assert cmd[1] == "instagram_upload.py" and cmd[-1] == "--preview"
    set_job(rig, job["id"], "skipped", error="preview: compose screen filled, nothing was posted")
    r.tick()
    assert hub.results == [] and rig.eng.used_today("instagram", "cutpress")["posts"] == 0
    assert r.store.counts()["items"] == {"preview": 1}


def test_a_new_attempt_from_hubs_retry_button_makes_a_new_plan_and_the_same_attempt_never_does(rig, hub):
    item(hub, 115, "threads", caption="Retried from Hub after a failure, cutpress.app")
    rig.tick(); rig.eng.import_plans()
    (j,) = jobs(rig)
    set_job(rig, j["id"], "failed", error="no luck")
    rig.tick()
    hub.items[0]["attempt"] = 2                                            # Hub: failed -> Retry -> handed over again
    rig.tick(time.time() + 10); rig.eng.import_plans()
    assert (rig.eng.INBOX / "hub-115-a2" / "plan.json").exists()
    assert [(x["status"], x["id"] == j["id"]) for x in jobs(rig)] == [("failed", True), ("waiting", False)]   # a failed caption may be reused
    rig.tick(time.time() + 20)
    assert len(jobs(rig)) == 2


def test_hub_down_or_token_missing_changes_nothing_and_alerts_once(rig, hub, monkeypatch):
    hub.fail_plan = 1
    item(hub, 116, "threads")
    rig.tick()
    assert not list(rig.eng.INBOX.glob("hub-*")) and rig.last_error and any(k == "hub" for k, _ in rig.alerts)
    rig.tick(time.time() + 60)                                              # recovered
    assert (rig.eng.INBOX / "hub-116-a1").exists() and rig.last_error is None
    monkeypatch.delenv("HUB_RIG_TOKEN")
    assert rig.key_missing and not rig.active


def test_two_platforms_of_one_row_share_one_plan_and_one_caption(rig, hub, tmp_path):
    blob = vertical_video(tmp_path)
    media = [{"url": hub.media_url("117.mp4", blob), "name": "117.mp4", "kind": "video"}]
    cap = "Same row on Instagram and Threads, cutpress.app"
    item(hub, 117, "instagram", caption=cap, media=media)
    item(hub, 117, "threads", caption=cap, media=media)
    rig.tick(); rig.eng.import_plans()
    assert [p.name for p in rig.eng.INBOX.glob("hub-117-*")] == ["hub-117-a1"]
    assert sorted((j["app"], j["account"]) for j in jobs(rig)) == [("instagram", "cutpress"), ("threads", "cutpress")]


def test_only_hub_plans_may_use_practice_accounts_outside_practice_plans(tmp_path):
    from datetime import date

    import plan_check
    cfg = {"accounts": {"threads": {"cutpress": {"use": "practice", "posts_per_day": 3}}}}
    post = {"id": "p", "caption": "A hub post for the check, cutpress.app", "at": "12:00", "targets": [{"app": "threads", "account": "cutpress"}]}
    day = date(2026, 11, 26)
    plain, problems = plan_check.check_plan({"day": "2026-11-26", "posts": [post]}, tmp_path, cfg, set(), {}, today=day)
    assert plain["posts"] == [] and any("not for nightly plans" in p for p in problems)
    ok, problems = plan_check.check_plan({"day": "2026-11-26", "source": "hub", "posts": [post]}, tmp_path, cfg, set(), {}, today=day)
    assert len(ok["posts"]) == 1 and not problems


def test_audience_is_read_from_both_instagram_layouts():
    import instagram_upload as iu
    old = [{"label": "Audience", "resource-id": ""}, {"label": "Everyone", "resource-id": "com.instagram.android:id/inline_subtitle"}]
    new = [{"label": "Rename audio"}, {"label": "Original Audio"}, {"label": "Audience"}, {"label": "Close Friends"}]
    assert iu.audience_value(old) == "Everyone" and iu.audience_value(new) == "Close Friends"
    assert iu.audience_value([{"label": "Audience"}, {"label": "Tag people"}]) == ""


def test_a_preview_item_is_scheduled_for_now_not_for_its_hub_slot(rig, hub, tmp_path):
    it = item(hub, 119, "threads", caption="Preview is scheduled right away, cutpress.app", minutes=24 * 60)
    it["dry"] = True
    rig.tick()
    plan = json.loads((rig.eng.INBOX / "hub-119-a1" / "plan.json").read_text())
    assert plan["preview"] is True
    from datetime import datetime
    at = datetime.strptime(f"{plan['day']} {plan['posts'][0]['at']}", "%Y-%m-%d %H:%M")
    assert abs((at - datetime.now()).total_seconds()) < 26 * 3600 and plan["day"] == hub_rig.local_slot(time.time(), time.time())[0]
