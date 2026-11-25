"""X and Threads flows (social.py) against a fake phone: screen graphs stand in for the apps.

What these tests prove is the flow LOGIC (login checks, no credentials, no paid taps, no drafts, text limits, media
push and clean-up, "tap Post at" only after Post, verification, stats parsing). They cannot prove the real selectors:
those steps are marked LIVE-VERIFY in social.py and flows/x_threads.md."""

from __future__ import annotations

import time

import pytest

import phone
import social
from fake_phone import N, FakePhone

EDIT = "android.widget.EditText"


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch, tmp_path):
    """sleep() advances a fake clock instead of waiting, so timeouts cost nothing; screenshots go to tmp."""
    now = [1000.0]
    monkeypatch.setattr(time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(phone, "SHOTS", tmp_path / "runs")


def tap_names(p: FakePhone) -> str:
    return " | ".join(p.taps)


# ================================================================ X
def x_phone(handle="brand", drawer_handle="brand", logged_in=True, result="ok", promo=False, switch_to=None) -> FakePhone:
    def tap_post(p):
        if result == "refused":
            p.state["error"] = True
            return
        p.state["posts"] = p.state.get("posts", 0) + 1
        if result == "ok":
            p.state["posted"] = p.state["text"]
        p.go("home")

    home = [N("Home", clickable=True), N("Search", clickable=True), N("Notifications", clickable=True), N("Messages", clickable=True),
            N("New post", rid="composer_write", go="compose"), N("Show navigation drawer", rid="toolbar_avatar", go="drawer")]
    drawer = [N(text="Brand"), N(text=f"@{drawer_handle}"), N("Profile", go="profile")]
    if switch_to:
        drawer.append(N("Switch account", go="accounts"))
    drawer += [N("Premium", go="premium")]
    screens = {
        "login": [N("Welcome to X"), N("Log in", go="form"), N("Create account", go="form")],
        "form": [N(cls=EDIT, rid="username_field")],
        "promo": [N("Subscribe to Premium for more reach", go="premium"), N("Not now", go="home")],
        "home": home,
        "drawer": drawer,
        "accounts": [N(text=f"@{drawer_handle}"), N(f"@{switch_to}", go=lambda p: (setattr(p, "start", "home_switched"), p.go("home_switched"))), N("Add an existing account", go="form"),
                     N("Create a new account", go="form")],
        "home_switched": home,
        "premium": [N("Premium")],
        "profile": [lambda p: N(f"Brand @{handle}, 1m, {p.state['posted']}. 1 reply. 2 reposts. 10 likes. 345 views")
                    if p.state.get("posted") else None, N("Posts")],
        "compose": [N(cls=EDIT, rid="tweet_text"), N("Add photos or video", rid="gallery", go="picker"),
                    lambda p: N("Remove media") if p.state.get("media") else None,
                    lambda p: N("Something went wrong. Try again.") if p.state.get("error") else None,
                    N("Post", rid="button_tweet", go=tap_post)],
        "picker": [N("Camera", go="form"), N("Photo, taken today", go=lambda p: p.state.__setitem__("picked", True)),
                   N("Photo, taken last week", go="form"), N("Add", go=lambda p: (p.state.__setitem__("media", True), p.go("compose")))],
        "discard": [N("Save post?"), N("Save", go="home"), N("Delete", go=lambda p: (p.state.__setitem__("discarded", True), p.go("home")))],
    }
    start = "login" if not logged_in else "promo" if promo else "home"
    return FakePhone(screens, start, on_back={"drawer": "home", "compose": "discard", "promo": "home", "accounts": "home"})


def test_x_text_post_end_to_end(capsys):
    p = x_phone()
    result = social.post(p, social.X, "brand", "Shipping the thing today. #buildinpublic")
    assert result["verified"] is True and result["permalink"] is None and result["media"] is None
    assert result["counts"] == {"replies": 1, "reposts": 2, "likes": 10, "views": 345}
    assert p.typed == ["Shipping the thing today. #buildinpublic"]
    assert p.state["posts"] == 1
    assert capsys.readouterr().out.count("tap Post at") == 1  # the engine's "may have gone out" marker, exactly once
    assert "Save" not in p.taps and "Premium" not in tap_names(p)


def test_x_logged_out_needs_login_and_touches_nothing(capsys):
    p = x_phone(logged_in=False)
    with pytest.raises(social.NeedsLogin, match="needs login: X @brand"):
        social.post(p, social.X, "brand", "hello world")
    assert p.typed == [] and p.taps == []  # never typed credentials, never tapped Log in / Create account
    assert "tap Post at" not in capsys.readouterr().out
    assert list((phone.SHOTS).glob("fail-*.png"))  # a screenshot for the owner


def test_x_other_account_is_needs_login_and_never_adds_one():
    p = x_phone(handle="brand", drawer_handle="someoneelse")
    with pytest.raises(social.NeedsLogin, match="needs login"):
        social.post(p, social.X, "brand", "hello world")
    assert p.typed == [] and p.state.get("posts", 0) == 0
    assert not {"Add an existing account", "Create a new account"} & set(p.taps)


def test_x_switches_only_between_logged_in_accounts():
    p = x_phone(drawer_handle="other", switch_to="brand")
    # the switcher lists @brand: it is tapped, never "Add an existing account" / "Create a new account"
    p.screens["home_switched"] = p.screens["home"][:4] + [
        N("New post", rid="composer_write", go="compose"), N("Show navigation drawer", rid="toolbar_avatar", go="drawer2")]
    p.screens["drawer2"] = [N(text="@brand"), N("Profile", go="profile")]
    p.on_back["drawer2"] = "home_switched"
    social.ensure_account(p, social.X, "brand")
    assert "@brand (switch)" in p.taps and not {"Add an existing account", "Create a new account"} & set(p.taps)


def test_x_text_limits_checked_before_the_phone_is_touched():
    p = x_phone()
    with pytest.raises(phone.Abort, match="281 characters; X allows 280"):
        social.post(p, social.X, "brand", "x" * 281)
    with pytest.raises(phone.Abort, match="plain ASCII"):
        social.post(p, social.X, "brand", "emoji \U0001F496 nope")
    assert p.taps == [] and p.started == []
    social.check_text(social.X, "y" * 280)
    social.check_text(social.THREADS, "y" * 500)
    with pytest.raises(phone.Abort):
        social.check_text(social.THREADS, "y" * 501)


def test_x_media_is_pushed_attached_and_removed(tmp_path, capsys):
    img = tmp_path / "shot.jpg"
    img.write_bytes(b"\xff\xd8" + b"0" * 5000)
    p = x_phone()
    result = social.post(p, social.X, "brand", "With a picture today", media=img)
    assert result["media"] == "image" and p.state.get("picked") and p.state.get("media")
    pushes = [c for c in p.adb_calls if c[0] == "push"]
    assert len(pushes) == 1 and pushes[0][1] == str(img) and "/DCIM/" in pushes[0][2]
    assert any(c[:2] == ("shell", "rm") for c in p.adb_calls)  # the copy on the phone is removed again
    assert any("MEDIA_SCANNER_SCAN_FILE" in " ".join(c) for c in p.adb_calls)  # and indexed so the picker shows it


def test_x_media_that_never_attaches_aborts_without_posting(tmp_path):
    img = tmp_path / "shot.jpg"
    img.write_bytes(b"\xff\xd8" + b"0" * 5000)
    p = x_phone()
    p.screens["picker"] = [N("Photo, taken today", go="compose")]  # picking does not attach anything
    with pytest.raises(phone.Abort, match="did not show up attached"):
        social.post(p, social.X, "brand", "With a picture today", media=img)
    assert p.state.get("posts", 0) == 0
    assert any(c[:2] == ("shell", "rm") for c in p.adb_calls)  # clean-up on failure too
    assert p.state.get("discarded") and "Save" not in p.taps  # the half-written post is discarded, never saved as a draft


def test_x_post_that_never_shows_up_is_reported_after_the_post_tap(capsys):
    p = x_phone(result="ghost")  # Post is accepted, the profile never lists it
    with pytest.raises(phone.Abort, match="not on the X profile"):
        social.post(p, social.X, "brand", "Ghost post text here", verify_wait_s=30)
    out = capsys.readouterr().out
    assert "tap Post at" in out  # so the engine marks the job unverified, never retries it
    assert not p.state.get("discarded")  # a published post is not touched


def test_x_refused_post_aborts(capsys):
    p = x_phone(result="refused")
    with pytest.raises(phone.Abort, match="refused the post"):
        social.post(p, social.X, "brand", "Refused post text")
    assert "tap Post at" in capsys.readouterr().out


def test_promo_sheet_is_closed_with_back_never_tapped():
    p = x_phone(promo=True)
    social.ensure_account(p, social.X, "brand")
    assert "back" in p.d.pressed and not any("Subscribe" in t or "Premium" in t for t in p.taps)


# ================================================================ Threads
def threads_phone(handle="brand", logged_in=True, result="ok") -> FakePhone:
    def tap_post(p):
        p.state["posts"] = p.state.get("posts", 0) + 1
        if result == "ok":
            p.state["posted"] = p.state["text"]
        p.go("home")

    tabs = [N("Home", clickable=True), N("Search", clickable=True), N("Activity", clickable=True)]
    screens = {
        # ids copied from the real logged-out screen mapped on phone-2 (2026-11-25)
        "login": [N("Log in with Instagram", cls="x"), N(rid="login_screen_login_with_ig_button", go="form"),
                  N(rid="login_screen_create_ig_account_button", go="form")],
        "form": [N(cls=EDIT)],
        "home": tabs + [N("Create", go="compose"), N("Profile", rid="profile_tab", go="profile")],
        "profile": tabs + [N(text=handle), N("Profile", rid="profile_tab", go="profile"), N("Create", go="compose"),
                           lambda p: N(text=p.state["posted"], box=[0, 600, 700, 800]) if p.state.get("posted") else None,
                           lambda p: N("Like 12", box=[10, 700, 100, 780]) if p.state.get("posted") else None,
                           lambda p: N("Reply 3", box=[110, 700, 200, 780]) if p.state.get("posted") else None,
                           lambda p: N("Repost 1", box=[210, 700, 300, 780]) if p.state.get("posted") else None],
        "compose": [N("New thread"), N(cls=EDIT, rid="text_input"), N("Post", rid="post_button", go=tap_post)],
        "discard": [N("Discard", go="home"), N("Save draft", go="home")],
    }
    return FakePhone(screens, "login" if not logged_in else "home", on_back={"compose": "discard"})


def test_threads_text_post_end_to_end(capsys):
    p = threads_phone()
    result = social.post(p, social.THREADS, "brand", "First thread of the day, building in public.")
    assert result["verified"] and result["app"] == "threads"
    assert result["counts"] == {"likes": 12, "replies": 3, "reposts": 1}
    assert capsys.readouterr().out.count("tap Post at") == 1
    assert "Save draft" not in p.taps


def test_threads_logged_out_screen_is_recognised_from_the_real_ids():
    p = threads_phone(logged_in=False)
    with pytest.raises(social.NeedsLogin):
        social.post(p, social.THREADS, "brand", "hello threads")
    assert p.taps == [] and p.typed == []


def test_threads_wrong_account():
    p = threads_phone(handle="other")
    with pytest.raises(social.NeedsLogin):
        social.post(p, social.THREADS, "brand", "hello threads")
    assert p.state.get("posts", 0) == 0


# ================================================================ stats
def test_parse_counts_x_description():
    lab = "Brand @brand, 2h, text here. 1 reply. 2 reposts. 10 likes. 1.2K views. 3 bookmarks"
    assert social.parse_counts([lab]) == {"replies": 1, "reposts": 2, "likes": 10, "views": 1200, "bookmarks": 3}


def test_parse_counts_threads_buttons_and_missing_numbers():
    assert social.parse_counts(["Like 12", "Reply, 3", "Repost", "Share"]) == {"likes": 12, "replies": 3}
    assert social.parse_counts(["nothing useful here"]) == {}  # never invented, never zero by guess


def test_read_stats_x_finds_the_post_and_reads_its_row():
    p = x_phone()
    social.post(p, social.X, "brand", "Stats target post text")
    social.ensure_account(p, social.X, "brand")  # what stats.py does first
    data = social.read_stats(p, social.X, "Stats target post text")
    assert data["views"] == 345 and data["likes"] == 10 and data["replies"] == 1 and data["reposts"] == 2


def test_read_stats_missing_post_aborts():
    p = x_phone()
    with pytest.raises(phone.Abort, match="no post starting"):
        social.read_stats(p, social.X, "Never posted anything like this")


def test_snapshot_metrics_for_x_and_threads():
    import insights
    m = insights.snap_metrics("x", {"views": 345, "likes": 10, "replies": 1, "reposts": 2})
    assert (m["views"], m["likes"], m["comments"], m["shares"]) == (345, 10, 1, 2)
    assert insights.snap_metrics("threads", {"likes": 5})["views"] is None
