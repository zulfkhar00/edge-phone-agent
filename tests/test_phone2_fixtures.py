"""Threads and Instagram flows against REAL uiautomator dumps taken on phone-2 (moto g, 2026-11-28): tests/fixtures/*.xml.

These pin the facts that broke the live runs: Threads' bottom tabs are non-clickable Views (Feed / Messages / Create ...), its
compose field is only an EditText once text is typed and has a bare resource id, Post is a non-clickable TextView, closing
with text asks "Save to drafts?", and Instagram's Reels grid tiles carry any view count."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

import instagram_upload as iu
import phone
import social
from phone import Phone

FIX = Path(__file__).parent / "fixtures"


def parse(xml: str) -> list[dict]:
    out = []
    for m in re.finditer(r"<node [^>]*>", xml):
        a = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(0)))
        b = [int(v) for v in re.findall(r"\d+", a.get("bounds", ""))]
        if len(b) == 4:
            a["label"] = (a.get("content-desc") or a.get("text") or "").strip()
            a["box"] = b
            a["center"] = ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
            out.append(a)
    return out


def load(name: str) -> list[dict]:
    return parse((FIX / f"{name}.xml").read_text())


T = social.THREADS


def test_threads_home_is_home_not_logged_out():
    ns = load("threads_home")
    assert social.is_home(T, ns) and not social.is_logged_out(T, ns)


def test_threads_compose_entry_is_found_even_though_the_tab_is_not_clickable():
    ns = load("threads_home")
    assert social.find_any(ns, T.compose_ids, T.compose_labels, clickable=True) is None  # why clickable=True alone failed live
    btn = social.find_any(ns, T.compose_ids, T.compose_labels)
    assert btn is not None and btn["label"] == "Create" and btn["box"][1] > 1400  # the bottom-bar tab, not the feed


def test_threads_empty_compose_is_recognised_by_its_title_and_empty_field():
    ns = load("threads_compose_empty")
    assert social.edit_field(T, ns)["text"] == ""
    assert social.compose_is_open(T, ns)
    assert social.find_label(ns, T.placeholder_labels) is not None
    assert not social.compose_is_open(T, load("threads_home"))


def test_threads_typed_compose_field_has_a_bare_resource_id():
    ns = load("threads_compose_typed")
    f = social.edit_field(T, ns)
    assert f["text"] == "hello world" and f["resource-id"] == "new_thread_screen_composer"
    assert social.find_id(ns, ("new_thread_screen_composer",)) is f


def test_threads_post_button_is_a_plain_textview():
    ns = load("threads_compose_typed")
    btn = social.post_button(T, ns)
    assert btn is not None and btn["label"] == "Post" and btn["clickable"] == "false"


def test_threads_save_to_drafts_dialog_is_discarded_with_dont_save_never_save():
    ns = load("threads_save_drafts_dialog")
    d = social.exact(ns, ("Delete", "Discard", "Discard post", "Discard thread", "Don’t save", "Don't save"))
    assert d is not None and d["label"] == "Don’t save"


def test_threads_profile_shows_the_handle_and_a_profile_tab():
    ns = load("threads_profile")
    assert "cutpressapp" in {t for t in social.labels_of(ns)}
    assert social.exact(load("threads_home"), ("Profile",), None) is not None


def test_a_notification_sheet_is_not_a_login_screen():
    sheet = parse('<node text="Allow Threads to send you notifications?" bounds="[0,0][10,10]" clickable="false"/>'
                  '<node text="Don’t allow" resource-id="com.android.permissioncontroller:id/permission_deny_button" '
                  'clickable="true" bounds="[0,20][10,30]"/>')
    assert not social.is_logged_out(T, sheet)
    assert social.exact(sheet, T.popups) is not None  # dismissed with "Don’t allow"


def test_instagram_reel_tiles_match_any_view_count():
    ns = load("ig_profile_reels")
    tiles = iu.reel_tiles(ns)
    assert len(tiles) == 1 and "View Count 15" in tiles[0]["content-desc"]
    assert iu.REEL_TILE.match("Reel by cutpressapp. View Count 0. Double tap to play or pause.")
    assert iu.REEL_TILE.match("Reel by cutpressapp. View Count 1.2K. Double tap to play or pause.")


def test_instagram_share_screen_detection_and_profile_tab_on_real_dump():
    ns = load("ig_profile_grid")
    assert not iu.share_screen_open(ns)
    assert any(n.get("resource-id", "").endswith(":id/profile_tab") for n in ns)


# ---------------------------------------------------------------- a whole Threads preview over the real screens
class DumpPhone(Phone):
    """A Phone whose screens are real dumps; `edges` maps (screen, tapped label) -> next screen."""

    def __init__(self, start: str, edges: dict):
        self.serial = "FAKE"
        self.screen, self.edges = start, edges
        self.h = 1604
        self.taps: list[str] = []
        self.text = ""
        self.pressed: list[str] = []
        outer = self

        class D:
            def press(self, key):
                outer.pressed.append(key)
                nxt = outer.edges.get((outer.screen, "<back>"))
                if nxt:
                    outer.screen = nxt

            def app_start(self, pkg, stop=False):
                outer.screen = "threads_home"

            def app_current(self):
                return {"package": "com.instagram.barcelona"}

            def screenshot(self, path):
                Path(path).write_bytes(b"png")

            def clear_text(self):
                pass

            def window_size(self):
                return (720, 1604)

        self.d = D()

    def nodes(self):
        ns = load(self.screen)
        if self.screen == "threads_compose_empty" and self.text:
            self.screen = "threads_compose_typed"
            ns = load(self.screen)
        return ns

    def tap(self, x, y, why):
        print(f"  tap {why} at ({x},{y})", flush=True)
        self.taps.append(why)
        hits = [n for n in self.nodes() if n["box"][0] <= x <= n["box"][2] and n["box"][1] <= y <= n["box"][3]]
        for n in sorted(hits, key=lambda n: (n["box"][2] - n["box"][0]) * (n["box"][3] - n["box"][1])):
            nxt = self.edges.get((self.screen, n["label"]))
            if nxt:
                self.screen = nxt
                return

    def type_text(self, text):
        self.text = text
        self.screen = "threads_compose_typed"

    def wake(self):
        pass

    def adb(self, *a, timeout=300):
        return ""


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch, tmp_path):
    now = [1000.0]
    monkeypatch.setattr(time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(phone, "SHOTS", tmp_path / "runs")


def test_threads_preview_on_real_screens_fills_the_composer_and_never_taps_post_or_save():
    edges = {
        ("threads_home", "Create"): "threads_compose_empty",
        ("threads_home", "Profile"): "threads_profile",
        ("threads_profile", "Feed"): "threads_home",
        ("threads_profile", "Create"): "threads_compose_empty",
        ("threads_compose_typed", "<back>"): "threads_save_drafts_dialog",
        ("threads_save_drafts_dialog", "Don’t save"): "threads_home",
    }
    p = DumpPhone("threads_home", edges)
    res = social.post(p, T, "cutpressapp", "hello world", preview=True)
    assert res["preview"] is True and res["post_button_enabled"]
    assert p.screen == "threads_home"
    assert "Post" not in p.taps and "Don’t save (discard the unfinished post)" in p.taps
    assert not any(t.startswith("Save") for t in p.taps)
