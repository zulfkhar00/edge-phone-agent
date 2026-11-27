"""The sheets between Instagram's share-screen button and the published reel (instagram_upload.finish_share_sheets), on a fake phone."""

from __future__ import annotations

import pytest

import instagram_upload as iu
from fake_phone import N, FakePhone
from phone import Abort


def phone(first: str, **extra) -> FakePhone:
    published = lambda p: (p.state.__setitem__("published", True), p.go("profile"))  # noqa: E731
    screens = {
        "about": [N("About Reels"), N("Share", rid="clips_nux_sheet_share_button", go=extra.get("after_about", published)), N("Cancel", go="share")],
        "meta": [N("Update on your original audio"), N("Turn off and share", go=published), N("Dismiss", go="share"),
                 N("Share", rid="clips_original_audio_nux_sheet_share_button", go=published)],
        "share": [N("New reel"), N("Next", rid="share_button", go="about")],
        "profile": [N("Profile")],
    }
    return FakePhone(screens, first)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(iu.time, "sleep", lambda s: None)
    monkeypatch.setitem(iu.CFG, "instagram", {"meta_ai_audio": "off"})


def test_the_about_reels_sheet_of_a_new_account_is_confirmed_with_its_share_button(capsys):
    p = phone("about")
    iu.finish_share_sheets(p)
    assert p.state.get("published") is True and p.taps == ["Share (About Reels notice)"]


def test_about_reels_then_the_meta_ai_notice_are_both_handled(monkeypatch):
    p = phone("about", after_about="meta")
    iu.finish_share_sheets(p)
    assert p.taps == ["Share (About Reels notice)", "Turn off and share (Meta AI audio off)"] and p.state.get("published") is True


def test_the_meta_ai_notice_alone_follows_the_owners_choice_and_refuses_without_one():
    p = phone("meta")
    iu.finish_share_sheets(p)
    assert p.taps == ["Turn off and share (Meta AI audio off)"]
    iu.CFG["instagram"] = {}
    q = phone("meta")
    with pytest.raises(Abort, match="meta_ai_audio"):
        iu.finish_share_sheets(q)
    assert "published" not in q.state


def test_no_sheet_at_all_just_returns(monkeypatch):
    t = iter(range(0, 1000, 5))
    monkeypatch.setattr(iu.time, "time", lambda: next(t))
    p = phone("profile")
    iu.finish_share_sheets(p)
    assert p.taps == []
