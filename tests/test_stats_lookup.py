"""Finding a post on a profile for the stats read: caption matching, scrolling the grid, pinned tiles, end of grid,
and the Instagram flow against a fake phone screen. No phone, no adb. Captions and handles are made up."""

from __future__ import annotations

import sys
import time
import types

import pytest

import stats_lookup as sl
from stats_lookup import NoTiles, NotFound, caption_key, caption_matches, scan_reels


# ---------------------------------------------------------------- caption matching
def test_key_ignores_case_emoji_punctuation_and_whitespace():
    a = caption_key("I interrupted them\nand wanted to disappear \U0001F62D\U0001F62D #fyp")
    b = caption_key("i  INTERRUPTED them and wanted to disappear!!")
    assert a == b and len(a) == 30


def test_key_is_the_first_30_normalised_characters():
    assert caption_key("a" * 100) == "a" * 30
    assert caption_key("\U0001F602 " + "b" * 100) == "b" * 30  # a leading emoji does not eat the key


def test_truncated_caption_with_more_marker_matches():
    full = "He said he would call and then he never called me again after that night"
    key = caption_key(full)
    for shown in ("He said he would call and then he never… more", "He said he would call and then he never... more",
                  "He said he would call and then he never …"):
        assert caption_matches(key, [shown]), shown
    assert caption_matches(key, ["Reels", full + " #love"])  # plain full text
    assert not caption_matches(key, ["He said"])  # too short to be a trustworthy prefix
    assert not caption_matches(key, ["A completely different caption about something else entirely"])


def test_hashtag_only_and_emoji_only_captions():
    assert caption_key("#fyp #love #dating") == "fyplovedating"
    assert caption_matches(caption_key("#fyp #love #dating"), ["#fyp   #love #dating"])
    k = caption_key("\U0001F602\U0001F62D\U0001F480")
    assert k and caption_matches(k, ["\U0001F602 \U0001F62D \U0001F480"])
    assert not caption_matches(k, ["\U0001F602\U0001F602"])
    assert caption_key("") == "" and not caption_matches("", ["anything"])


def test_non_latin_captions_match():
    k = caption_key("Мен оны кездестірдім және бәрі өзгерді … more")
    assert caption_matches(k, ["мен оны   кездестірдім және бәрі өзгерді"])


def test_pinned_label():
    assert sl.is_pinned("Reel by x. Pinned. View Count 10.")
    assert not sl.is_pinned("Reel by x. View Count 10. Unpinned reels")  # only the word "pinned"


# ---------------------------------------------------------------- the scan, on a fake grid
class Grid:
    """A profile grid of 3 columns, 9 tiles on screen, scrolling 6 tiles at a time; opening a tile shows its caption."""

    def __init__(self, captions, pinned=()):
        self.tiles = [(f"Reel by me. {'Pinned. ' if i in pinned else ''}View Count {i}.", c) for i, c in enumerate(captions)]
        self.offset, self.view, self.opened, self.scrolls = 0, None, [], 0

    def get_tiles(self):
        return [{"label": lab} for lab, _ in self.tiles[self.offset:self.offset + 9]]

    def open_tile(self, t, n):
        self.view = next(c for lab, c in self.tiles if lab == t["label"])
        self.opened.append(t["label"])

    def is_match_for(self, key):
        return lambda: caption_matches(key, [self.view or ""])

    def close(self):
        self.view = None

    def scroll(self):
        self.scrolls += 1
        self.offset = min(self.offset + 6, max(0, len(self.tiles) - 9))

    def scan(self, caption, **kw):
        return scan_reels(self.get_tiles, self.open_tile, self.is_match_for(caption_key(caption)), self.close, self.scroll, **kw)


def caps(n):
    return [f"Caption number {i} about a text message" for i in range(n)]


def test_finds_a_reel_far_down_the_grid():
    g = Grid(caps(40))
    assert g.scan(caps(40)[14]) == 15  # opened 15 reels, scrolling twice
    assert g.scrolls >= 1


def test_pinned_tiles_are_skipped_not_opened():
    g = Grid(caps(20), pinned={0, 1})
    n = g.scan(caps(20)[3])
    assert n == 2  # tiles 2 and 3; the two pinned tiles cost nothing
    assert not any("Pinned" in lab for lab in g.opened)


def test_fewer_than_six_reels_missing_post_is_not_found_after_scanning_them_all():
    g = Grid(caps(4))
    with pytest.raises(NotFound, match="4 reels"):
        g.scan("a caption that was never published")
    assert len(g.opened) == 4


def test_deleted_reel_is_not_found_after_a_full_scan():
    g = Grid(caps(25))
    with pytest.raises(NotFound, match="25 reels"):
        g.scan("this reel was deleted by the owner")
    assert len(g.opened) == 25


def test_scan_stops_at_the_limit():
    g = Grid(caps(100))
    with pytest.raises(NotFound, match="30 reels"):
        g.scan("not here", limit=30)


def test_empty_grid_is_a_real_failure_not_not_found():
    g = Grid([])
    with pytest.raises(NoTiles):
        g.scan("anything")


def test_time_budget_is_a_timeout_not_not_found():
    g = Grid(caps(40))
    with pytest.raises(TimeoutError):
        g.scan("not here", deadline=time.time() - 1)


def test_all_pinned_grid_ends():
    g = Grid(caps(3), pinned={0, 1, 2})
    with pytest.raises(NotFound):  # nothing opened but tiles were seen: pinned only
        g.scan("x")


# ---------------------------------------------------------------- the Instagram flow on a fake phone
class FakePhone:
    h = 1600

    def __init__(self, reels, pinned=(), blank=False):
        self.reels, self.pinned, self.blank = reels, set(pinned), blank
        self.offset, self.reel, self.opened, self.shots = 0, None, [], []
        self.d = self

    # uiautomator2 surface used by stats.py
    def app_start(self, *a, **k): ...
    def press(self, key):
        assert key == "back"
        self.reel = None

    def swipe(self, *a, **k):
        self.offset = min(self.offset + 6, max(0, len(self.reels) - 9))

    def screenshot(self, path=None, **k):
        self.shots.append(path)

    # phone.Phone surface
    def wait(self, *a, **k): ...
    def wake(self): ...
    def tap_node(self, n, why):
        self.reel = n["idx"]
        self.opened.append(n["idx"])

    def fail(self, msg):
        raise sys.modules["phone"].Abort(msg)

    def nodes(self):
        if self.blank:
            return []
        if self.reel is not None:
            return [{"label": self.reels[self.reel], "text": self.reels[self.reel], "content-desc": "", "box": [0, 1200, 700, 1300]},
                    {"label": "More", "box": [0, 0, 1, 1]}]
        out = []
        for i in range(self.offset, min(self.offset + 9, len(self.reels))):
            r = i - self.offset
            y = 200 + (r // 3) * 340
            out.append({"label": f"Reel by me. {'Pinned. ' if i in self.pinned else ''}View Count {i}.", "idx": i,
                        "box": [(r % 3) * 240, y, (r % 3) * 240 + 230, y + 330], "center": ((r % 3) * 240 + 115, y + 165)})
        return out


@pytest.fixture
def stats(monkeypatch):
    fake_ig = types.ModuleType("instagram_upload")  # the real one reads config.toml at import
    fake_ig.open_profile_reels = lambda p: None
    monkeypatch.setitem(sys.modules, "instagram_upload", fake_ig)
    import stats as st
    monkeypatch.setattr(st.time, "sleep", lambda s: None)
    return st


def ig_caps(n):
    return [f"Reel caption {i}: something people say after a bad date" for i in range(n)]


def test_ig_flow_finds_a_reel_seven_to_fifteen_tiles_down(stats):
    reels = ig_caps(30)
    p = FakePhone(reels)
    stats.ig_open_post(p, reels[11] + " #fyp")
    assert p.opened[-1] == 11 and len(p.opened) == 12


def test_ig_flow_matches_a_truncated_caption_with_emoji(stats):
    reels = ["The text I sent at 2am \U0001F62D … more"] + ig_caps(5)
    p = FakePhone(reels)
    stats.ig_open_post(p, "The text I sent at 2am \U0001F62D and wish I could take back\n#fyp")
    assert p.opened == [0]


def test_ig_flow_skips_pinned_and_finds_the_next(stats):
    reels = ig_caps(12)
    p = FakePhone(reels, pinned={0})
    stats.ig_open_post(p, reels[2])
    assert 0 not in p.opened and p.opened == [1, 2]


def test_ig_flow_not_found_after_full_scan_has_a_screenshot_path_and_is_terminal(stats):
    reels = ig_caps(14)
    p = FakePhone(reels)
    with pytest.raises(NotFound):
        stats.ig_open_post(p, "this one was deleted from the profile")
    assert sorted(p.opened) == list(range(14))


def test_ig_flow_blank_screen_is_an_abort_that_alerts(stats):
    p = FakePhone(ig_caps(5), blank=True)
    with pytest.raises(sys.modules["phone"].Abort, match="did not load"):
        stats.ig_open_post(p, "anything at all")


def test_main_reports_not_found_with_exit_3_and_a_screenshot(stats, monkeypatch, capsys):
    p = FakePhone(ig_caps(3))
    monkeypatch.setattr(stats, "Phone", lambda serial: p)
    monkeypatch.setattr(sys, "argv", ["stats.py", "instagram", "never published at all"])
    with pytest.raises(SystemExit) as e:
        stats.main()
    out = capsys.readouterr().out
    assert e.value.code == stats.NOT_FOUND_EXIT == 3
    assert out.startswith("NOT_FOUND:") and "screenshot:" in out and "fail-" in out
