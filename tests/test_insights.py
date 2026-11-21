"""Aggregations behind the Post stats / Research tabs and /api/trends, over web/fixtures/insights.json (no phone)."""

from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import insights  # noqa: E402
import phones  # noqa: E402
import research_seeds  # noqa: E402

NOW = 1_800_000_000.0


@pytest.fixture(scope="module")
def data():
    d = insights.resolve_offsets(json.loads((REPO / "web" / "fixtures" / "insights.json").read_text()), NOW)
    return d["posts"], d["research"]


@pytest.fixture(scope="module")
def stats(data):
    return insights.stats_report(data[0], NOW, 7)


def card(report, app, account):
    return next(c for c in report["cards"] if (c["app"], c["account"]) == (app, account))


def test_parse_label():
    assert insights.parse_label("whites-2-b") == {"series": "whites", "part": 2, "variant": "b"}
    assert insights.parse_label("bag-3") == {"series": "bag", "part": 3, "variant": None}
    assert insights.parse_label("video-01")["part"] == 1
    assert insights.parse_label("solo-clip")["series"] is None
    assert insights.parse_label("")["series"] is None


def test_account_card_values_and_deltas(stats):
    c = card(stats, "instagram", "tinyloveshows")
    s, d = c["summary"], c["deltas"]
    assert s["posts"] == 4 and c["prior"]["posts"] == 3
    assert s["measured"] == 4 and s["views_total"] == pytest.approx(19544, rel=0.05)
    assert d["posts"]["pct"] == 33.3 and d["posts"]["diff"] == 1
    assert d["views_total"]["pct"] > 0 and d["views_total"]["prev"] == c["prior"]["views_total"]
    assert d["skip_pct"]["diff"] == pytest.approx(s["skip_pct"] - c["prior"]["skip_pct"], abs=0.01)


def test_unverified_and_unmeasured_posts_stay_out_of_the_maths(stats):
    c = card(stats, "instagram", "noswipe.app")
    assert c["summary"]["posts"] == 8
    assert (c["summary"]["unverified"], c["summary"]["awaiting_stats"], c["summary"]["measured"]) == (1, 2, 5)
    assert c["state"] == "ok"
    states = {p["label"]: p["state"] for p in stats["posts"]}
    assert states["whites-4-a"] == "unverified" and states["solo-clip"] == "no_stats" and states["whites-1-a"] == "ok"


def test_tiktok_has_no_skip_rate_and_uses_detailed_numbers(stats):
    s = card(stats, "tiktok", "nomoreawkwardness")["summary"]
    assert s["skip_pct"] is None and s["avg_watch_s"] is not None and s["follows"] > 0


def test_best_posts_ranked_by_views(stats):
    views = [b["views"] for b in stats["best"]]
    assert views == sorted(views, reverse=True) and stats["best"][0]["label"] == "bag-1-a"
    assert all(b["state"] == "ok" for b in stats["best"]) and stats["best"][0]["rank"] == 1


def test_sparkline_runs_1h_to_latest_snapshot(stats):
    p = next(p for p in stats["posts"] if p["label"] == "whites-1-a")
    assert [x["label"] for x in p["spark"]] == ["1h", "3h", "6h", "12h", "24h", "48h"]
    assert [x["views"] for x in p["spark"]] == sorted(x["views"] for x in p["spark"])
    young = next(p for p in stats["posts"] if p["label"] == "table-3-a")
    assert [x["label"] for x in young["spark"]] == ["1h", "3h", "6h", "12h", "24h"]  # posted 30 h ago


def test_series_grouping_and_ab(stats):
    names = {s["series"]: s for s in stats["series"]}
    assert set(names) == {"whites", "table", "bag"} and "solo-clip" not in names
    assert names["whites"]["summary"]["posts"] == 7
    assert [p["part"] for p in names["whites"]["parts"]] == [1, 2, 3, 4]
    assert names["bag"]["ab"]["winner"] == "a" and names["table"]["ab"]["winner"] == "b"
    pairs = {(p["series"], p["part"]): p for p in stats["ab_pairs"]}
    assert pairs[("whites", 1)]["winner"] == "a" and pairs[("whites", 1)]["lift_pct"] > 100
    assert pairs[("table", 2)]["winner"] == "tie"          # 7.8k vs 7.5k: within 5%
    assert pairs[("whites", 4)]["winner"] is None           # neither variant has stats
    assert ("bag", 3) not in pairs                          # no variants


def test_pick_winner():
    assert insights.pick_winner(100, 50) == ("a", 100.0)
    assert insights.pick_winner(50, 100) == ("b", 100.0)
    assert insights.pick_winner(100, 97)[0] == "tie"
    assert insights.pick_winner(None, 5) == (None, None)


def test_filters_and_window(data):
    ig = insights.stats_report(data[0], NOW, 7, app="instagram")
    assert {c["app"] for c in ig["cards"]} == {"instagram"} and ig["apps"] == ["instagram", "tiktok"]  # options stay complete
    one = insights.stats_report(data[0], NOW, 7, account="nomoreawkwardness")
    assert [c["account"] for c in one["cards"]] == ["nomoreawkwardness"] and "noswipe.app" in one["accounts"]
    wide = insights.stats_report(data[0], NOW, 30)
    assert wide["totals"]["current"]["posts"] == 25 and wide["totals"]["prior"]["posts"] == 0


def test_empty_states(data):
    assert insights.stats_report([], NOW)["empty_reason"] == "no_posts"
    assert insights.stats_report(data[0], NOW + 400 * 86400)["empty_reason"] == "none_in_window"
    only_unverified = [p for p in data[0] if p["label"] == "whites-4-a"]
    r = insights.stats_report(only_unverified, NOW)
    assert r["empty_reason"] == "no_stats" and r["cards"][0]["state"] == "unverified"
    assert insights.research_report([], NOW)["empty_reason"] == "no_research"


def test_classify_formats():
    c = insights.classify
    assert c("POV: you matched")[0] == "pov"
    assert "series" in c("Part 2: the reply")
    assert "choice" in c("Which opener works better, A or B?")
    assert "storytime" in c("Storytime: my ex texted")
    assert "explainer" in c("3 signs you need a break")
    assert c("a quiet sunset") == ["other"]
    assert insights.cta_style("save this for later") == "save_share" and insights.cta_style("hello") == "none"


def test_research_report_shape_and_keywords(data):
    r = insights.research_report(data[1], NOW, 14)
    assert r["totals"]["videos"] == len(data[1]) and r["totals"]["keywords"] == 12
    k = {x["keyword"]: x for x in r["keywords"]}
    assert k["rizz"]["posts_seen"] == sum(1 for v in data[1] if v["keyword"] == "rizz")
    assert len(k["rizz"]["top_creators"]) <= 3 and len(k["rizz"]["top_captions"]) <= 3
    likes = [v["likes"] for v in data[1] if v["keyword"] == "rizz"]
    assert k["rizz"]["median_likes"] == sorted(likes)[len(likes) // 2] or len(likes) % 2 == 0
    assert [f["id"] for f in r["formats"] if f["id"] != "other"]
    assert insights.research_report(data[1], NOW, 1)["totals"]["videos"] < len(data[1])


def test_formats_lift_is_relative_to_the_median(data):
    r = insights.research_report(data[1], NOW, None)
    base = insights.median(v["likes"] for v in data[1])
    for f in r["formats"]:
        assert f["lift"] == pytest.approx(f["median_likes"] / base, abs=0.01)


def test_rising_themes_need_growth():
    def v(i, age_days, cap):
        return {"id": i, "app": "tiktok", "keyword": "k", "seen_at": NOW - age_days * 86400, "creator": f"c{i}", "caption": cap, "likes": 10}
    vids = [v(1, 1, "#hot thing"), v(2, 2, "#hot thing"), v(3, 3, "#hot thing"), v(4, 9, "#hot thing"),
            v(5, 1, "#cold"), v(6, 2, "#cold"), v(7, 9, "#cold"), v(8, 10, "#cold"), v(9, 11, "#cold")]
    rising = insights.rising_themes(insights.prep_videos(vids, NOW, None, None), NOW, 7)
    themes = {r["theme"]: r for r in rising}
    assert themes["hot"]["recent"] == 3 and themes["hot"]["prior"] == 1 and not themes["hot"]["new"]
    assert "cold" not in themes                            # 2 recent < 3 before


def test_promotion_ideas_cite_real_videos(data):
    r = insights.research_report(data[1], NOW, 14)
    assert r["ideas"]
    captions = {v["caption"][:140].strip() for v in data[1]}
    for i in r["ideas"]:
        assert i["lift"] >= 1 and i["angle"] and i["cta_label"]
        assert any(c.startswith(i["evidence"]["caption"].rstrip("…")) for c in captions)


def test_trends_shape(data):
    t = insights.trends(data[1], NOW, days=14, limit=5)
    assert set(t) >= {"generated_at", "window_days", "source", "trends", "formats", "product"}
    assert 1 <= len(t["trends"]) <= 5 and t["source"]["videos"] == len(data[1])
    scores = [x["score"] for x in t["trends"]]
    assert scores == sorted(scores, reverse=True) and scores[0] == 100.0
    for i, x in enumerate(t["trends"], 1):
        assert x["rank"] == i and x["kind"] in ("keyword", "hashtag", "phrase") and x["trending"] in ("rising", "steady")
        assert 1 <= len(x["example_posts"]) <= 3 and x["suggested_angle"] and x["creators"] >= 2
        assert set(x["example_posts"][0]) >= {"app", "creator", "caption", "likes", "comments"}
    json.dumps(t)
    assert insights.trends([], NOW)["trends"] == []
    assert all(x["example_posts"][0]["app"] == "tiktok" for x in insights.trends(data[1], NOW, app="tiktok")["trends"])


def test_trends_endpoint_returns_that_shape(make_engine, tmp_path):
    import asyncio

    eng = make_engine()
    con = __import__("sqlite3").connect(tmp_path / "research.db")
    con.execute("create table videos (id integer primary key, app text, keyword text, seen_at text, creator text, caption text, "
                "likes integer, comments integer, shares integer, sound text, relevant integer, liked integer, commented text, account text)")
    for i in range(4):
        con.execute("insert into videos values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i, "tiktok", "rizz", time.strftime("%Y-%m-%d %H:%M:%S"), f"c{i % 2}", f"POV: rizz #rizz {i}", 100 * (i + 1), 5, 2, "", 1, 0, None, None))
    con.commit()
    con.close()

    class Req:
        query = {"limit": "3"}

    body = json.loads(asyncio.run(eng.api_trends(Req())).text)
    assert body["trends"][0]["theme"] == "rizz" and body["source"]["videos"] == 4


# ---- X / Reddit accounts and seeded research
def test_x_and_reddit_accounts_parse():
    assert phones.parse_account("x:@handle") == ("x", "handle")
    assert phones.parse_account("twitter:handle") == ("x", "handle")
    assert phones.parse_account("reddit:u/some_name") == ("reddit", "some_name")
    assert phones.parse_account("reddit:/u/Other") == ("reddit", "Other")
    assert "x" not in phones.AUTOMATED_APPS and "reddit" not in phones.AUTOMATED_APPS


def test_listed_accounts_show_in_state_without_jobs(make_engine):
    cfg = """
[[phones]]
id = "p1"
hw_serial = "HW1"
accounts = ["ig:@alpha", "x:@brand", "reddit:u/brand"]
"""
    from conftest import BASE
    eng = make_engine(BASE + cfg)
    accts = eng.build_state()["phones"][0]["accounts"]
    listed = [a for a in accts if a["app"] in ("x", "reddit")]
    assert {(a["app"], a["account"]) for a in listed} == {("x", "brand"), ("reddit", "brand")}
    assert all(a["login"] == "unknown" and a["automated"] is False and a["note"] for a in listed)
    assert all(a["automated"] for a in accts if a["app"] == "instagram")


def test_seed_jobs_rotate_and_respect_config():
    accounts = {"instagram": {"a": {}}, "tiktok": {"a": {}}}
    cfg = {"accounts": ["instagram:a", "tt:@a", "instagram:ghost"], "per_account_per_day": 3}
    jobs, problems = research_seeds.seed_jobs(cfg, date(2026, 11, 21), accounts)
    assert len(jobs) == 6 and len(problems) == 1 and "ghost" in problems[0]
    ig = [j["keyword"] for j in jobs if j["app"] == "instagram"]
    tt = [j["keyword"] for j in jobs if j["app"] == "tiktok"]
    assert len(set(ig)) == 3 and set(ig) != set(tt)
    jobs2, _ = research_seeds.seed_jobs(cfg, date(2026, 11, 22), accounts)
    assert [j["keyword"] for j in jobs2 if j["app"] == "instagram"] != ig
    assert research_seeds.seed_jobs({}, date(2026, 11, 21), accounts) == ([], [])
    assert all(2 <= len(k) <= 60 and k.isascii() for k in research_seeds.SEED_KEYWORDS)
    for kw in ("dating app burnout", "AI dating assistant", "hinge tips", "tinder tips", "bumble", "first date ideas", "situationship",
               "talking stage", "meet cute", "rizz", "dating in your 20s", "dating in your 30s"):
        assert kw in research_seeds.SEED_KEYWORDS
