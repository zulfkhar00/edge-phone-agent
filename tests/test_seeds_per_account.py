from datetime import date
import research_seeds as rs

ACC = {"instagram": {"cutpressapp": {}, "nomore": {}}}


def test_per_account_keywords_override():
    cfg = {"accounts": ["instagram:cutpressapp", "instagram:nomore"], "per_account_per_day": 2,
           "keywords_by_account": {"instagram:cutpressapp": ["video editing", "ai video"]}}
    jobs, problems = rs.seed_jobs(cfg, date(2026, 11, 25), ACC)
    assert not problems
    cp = {j["keyword"] for j in jobs if j["account"] == "cutpressapp"}
    other = {j["keyword"] for j in jobs if j["account"] == "nomore"}
    assert cp <= {"video editing", "ai video"}
    assert other and not (other & {"video editing", "ai video"})


def test_bad_override_falls_back():
    cfg = {"accounts": ["instagram:cutpressapp"], "keywords_by_account": {"instagram:cutpressapp": ["é"]}}
    jobs, _ = rs.seed_jobs(cfg, date(2026, 11, 25), ACC)
    assert jobs and all(j["keyword"] in rs.SEED_KEYWORDS for j in jobs)
