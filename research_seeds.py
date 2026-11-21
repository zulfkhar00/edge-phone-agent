"""Seed keywords for the engine's own research sessions (so research does not depend on the nightly plan alone).

Config (config.toml), off unless `accounts` is set:

    [research_seeds]
    accounts = ["instagram:nomoreawkwardness", "tiktok:nomoreawkwardness"]   # app:handle, must be in [accounts.*]
    per_account_per_day = 3     # keyword sessions queued per account per day
    minutes = 10
    max_likes = 3
    max_comments = 0            # comments are written by the claude CLI; 0 = research only
    after = "10:00"
    before = "22:00"
    keywords = [...]            # optional: replaces the built-in list below

Every queued job is an ordinary research job: it only runs in a gap between fixed jobs and is clipped by the account's
research_minutes_per_day / likes_per_day / comments_per_day, like any plan's research.
"""

from __future__ import annotations

import re
from datetime import date

# What people in NoSwipe's niche search for and post about (2-60 plain ASCII characters, as plan_check requires).
SEED_KEYWORDS = [
    "dating app burnout", "swipe fatigue", "AI dating assistant", "hinge tips", "tinder tips", "bumble",
    "first date ideas", "situationship", "talking stage", "meet cute", "rizz", "dating in your 20s",
    "dating in your 30s", "texting anxiety",
]
ASCII_LINE = re.compile(r"^[\x20-\x7E]+$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def clean_keywords(cfg: dict) -> list[str]:
    kws = cfg.get("keywords") or SEED_KEYWORDS
    return [k for k in kws if isinstance(k, str) and ASCII_LINE.match(k) and 2 <= len(k) <= 60]


def seed_jobs(cfg: dict, day: date, accounts: dict) -> tuple[list[dict], list[str]]:
    """The research jobs to queue for `day`, and problems to report. `accounts` is the parsed [accounts.*] table.

    Keywords rotate with the date, and each account starts at a different place in the list so Instagram and TikTok
    do not search the same terms on the same day. Returns ([{app, account, keyword, minutes, max_likes, max_comments,
    not_before, not_after}], problems)."""
    jobs: list[dict] = []
    problems: list[str] = []
    specs = cfg.get("accounts") or []
    if not specs:
        return jobs, problems
    kws = clean_keywords(cfg)
    if not kws:
        return jobs, ["[research_seeds] has no usable keywords (2-60 plain ASCII characters)"]
    after, before = str(cfg.get("after", "10:00")), str(cfg.get("before", "22:00"))
    if not (HHMM.match(after) and HHMM.match(before)):
        return jobs, ["[research_seeds] after / before must be HH:MM"]
    per = max(1, min(int(cfg.get("per_account_per_day", 3)), 10))
    minutes = max(2.0, min(float(cfg.get("minutes", 10)), 30.0))
    for i, spec in enumerate(specs):
        app, _, handle = str(spec).partition(":")
        app, handle = {"ig": "instagram", "tt": "tiktok"}.get(app.lower(), app.lower()), handle.lstrip("@")
        if app not in ("instagram", "tiktok") or handle not in accounts.get(app, {}):
            problems.append(f"[research_seeds] {spec!r}: not an Instagram / TikTok account listed in [accounts.*]")
            continue
        start = (day.toordinal() * per + i * 5) % len(kws)
        for k in range(min(per, len(kws))):
            jobs.append({"app": app, "account": handle, "keyword": kws[(start + k) % len(kws)], "minutes": minutes,
                         "max_likes": int(cfg.get("max_likes", 3)), "max_comments": int(cfg.get("max_comments", 0)),
                         "not_before": after, "not_after": before})
    return jobs, problems
