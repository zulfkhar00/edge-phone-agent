"""Switch the active TikTok / Instagram account on the phone (only between accounts already logged in).

TikTok:    Profile tab -> tap the name at the top -> "Switch account" sheet -> tap the handle.
Instagram: Profile tab -> tap the username at the top -> account sheet -> tap the handle.
X / Threads: social.ensure_account: verifies the logged-in handle, switches only to an already logged-in account,
           otherwise exits with "ABORTED: needs login: ..." (never logs in).
Each switch is verified on the profile afterwards. "Add account" / "Add Instagram account" are never tapped.

Usage:
  python accounts.py tiktok try.flagd
  python accounts.py instagram try.flagd
  python accounts.py tiktok --current
"""

from __future__ import annotations

import argparse
import re
import sys
import time

from phone import SERIAL, Abort, Phone

TT = "com.zhiliaoapp.musically"
IG = "com.instagram.android"


# Pop-ups that appear after a switch. Dismiss with Back; never accept (e.g. "Allow" would turn on precise location).
TT_POPUPS = ("Allow precise location", "Turn on notifications", "Sync your contacts", "Find your friends")


def tt_clear_pending_edit(p: Phone) -> None:
    """A failed post leaves 'Continue editing this post?' (Save draft / Edit). Never Save draft: open it with Edit,
    leave with Back (TikTok then keeps it as a draft), and delete that draft, only if it is the newest and dated today."""
    print("  TikTok kept an unfinished post from a failed run: discarding it", flush=True)
    p.tap_node(p.find("Edit"), "Edit (to discard the unfinished post)")
    time.sleep(4)
    p.d.press("back")
    time.sleep(3)
    tt_profile(p)
    if not p.find(re.compile(r"^Drafts: \d+")):
        return
    p.tap(119, 795, "Drafts tile")
    time.sleep(3)
    today = time.strftime("%b"), str(int(time.strftime("%d")))
    ns = p.nodes()
    month = next((n for n in ns if n["label"] in ("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())), None)
    day = month and next((n for n in ns if n["label"].isdigit() and abs(n["box"][0] - month["box"][0]) < 30
                          and 0 < month["box"][1] - n["box"][1] < 50), None)
    if not (month and day and (month["label"], day["label"]) == today):
        p.d.press("back")
        p.fail("the newest TikTok draft is not from today: leaving drafts alone")
    p.tap_node(p.find("Select"), "Select")
    time.sleep(2)
    box = next((n for n in p.nodes() if n.get("checkable") == "true"), None)  # first = newest draft
    p.tap_node(box, "newest draft checkbox")
    time.sleep(1.5)
    p.tap_node(p.find("Delete"), "Delete")
    ns = p.wait("delete draft confirm", "Delete 1 draft?")
    p.tap_node(next((n for n in ns if n["label"] == "Delete" and n["box"][1] > 800), None), "Delete (confirm)")
    time.sleep(3)
    p.d.press("back")
    time.sleep(2)


def tt_dismiss(p: Phone) -> None:
    for _ in range(3):
        if p.find("Continue editing this post?"):
            tt_clear_pending_edit(p)
            continue
        if p.find("Allow precise location"):
            # Modal with only "Open settings" / "Allow"; Back doesn't close it. Open settings, change nothing, come back.
            p.tap_node(p.find("Open settings"), "Open settings (to close the location prompt)")
            time.sleep(3)
            if p.d.app_current().get("package") == "com.android.settings":
                p.d.press("back")
                time.sleep(3)
            continue
        if not any(p.find(t) for t in TT_POPUPS):
            return
        print("  dismissing a TikTok pop-up with Back", flush=True)
        p.d.press("back")
        time.sleep(1.5)


# ---------------------------------------------------------------- TikTok
def tt_profile(p: Phone) -> None:
    """Get to our own profile, dealing with pop-ups (unfinished post, location...) that can appear at any point."""
    end = time.time() + 40
    while time.time() < end:
        p.wake()
        if p.find("Continue editing this post?") or p.find("Allow precise location") or any(p.find(t) for t in TT_POPUPS):
            tt_dismiss(p)
            continue
        ns = p.nodes()
        if p.find("Profile menu", ns):  # only our own profile page has this button
            time.sleep(2)  # pop-ups tend to appear a moment after the profile opens: look once more
            if not (p.find("Continue editing this post?") or p.find("Allow precise location")):
                return
            continue
        tab = next((n for n in ns if n["label"] == "Profile" and n["box"][1] > p.h * 0.88), None)
        if tab:
            p.tap_node(tab, "Profile tab")
        elif p.d.app_current().get("package") != TT:
            p.d.app_start(TT)
        else:
            p.d.press("back")
        time.sleep(2.5)
    p.fail("expected the TikTok profile (Profile menu) but did not see it")


def tt_current(p: Phone) -> str | None:
    tt_profile(p)
    handle = p.find(re.compile(r"^@[A-Za-z_][\w.]*$"))  # real handles; TikTok also has "@2131823465"-style placeholders
    return handle["label"][1:] if handle else None


def tt_switch(p: Phone, handle: str) -> None:
    if tt_current(p) == handle:
        print(f"  TikTok already on @{handle}", flush=True)
        return
    ns = p.nodes()
    name = next((n for n in ns if n["box"][1] < 260 and n.get("clickable") == "true" and n["label"]
                 and n["label"] not in ("Profile views", "Add person", "Profile menu")), None)
    p.tap_node(name, "name at the top (account switcher)")
    ns = p.wait("Switch account sheet", "Switch account")
    row = next((n for n in ns if n["label"] == handle and n.get("clickable") == "true"), None) \
        or next((n for n in ns if n["label"] == handle), None)
    if row is None:
        p.d.press("back")
        p.fail(f"@{handle} is not logged in on TikTok (accounts: "
               f"{[n['label'] for n in ns if n['label'] not in ('Bottom sheet', 'Switch account', 'Close', 'Checkmark', 'Add account')]})")
    p.tap_node(row, f"@{handle}")
    time.sleep(6)
    p.d.app_start(TT)  # the switch reloads the app on the For You feed
    end = time.time() + 30
    while time.time() < end:
        try:
            if tt_current(p) == handle:
                print(f"  TikTok now on @{handle}", flush=True)
                return
        except Abort:
            pass
        time.sleep(2)
    p.fail(f"TikTok did not switch to @{handle}")


# ---------------------------------------------------------------- Instagram
def ig_setup_screens(p: Phone) -> None:
    """One-off screens after a new login ("Set up on new device": contacts, etc.): always Skip, never Allow."""
    for _ in range(4):
        if any(p.find(t) for t in ("Stand out with a verified badge", "Meta One", "Meta Verified")):
            print("  closing a promo sheet with Back (never its button)", flush=True)  # paid upsell: never tap it
            p.d.press("back")
            time.sleep(1.5)
        elif p.find("Set up on new device") or p.find("Allow access to contacts to find people to follow"):
            skip = p.find("Skip", clickable=None) or p.find("Not now")
            if skip is None:
                p.fail("new-device setup screen without a Skip button")
            p.tap_node(skip, "Skip (new-device setup)")
            time.sleep(3)
        else:
            return


def ig_profile(p: Phone) -> None:
    p.wake()
    ig_setup_screens(p)
    tab = None
    for i in range(15):  # the splash screen after a cold start can take a while
        tab = p.by_id("profile_tab")
        if tab:
            break
        if i == 8:  # e.g. left on a settings page: start the app fresh
            p.d.app_start(IG, stop=True)
        time.sleep(2)
        p.wake()
    p.tap_node(tab, "Profile tab")
    time.sleep(2.5)


def ig_current(p: Phone) -> str | None:
    ig_profile(p)
    title = p.by_id("action_bar_title") or next(
        (n for n in p.nodes() if n["box"][1] < 180 and re.fullmatch(r"[\w.]+", n["label"])
         and n["label"] not in ("Options", "Back", "Threads")), None)
    return title["label"] if title else None


def ig_switch(p: Phone, handle: str) -> None:
    cur = ig_current(p)
    if cur == handle:
        print(f"  Instagram already on {handle}", flush=True)
        return
    ns = p.nodes()
    # the current username at the top opens the switcher (the ☰ next to it is labelled "Options": don't match words)
    top = next((n for n in ns if n["box"][1] < 180 and n["label"] == cur and n.get("clickable") == "true"), None) \
        or next((n for n in ns if n["box"][1] < 180 and n["label"] == cur), None)
    p.tap_node(top, "username at the top (account switcher)")
    ns = None  # two sheet designs: "Add Instagram account" (classic) or "Add Instagram" (Meta accounts sheet)
    for _ in range(10):
        time.sleep(1.5)
        cand = p.nodes()
        if any(n["label"] in ("Add Instagram account", "Add Instagram") for n in cand):
            ns = cand
            break
    if ns is None:
        p.fail("the account sheet did not open")
    row = next((n for n in ns if n["label"] == handle and n.get("clickable") == "true"), None) \
        or next((n for n in ns if n["label"] == handle), None)
    if row is None:
        p.d.press("back")
        p.fail(f"{handle} is not logged in on Instagram")
    p.tap_node(row, handle)
    end = time.time() + 40
    while time.time() < end:
        time.sleep(4)
        ig_setup_screens(p)
        try:
            if ig_current(p) == handle:
                print(f"  Instagram now on {handle}", flush=True)
                return
        except Abort:
            pass
    p.fail(f"Instagram did not switch to {handle}")


def switch(p: Phone, app: str, handle: str) -> None:
    if app in ("x", "threads"):  # social.py: checks the logged-in account, switches only between logged-in ones, else NeedsLogin
        import social
        social.ensure_account(p, social.SPECS[app], handle)
        return
    p.wake()
    # always start from a clean app (it may have been left deep in search, comments, an editor...)
    p.d.app_start(TT if app == "tiktok" else IG, stop=True)
    time.sleep(7)
    if app == "tiktok":
        tt_switch(p, handle)
    else:
        ig_switch(p, handle)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("app", choices=("tiktok", "instagram", "x", "threads"))
    ap.add_argument("handle", nargs="?")
    ap.add_argument("--current", action="store_true")
    a = ap.parse_args()
    p = Phone(SERIAL)
    try:
        if a.app in ("x", "threads") and not a.handle:
            ap.error("x / threads: give the handle to check")
        if a.current or not a.handle:
            p.d.app_start(TT if a.app == "tiktok" else IG)
            time.sleep(6)
            print((tt_current if a.app == "tiktok" else ig_current)(p))
        else:
            switch(p, a.app, a.handle)
    except Abort as e:
        print(f"ABORTED: {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
