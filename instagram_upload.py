"""Upload one video as an Instagram Reel on the phone, end to end, checking every screen before tapping.

Steps (see flows/instagram_upload.md): copy to gallery -> Home "+" -> New reel gallery -> newest video
(must be dated today) -> editor Next -> share screen (caption, audience, all toggles off) -> Share ->
confirm it is at the top of the Home feed. Facebook, Your story, AI label, Save draft, location and
tag-people are never touched; if any toggle is on, the run aborts instead of guessing.

Usage:
  python instagram_upload.py VIDEO.mp4 --caption "text" --audience everyone
  python instagram_upload.py VIDEO.mp4 --caption "test" --audience close_friends --delete-after   # test mode
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import tomllib
from datetime import date
from pathlib import Path

from phone import SERIAL, Abort, Phone

PKG = "com.instagram.android"
_CONFIG = Path(__file__).parent / "config.toml"
CFG = tomllib.loads(_CONFIG.read_text()) if _CONFIG.exists() else {}  # missing only in CI / tests
AUDIENCE = {"everyone": "Everyone", "close_friends": "Close Friends"}
POPUPS = ("No, thanks", "Not now", "Not Now", "Remind me later")  # dismiss buttons for nag dialogs


PROMO_SHEETS = ("Stand out with a verified badge", "Meta One", "Meta Verified")  # paid-upsell sheets: close, never tap


def dismiss_popups(p: Phone) -> None:
    for _ in range(3):
        ns = p.nodes()
        if any(n["label"].startswith(PROMO_SHEETS) for n in ns):
            print("  closing a promo sheet with Back (never its button)", flush=True)
            p.d.press("back")
            time.sleep(1.5)
            continue
        hit = next((n for n in ns if n["label"] in POPUPS and n.get("clickable") == "true"), None)
        if hit is None:
            return
        p.tap_node(hit, f"dismiss '{hit['label']}'")


def discard_unfinished(p: Phone) -> None:
    """If a half-finished reel from an earlier aborted run is waiting, throw it away (never save it as a draft)."""
    if not p.find("Keep editing your draft?"):
        return
    p.tap_node(p.find("Keep editing", clickable=True), "Keep editing (to reach the discard option)")
    time.sleep(3)
    discard_session(p)


def discard_session(p: Phone) -> None:
    """Back out of an unfinished reel (share screen or editor) and choose Start over, never Save draft."""
    for _ in range(4):
        start_over = p.find("Start over")
        if start_over:
            p.tap_node(start_over, "Start over (discard)")
            time.sleep(2)
            return
        if p.find("New reel") and p.find("REEL"):  # back in the empty gallery: nothing to discard
            return
        p.d.press("back")
        time.sleep(2)
    p.fail("could not reach 'Start over' to discard the unfinished reel")


def open_profile_reels(p: Phone) -> None:
    """Profile tab, then the Reels tab of the profile grid (tapped once: a second tap opens a sort menu)."""
    p.wake()
    dismiss_popups(p)
    tab = p.by_id("profile_tab")
    if tab is None:  # a sheet may cover the tab bar
        dismiss_popups(p)
        time.sleep(1)
        tab = p.by_id("profile_tab")
    p.tap_node(tab, "Profile tab")
    p.wait("profile", "Profile", timeout=20)
    tabs = None
    for _ in range(10):  # the grid's tab row renders a moment after the header
        tabs = p.by_id("profile_tab_layout")
        if tabs:
            break
        time.sleep(1.5)
        p.wake()
    if tabs is None:
        p.fail("profile tabs not found")
    x1, y1, x2, y2 = tabs["box"]
    p.tap(x1 + (x2 - x1) * 3 // 8, (y1 + y2) // 2, "profile Reels tab")
    time.sleep(2)
    if p.find("Most viewed"):  # the tab was already selected; tapping again opened its sort menu
        p.d.press("back")
        time.sleep(1)


def audience_value(ns: list[dict]) -> str:
    """The audience the share screen currently shows ("Everyone" / "Close Friends"). Older Instagram builds put it in an
    `inline_subtitle` node; newer ones (phone-2, Nov 2026) list the "Audience" label and its value as consecutive nodes."""
    by_id = next((n["label"] for n in ns if n.get("resource-id", "").endswith(":id/inline_subtitle")), "")
    if by_id:
        return by_id
    labels = [n["label"] for n in ns]
    if "Audience" in labels:
        nxt = labels[labels.index("Audience") + 1:][:1]
        if nxt and nxt[0] in AUDIENCE.values():
            return nxt[0]
    return ""


def keyboard_open(p: Phone) -> bool:
    return any(n.get("resource-id", "").endswith(("key_pos_space", "input_method_nav_back")) or n["label"] == "Space"
               for n in p.nodes())


def scroll(p: Phone, down: bool = True) -> None:
    if keyboard_open(p):
        p.fail("refusing to scroll while the keyboard is open (the swipe would type into the caption)")
    a, b = ("1300", "500") if down else ("500", "1300")
    p.adb("shell", "input", "swipe", "360", a, "360", b, "350")
    time.sleep(1.5)


def upload(video: Path, caption: str, audience: str, delete_after: bool, preview: bool = False) -> None:
    p = Phone(SERIAL)
    today = f"{date.today():%B} {date.today().day}, {date.today().year}"  # e.g. "November 2, 2026"

    print("1. copy video to the gallery", flush=True)
    remote = p.push_video(video)
    try:
        _upload(p, remote, caption, audience, delete_after, today, preview)
    except Abort:
        try:
            if p.find("Share") or p.find("Edit video") or p.find("Start over") or p.find("Save draft"):
                discard_session(p)
        except Abort:
            pass
        raise
    finally:
        p.remove_video(remote)
        print("  removed the copied video from the phone", flush=True)


def _upload(p: Phone, remote: str, caption: str, audience: str, delete_after: bool, today: str, preview: bool = False) -> None:

    print("2. open Instagram -> Create", flush=True)
    p.wake()
    p.d.app_start(PKG, stop=True)
    p.wait("home screen", "Home", "Reels", "Profile", timeout=30)
    dismiss_popups(p)
    p.tap_node(p.by_id("action_bar_left_button"), "Create (+)")
    time.sleep(2)
    if p.find("Keep editing your draft?"):
        discard_unfinished(p)
        if not p.find("New reel"):  # "Start over" usually lands back in the gallery already
            p.d.app_start(PKG, stop=True)
            p.wait("home screen", "Home", timeout=30)
            p.tap_node(p.by_id("action_bar_left_button"), "Create (+) again")

    # Since 2026-11-03 "+" can open on the POST tab ("New post"), and the new INSTANTS tab pushes REEL off the
    # right edge of the tab strip: select the REEL tab by its id (uiautomator taps its visible part).
    end = time.time() + 10
    while time.time() < end and not p.find("New reel"):
        if p.find("New post") or p.by_id("cam_dest_feed"):
            reel_tab = p.d(resourceId=f"{PKG}:id/cam_dest_clips")
            if reel_tab.exists:
                print("  '+' opened on New post: switching to the REEL tab", flush=True)
                reel_tab.click()
                time.sleep(3)
                continue
        time.sleep(1)

    print("3. New reel gallery -> newest video", flush=True)
    p.wait("New reel gallery", "New reel", "REEL")
    tiles: list[dict] = []
    end = time.time() + 20  # thumbnails load a few seconds after the gallery opens
    while time.time() < end and not tiles:
        tiles = sorted((n for n in p.nodes() if n["label"].startswith("Unselected Video thumbnail")),
                       key=lambda n: (n["box"][1], n["box"][0]))
        if not tiles:
            time.sleep(1.5)
    if not tiles:
        p.fail("no video tiles in the gallery")
    if today not in tiles[0]["label"]:
        p.fail(f"newest video tile is not from today ({tiles[0]['label'][:80]!r})")
    time.sleep(2)  # tiles are listed a moment before they accept taps
    for attempt in range(2):
        p.tap_node(tiles[0], "newest video")
        end = time.time() + 12
        while time.time() < end and not p.find("Edit video"):
            time.sleep(1)
        if p.find("Edit video"):
            break

    print("4. editor -> Next", flush=True)
    p.wait("reel editor", "Next", "Edit video", timeout=20)
    p.tap_node(p.by_id("clips_right_action_button"), "Next")

    print("5. share screen", flush=True)
    ns = p.wait("share screen", "New reel", "Save draft", timeout=30)  # its share button may say "Share" or "Next"
    p.tap_node(p.by_id("caption_input_text_view", ns), "caption field")
    p.d(resourceId=f"{PKG}:id/caption_input_text_view").clear_text()
    time.sleep(0.5)
    p.type_text(caption)
    # commit the caption: a "Done" check mark (since 2026-11-03) or an "OK" button; then make sure the keyboard is
    # gone, because the scroll below is a swipe and on the keyboard it would gesture-type letters into the caption
    done = p.find("Done") or p.find("OK", clickable=True)
    if done:
        p.tap_node(done, f"{done['label']} (commit caption)")
        time.sleep(1.5)
    for _ in range(2):
        if not keyboard_open(p):
            break
        p.d.press("back")
        time.sleep(1.5)
    if keyboard_open(p):
        p.fail("the keyboard would not close after the caption")
    field = p.by_id("caption_input_text_view")
    if field is None or field.get("text") != caption:
        p.fail(f"caption field shows {field and field.get('text')!r}, expected {caption!r}")

    scroll(p)
    want = AUDIENCE[audience]
    ns = p.wait("share screen settings", "Audience", "Facebook", "Your story")
    current = audience_value(ns)
    if current != want:
        p.tap_node(p.find("Audience", ns), "Audience row")
        ns = p.wait("audience screen", "Who can see your reel", want)
        row = p.find(want, ns)
        # the radio button sits at the right end of the row
        p.tap(660, row["center"][1], f"audience '{want}' radio")
        ok = p.find("OK", clickable=True)
        if ok:
            p.tap_node(ok, "OK (audience info)")
        p.d.press("back")
        time.sleep(1.5)
        ns = p.wait("share screen settings", "Audience", "Facebook")
        current = audience_value(ns)
        if current != want:
            p.fail(f"audience did not stick (shows {current!r})")

    xml = p.d.dump_hierarchy()
    toggles_on = re.findall(r'checkable="true" checked="true"', xml)
    if toggles_on:
        p.fail(f"{len(toggles_on)} toggle(s) are on (Facebook / Your story / AI label); refusing to share")

    if preview:
        shot = Path(__file__).parent / "runs" / f"preview-instagram-{time.strftime('%m%d-%H%M%S')}{'-' + os.environ['PHONE_ID'] if os.environ.get('PHONE_ID') else ''}.png"
        shot.parent.mkdir(exist_ok=True)
        p.d.screenshot(str(shot))
        print(f"6. PREVIEW  (audience: {want}, caption: {caption!r}): share screen is filled, NOT sharing; screenshot {shot}", flush=True)
        discard_session(p)   # Start over, never Save draft
        print("RESULT " + json.dumps({"app": "instagram", "preview": True, "shot": str(shot), "audience": want, "chars": len(caption)}), flush=True)
        return

    print(f"6. SHARE  (audience: {want}, caption: {caption!r})", flush=True)
    p.tap_node(p.by_id("share_button"), "Share")
    # One-time notice (2026-11-03): "Update on your original audio ... can be used by people on the Meta AI app".
    # Its buttons both share. Which one is the account owner's choice: [instagram].meta_ai_audio = "off" | "allow".
    end = time.time() + 8
    while time.time() < end:
        if p.find("Turn off and share"):
            choice = (CFG.get("instagram") or {}).get("meta_ai_audio")
            if choice == "off":
                p.tap_node(p.find("Turn off and share"), "Turn off and share (Meta AI audio off)")
            elif choice == "allow":
                p.tap_node(p.by_id("clips_original_audio_nux_sheet_share_button"), "Share (Meta AI audio allowed)")
            else:
                p.tap_node(p.find("Dismiss"), "dismiss the Meta AI audio notice")
                p.fail("Instagram asks whether Meta AI may reuse our original audio; set [instagram] meta_ai_audio "
                       "= \"off\" or \"allow\" in config.toml (nothing was shared)")
            break
        time.sleep(1)

    print("7. confirm it on the profile's Reels tab", flush=True)
    time.sleep(20)  # let the upload finish; never restart the app here, it could cut the upload off
    open_profile_reels(p)
    tile = None
    end = time.time() + 180
    while time.time() < end:
        tile = p.find(re.compile(r"^Reel by .+\. View Count 0\."))
        if tile is not None:
            break
        time.sleep(10)
        p.wake()
        if p.find("Most viewed"):  # sort menu got opened; close it
            p.d.press("back")
            time.sleep(1)
        p.adb("shell", "input", "swipe", "360", "400", "360", "1200", "300")  # pull to refresh
        time.sleep(4)
    if tile is None:
        p.fail("no new (0-view) reel on the profile after 3 minutes")
    p.tap_node(tile, "newest reel")
    ns = p.wait("reel viewer", "More", timeout=20)
    if not p.find(re.compile(re.escape(caption[:25])), ns):
        p.fail("the newest reel's caption does not match")
    print("  OK: reel found with the right caption", flush=True)

    if delete_after:
        print("8. delete the test reel", flush=True)
        p.tap_node(p.find("More", ns), "reel menu")
        ns = p.wait("reel menu", "Delete")
        p.tap_node(next(n for n in ns if n["label"] == "Delete"), "Delete")
        p.wait("delete confirm", "Delete reel?")
        p.tap_node(p.by_id("igds_alert_dialog_primary_button"), "Delete (confirm)")
        time.sleep(3)
        print("  deleted", flush=True)

    print("done", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--caption", required=True)
    ap.add_argument("--audience", choices=AUDIENCE, default="everyone")
    ap.add_argument("--account", help="switch to this already-logged-in account first")
    ap.add_argument("--delete-after", action="store_true", help="for tests: delete the reel once verified")
    ap.add_argument("--preview", action="store_true", help="rehearse: fill the share screen, screenshot, Start over; never tap Share")
    a = ap.parse_args()
    try:
        if a.account:
            import accounts
            p = Phone(SERIAL)
            accounts.switch(p, "instagram", a.account)
        upload(a.video, a.caption, a.audience, a.delete_after, a.preview)
    except Abort as e:
        print(f"ABORTED: {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
