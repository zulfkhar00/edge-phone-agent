"""X (com.twitter.android) and Threads (com.instagram.barcelona): post a text post (+ optional image or video), read a
post's stats, check the login. Same pattern as the Instagram / TikTok flows (see docs/how-flows-work.md):
find nodes by label or resource id (never by coordinate), wait for the expected screen before every tap, and abort
with a screenshot on anything unexpected.

Hard rules (kept in code, tested in tests/test_social.py):
  * never log in: a logged-out app, or a different account than the configured handle, raises NeedsLogin
    ("ABORTED: needs login: ...") and nothing else happens. No credentials are ever typed.
  * nothing paid is ever tapped (Premium / Verified / Subscribe / Boost / promote sheets are closed with Back).
  * drafts are never saved: an unfinished compose screen is discarded.
  * "tap Post at" is printed exactly when Post is tapped, so the engine knows a failed run may have gone out (it then
    marks the job `unverified` and never re-posts it).

STATUS: the logged-OUT screens of both apps were mapped read-only on a real phone (2026-11-25). Every step after the login
screen (drawer, compose, media picker, profile list, count parsing) is written from the apps' current public UI and is
marked `LIVE-VERIFY`: it needs one supervised run on a logged-in practice account before it is trusted. See flows/x_threads.md.

Usage:
  python x_post.py "text" [--media FILE] --account handle         # X
  python threads_post.py "text" [--media FILE] --account handle   # Threads
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from phone import SHOTS, Abort, Phone

MEDIA_DIR = "/storage/emulated/0/DCIM/flagd"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}


class NeedsLogin(Abort):
    """The app is logged out, or logged in as someone else. The owner logs in by hand (Take over); we never do."""


@dataclass(frozen=True)
class Spec:
    key: str
    name: str
    pkg: str
    limit: int  # max characters of one post
    # --- logged-in home screen: this many of these labels (content-desc / text) must be on screen, or one of the ids
    home_labels: tuple[str, ...]
    home_ids: tuple[str, ...]
    home_min: int
    # --- logged-out screen (mapped on a real phone for both apps)
    login_labels: tuple[str, ...]  # regexes
    login_ids: tuple[str, ...]
    # --- harmless nag dialogs we dismiss (exact labels); promo sheets are closed with Back, never tapped
    popups: tuple[str, ...]
    promo: tuple[str, ...]  # regexes: paid upsells
    # --- compose
    compose_ids: tuple[str, ...]
    compose_labels: tuple[str, ...]  # regexes
    field_ids: tuple[str, ...]
    post_ids: tuple[str, ...]
    post_labels: tuple[str, ...]  # exact
    media_labels: tuple[str, ...]  # regexes: the "add photo or video" button
    media_ids: tuple[str, ...]
    tile_labels: tuple[str, ...]  # regexes: gallery tiles
    picker_done: tuple[str, ...]  # exact labels that confirm the picker
    attached_labels: tuple[str, ...]  # regexes: proof a media preview is attached
    error_labels: tuple[str, ...]  # regexes: the app refused the post
    # --- navigation
    profile_labels: tuple[str, ...]  # regexes
    profile_ids: tuple[str, ...]
    compose_open_labels: tuple[str, ...] = ()  # regexes: the compose screen's title, for apps whose empty field is not an EditText node
    placeholder_labels: tuple[str, ...] = ()  # regexes: the empty field's hint node (tapped to focus before typing)
    drawer_labels: tuple[str, ...] = ()  # X: the side drawer that shows "@handle" and "Profile"
    drawer_ids: tuple[str, ...] = ()
    switch_labels: tuple[str, ...] = ()  # regexes: the account-switcher control
    never: tuple[str, ...] = ("Add an existing account", "Create a new account", "Add account", "Create account", "Log out", "Log in")


X = Spec(
    key="x", name="X", pkg="com.twitter.android", limit=280,
    home_labels=("Home", "Search", "Notifications", "Messages"), home_ids=("composer_write", "bottomNavigationBar"), home_min=3,
    login_labels=(r"^Log in$", r"^Sign in$", r"^Create account$", r"^Sign up", r"Have an account already\?", r"^Continue with Google",
                  r"^Sign in with Google", r"^Continue with Apple"),
    login_ids=("google_sign_in", "sign_in_button", "cta_button_login"),
    popups=("Not now", "Maybe later", "Skip for now", "Skip", "No thanks", "Not Now", "Don’t allow", "Don't allow", "Dismiss", "Got it", "Later"),
    promo=(r"Premium", r"Subscribe", r"Get verified", r"Verified Organizations", r"Upgrade to"),
    compose_ids=("composer_write", "composer_post"), compose_labels=(r"^New post$", r"^Compose", r"^New Tweet$"),
    field_ids=("tweet_text", "composer_text", "post_text"),
    post_ids=("button_tweet", "composer_post_button"), post_labels=("Post",),
    media_labels=(r"^Add photos? or videos?$", r"^Add (a )?(photo|media|image|video)", r"^Photos?$", r"^Gallery$"),
    media_ids=("gallery", "gallery_btn", "media_button"),
    tile_labels=(r"^(Photo|Image|Video|GIF)\b", r"^Select (photo|image|video)", r"(?i)\b(photo|video) (taken|created)"),
    picker_done=("Add", "Done", "Next"),
    attached_labels=(r"(?i)^(remove|delete) (media|image|photo|video|attachment)", r"(?i)^(edit|add description|add alt text)",
                     r"(?i)(attached|attachment)"),
    error_labels=(r"(?i)something went wrong", r"(?i)already said that", r"(?i)not sent", r"(?i)failed to send", r"(?i)try again",
                  r"(?i)over the character limit"),
    profile_labels=(r"^Profile$",), profile_ids=("profile",),
    drawer_labels=(r"^Show navigation drawer$", r"^Show navigation menu$", r"^Account menu$"), drawer_ids=("toolbar_avatar", "avatar", "drawer_avatar"),
    switch_labels=(r"(?i)switch account", r"(?i)account.*(list|menu|switcher)", r"(?i)show (more )?accounts", r"(?i)^more accounts"),
)

THREADS = Spec(
    key="threads", name="Threads", pkg="com.instagram.barcelona", limit=500,
    home_labels=("Home", "Search", "Activity", "Profile", "Feed", "Messages", "Create"), home_ids=("tab_bar", "bottom_tab_bar"), home_min=3,
    login_labels=(r"^Log in with Instagram$", r"^Log in$", r"^Create Instagram account$", r"^Switch accounts$", r"^Continue with Instagram"),
    login_ids=("login_screen_login_with_ig_button", "login_screen_create_ig_account_button"),  # seen on the real logged-out phone
    popups=("Not now", "Not Now", "Skip", "Maybe later", "No thanks", "Dismiss", "Got it", "Later", "Don’t allow", "Don't allow", "Deny"),
    promo=(r"Meta Verified", r"Subscribe", r"Get verified", r"Verified badge", r"Upgrade"),
    # Verified on phone-2 (Threads, 2026-11-28): bottom tabs are plain Views labelled Feed / Messages / Create / Activity / Profile (NOT
    # clickable in the dump, no resource id); the home feed starts with a "What's new?" row. The compose screen's field is an
    # EditText only once something is typed (id new_thread_screen_composer); empty, it is a "What's new?" TextView under a
    # "New thread" title. Post is a plain TextView "Post" (not clickable). Closing with text opens "Save to drafts?" -> "Don't save".
    compose_ids=("create_tab", "composer_entry"), compose_labels=(r"^Create$", r"^New thread$", r"^Start a thread", r"^What.s new\?$"),
    compose_open_labels=(r"^New thread$",), placeholder_labels=(r"^What.s new\?$",),
    field_ids=("new_thread_screen_composer", "text_input", "composer_text_input", "thread_text"),
    post_ids=("post_button", "composer_post_button"), post_labels=("Post",),
    media_labels=(r"(?i)^(attach|add) (a )?(media|photo|image|video)", r"(?i)^add (a )?photo or video", r"(?i)^gallery$", r"(?i)^photos?$"),
    media_ids=("media_button", "gallery_button", "composer_gallery"),
    tile_labels=(r"^(Photo|Image|Video)\b", r"(?i)\b(photo|video) (taken|created)", r"^Select"),
    picker_done=("Done", "Add", "Next"),
    attached_labels=(r"(?i)^(remove|delete) (media|image|photo|video|attachment)", r"(?i)^(edit|add alt text)", r"(?i)(attached|attachment)"),
    error_labels=(r"(?i)something went wrong", r"(?i)couldn’t post", r"(?i)couldn't post", r"(?i)failed to post", r"(?i)try again",
                  r"(?i)over the character limit"),
    profile_labels=(r"^Profile$",), profile_ids=("profile_tab",),
    switch_labels=(r"(?i)switch account", r"(?i)^accounts?$"),
)
SPECS = {"x": X, "threads": THREADS}


# ---------------------------------------------------------------- small helpers
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def labels_of(ns: list[dict]) -> list[str]:
    return [t for n in ns for t in (n["label"], n.get("text", ""), n.get("content-desc", "")) if t]


def find_id(ns: list[dict], ids: tuple[str, ...]) -> dict | None:
    for want in ids:
        for n in ns:
            rid = n.get("resource-id", "")
            if rid == want or rid.endswith(":id/" + want):  # Threads' Compose UI reports bare ids ("new_thread_screen_composer")
                return n
    return None


def find_label(ns: list[dict], patterns: tuple[str, ...], clickable: bool | None = None) -> dict | None:
    for pat in patterns:
        rx = re.compile(pat)
        for n in ns:
            if any(rx.search(t) for t in (n["label"], n.get("text", ""), n.get("content-desc", "")) if t):
                if clickable is None or (n.get("clickable") == "true") == clickable:
                    return n
    return None


def find_any(ns: list[dict], ids: tuple[str, ...] = (), labels: tuple[str, ...] = (), clickable: bool | None = None) -> dict | None:
    return find_id(ns, ids) or find_label(ns, labels, clickable)


def exact(ns: list[dict], names: tuple[str, ...], clickable: bool | None = True) -> dict | None:
    for name in names:
        for n in ns:
            if n["label"] == name and (clickable is None or (n.get("clickable") == "true") == clickable):
                return n
    return None


def is_home(spec: Spec, ns: list[dict]) -> bool:
    have = set(labels_of(ns))
    return find_id(ns, spec.home_ids) is not None or sum(1 for l in spec.home_labels if l in have) >= spec.home_min


def is_logged_out(spec: Spec, ns: list[dict]) -> bool:
    return find_id(ns, spec.login_ids) is not None or find_label(ns, spec.login_labels) is not None


def edit_field(spec: Spec, ns: list[dict]) -> dict | None:
    return find_id(ns, spec.field_ids) or next((n for n in ns if n.get("class") == "android.widget.EditText"), None)


def compose_is_open(spec: Spec, ns: list[dict]) -> bool:
    return edit_field(spec, ns) is not None or find_label(ns, spec.compose_open_labels) is not None


def post_button(spec: Spec, ns: list[dict]) -> dict | None:
    """The Post button. Threads' is a plain TextView (not clickable in the dump), X's a real button."""
    return find_id(ns, spec.post_ids) or exact(ns, spec.post_labels) or exact(ns, spec.post_labels, clickable=None)


def foreground_pkg(p: Phone) -> str:
    try:
        return p.d.app_current().get("package", "") or ""
    except Exception:
        return ""


def needs_login(p: Phone, spec: Spec, handle: str, why: str) -> None:
    """Screenshot + NeedsLogin. The message starts with 'needs login:' (the engine alerts on that, and does not retry)."""
    try:
        p.fail(f"needs login: {spec.name} @{handle}: {why}. Log in by hand with Take over; the engine never types credentials")
    except Abort as e:
        raise NeedsLogin(str(e)) from None


def check_text(spec: Spec, text: str) -> None:
    if not text or not text.strip():
        raise Abort("empty text")
    if len(text) > spec.limit:
        raise Abort(f"text is {len(text)} characters; {spec.name} allows {spec.limit}")
    if "\n" in text or not text.isascii():
        raise Abort("text must be one line of plain ASCII (it is typed with `adb input text`)")


def media_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    raise Abort(f"unsupported media type {path.suffix!r}")


# ---------------------------------------------------------------- screens
def dismiss_popups(p: Phone, spec: Spec) -> None:
    """Harmless nag dialogs go away with their dismiss button; paid upsells are closed with Back and never tapped."""
    for _ in range(4):
        ns = p.nodes()
        promo = find_label(ns, spec.promo)
        if promo is not None and not is_home(spec, ns):  # a sheet over the app; a promo card inside the feed is left alone
            print("  closing a promo sheet with Back (never its button)", flush=True)
            p.d.press("back")
            time.sleep(1.5)
            continue
        hit = exact(ns, spec.popups)
        if hit is None:
            return
        p.tap_node(hit, f"dismiss '{hit['label']}'")


def open_app(p: Phone, spec: Spec, handle: str) -> list[dict]:
    """Start the app fresh. Returns the nodes of the logged-in home screen, or raises NeedsLogin.
    Permission / notification sheets are dismissed first and never count as a log-in screen; another app in the foreground
    (a system settings page, say) brings the app back instead of being read as logged out."""
    p.wake()
    p.d.app_start(spec.pkg, stop=True)
    end = time.time() + 40
    ns: list[dict] = []
    out_strikes = 0
    while time.time() < end:
        p.wake()
        fg = foreground_pkg(p)
        if fg and fg != spec.pkg and "permissioncontroller" not in fg and "packageinstaller" not in fg:
            print(f"  {fg} is in the foreground, bringing {spec.name} back", flush=True)
            p.d.app_start(spec.pkg)
            time.sleep(3)
            continue
        dismiss_popups(p, spec)
        ns = p.nodes()
        if is_home(spec, ns):
            return ns
        if is_logged_out(spec, ns):
            out_strikes += 1  # twice in a row, so a half-drawn sheet or a transition frame cannot cause it
            if out_strikes >= 2:
                needs_login(p, spec, handle, "the app shows its log-in screen")
        else:
            out_strikes = 0
        time.sleep(2)
    p.fail(f"{spec.name} home screen not recognised after 40 s (labels seen: {labels_of(ns)[:12]})")
    return ns


def open_own_profile(p: Phone, spec: Spec) -> list[dict]:
    """X: side drawer -> Profile. Threads: Profile tab. Returns the profile's nodes."""
    ns = p.nodes()
    if spec.key == "threads":
        tab = find_id(ns, spec.profile_ids) or exact(ns, ("Profile",), None)
        p.tap_node(tab, "Profile tab")
        time.sleep(3)
        return p.nodes()
    open_drawer(p, spec)
    ns = p.nodes()
    item = exact(ns, ("Profile",)) or find_label(ns, spec.profile_labels, clickable=True)
    p.tap_node(item, "Profile (drawer)")
    time.sleep(3)
    return p.nodes()


def open_drawer(p: Phone, spec: Spec) -> list[dict]:  # LIVE-VERIFY (X)
    ns = p.nodes()
    btn = find_any(ns, spec.drawer_ids, spec.drawer_labels)
    if btn is None:
        p.fail(f"{spec.name}: the side-drawer button was not found")
    p.tap_node(btn, "avatar (side drawer)")
    time.sleep(1.5)
    return p.nodes()


def handles_on_screen(ns: list[dict]) -> list[str]:
    return [t for t in labels_of(ns) if re.fullmatch(r"@[A-Za-z0-9_.]{1,30}", t)]


def logged_in_as(p: Phone, spec: Spec, handle: str) -> bool:  # LIVE-VERIFY
    """Is the expected handle the one shown for the logged-in account? X: in the side drawer (@handle under the name).
    Threads: in the header of our own profile. Never switches or logs in here."""
    want = handle.lstrip("@").casefold()
    if spec.key == "x":
        ns = open_drawer(p, spec)
    else:
        ns = open_own_profile(p, spec)
    # X shows "@handle" (a bare word could be the display name); Threads shows the bare username in the profile header
    pattern = r"@[A-Za-z0-9_.]{1,30}" if spec.key == "x" else r"@?[A-Za-z0-9_.]{1,30}"
    shown = {t.lstrip("@").casefold() for t in labels_of(ns) if re.fullmatch(pattern, t)}
    mismatch = handles_on_screen(ns)
    if spec.key == "x":
        p.d.press("back")  # close the drawer
        time.sleep(1)
    if want in shown:
        return True
    if mismatch:
        print(f"  {spec.name} is logged in as {mismatch[0]}, expected @{handle}", flush=True)
    return False


def switch_account(p: Phone, spec: Spec, handle: str) -> bool:  # LIVE-VERIFY
    """Only between accounts that are already logged in on the phone. Never taps Add / Create account. False = not there."""
    if spec.key == "x":
        ns = open_drawer(p, spec)
        ctl = find_label(ns, spec.switch_labels, clickable=True)
        if ctl is None:
            p.d.press("back")
            return False
        p.tap_node(ctl, "account switcher")
    else:
        ns = p.nodes()
        tab = find_id(ns, spec.profile_ids) or exact(ns, ("Profile",), None)
        if tab is None:
            return False
        x, y = tab["center"]
        p.adb("shell", "input", "swipe", str(x), str(y), str(x), str(y), "900")  # long-press the Profile tab
    time.sleep(2)
    ns = p.nodes()
    rows = [n for n in ns if n["label"] and n["label"] not in spec.never and handle.casefold() in n["label"].casefold()]
    if not rows:
        p.d.press("back")
        return False
    p.tap_node(sorted(rows, key=lambda n: len(n["label"]))[0], f"@{handle} (switch)")
    time.sleep(8)
    return True


def ensure_account(p: Phone, spec: Spec, handle: str) -> None:
    """Open the app and make sure @handle is the logged-in account; NeedsLogin otherwise. Used by every job and the preflight."""
    open_app(p, spec, handle)
    if logged_in_as(p, spec, handle):
        print(f"  {spec.name} already on @{handle}", flush=True)
        return
    if switch_account(p, spec, handle):
        open_app(p, spec, handle)
        if logged_in_as(p, spec, handle):
            print(f"  {spec.name} now on @{handle}", flush=True)
            return
    needs_login(p, spec, handle, f"@{handle} is not the logged-in account and is not in the account list")


# ---------------------------------------------------------------- posting
def open_compose(p: Phone, spec: Spec) -> None:
    ns = p.nodes()
    # bottom-bar entries can be plain Views (Threads): try real buttons first, then anything with the label
    btn = find_any(ns, spec.compose_ids, spec.compose_labels, clickable=True) or find_any(ns, spec.compose_ids, spec.compose_labels)
    p.tap_node(btn, "compose (new post)")
    end = time.time() + 15
    while time.time() < end:
        ns = p.nodes()
        if compose_is_open(spec, ns):
            return
        # X may open a small menu first (Post / Go Live / Spaces): take its plain "Post" entry
        entry = find_label(ns, (r"^Post$", r"^New post$"), clickable=True)
        if entry is not None and find_id(ns, spec.compose_ids) is None:
            p.tap_node(entry, "Post (compose menu entry; this does not publish)")
        time.sleep(1.5)
    p.fail("the compose screen with a text field did not open")


def type_text(p: Phone, spec: Spec, text: str) -> None:
    ns = p.nodes()
    f = edit_field(spec, ns) or find_label(ns, spec.placeholder_labels)  # an empty Threads field is only its hint text
    p.tap_node(f, "text field")
    try:
        p.d.clear_text()
    except Exception:
        pass
    p.type_text(text)
    time.sleep(1)
    shown = norm((edit_field(spec, p.nodes()) or {}).get("text", ""))
    if shown != norm(text):
        p.fail(f"the text field shows {shown[:60]!r}, expected {norm(text)[:60]!r}")


def attach_media(p: Phone, spec: Spec, path: Path) -> None:  # LIVE-VERIFY
    """Pick the newest gallery item (the file was just pushed, so it is first) and confirm the preview is attached."""
    ns = p.nodes()
    btn = find_any(ns, spec.media_ids, spec.media_labels, clickable=True)
    p.tap_node(btn, "add photo or video")
    time.sleep(2)
    ns = p.nodes()
    if find_label(ns, (r"(?i)allow .* to access", r"(?i)photos and videos"), clickable=None) and exact(ns, ("Allow", "Allow all", "While using the app"), None):
        p.fail("the app asks for photo access: grant it once by hand (Take over); the engine does not answer permission prompts")
    tiles: list[dict] = []
    end = time.time() + 20
    while time.time() < end and not tiles:
        ns = p.nodes()
        tiles = sorted((n for n in ns if find_label([n], spec.tile_labels) and n["label"] not in ("Camera", "Take photo")),
                       key=lambda n: (n["box"][1], n["box"][0]))
        if not tiles:
            time.sleep(1.5)
    if not tiles:
        p.fail("no media tiles in the picker")
    p.tap_node(tiles[0], "newest media tile")
    time.sleep(2)
    done = exact(p.nodes(), spec.picker_done)
    if done is not None:
        p.tap_node(done, f"{done['label']} (confirm media)")
        time.sleep(3)
    end = time.time() + 20
    while time.time() < end:
        if find_label(p.nodes(), spec.attached_labels) is not None:
            return
        time.sleep(1.5)
    p.fail("the picked media did not show up attached to the post")


def tap_post(p: Phone, spec: Spec) -> None:
    ns = p.nodes()
    btn = post_button(spec, ns)
    if btn is None:
        p.fail("the Post button was not found")
    if btn.get("enabled") == "false":
        p.fail("the Post button is disabled (empty or over-long text?)")
    p.tap_node(btn, "Post")  # prints 'tap Post at': from here a failure is `unverified`, never re-posted


def wait_sent(p: Phone, spec: Spec) -> None:
    end = time.time() + 40
    while time.time() < end:
        ns = p.nodes()
        bad = find_label(ns, spec.error_labels)
        if bad is not None:
            p.fail(f"{spec.name} refused the post: {bad['label'][:80]!r}")
        if not compose_is_open(spec, ns):
            return
        time.sleep(1.5)
    p.fail("the compose screen is still open 40 s after Post")


def discard_compose(p: Phone, spec: Spec) -> None:
    """Back out of an unfinished compose screen and Delete / Discard it. Never 'Save draft'."""
    for _ in range(4):
        ns = p.nodes()
        d = exact(ns, ("Delete", "Discard", "Discard post", "Discard thread", "Don’t save", "Don't save"))
        if d is not None:
            p.tap_node(d, f"{d['label']} (discard the unfinished post)")
            return
        if not compose_is_open(spec, ns):
            return
        p.d.press("back")
        time.sleep(1.5)


def find_post_row(p: Phone, spec: Spec, text: str) -> tuple[dict, list[dict]] | None:
    """On our profile: the node of the post whose text starts like ours (+ all nodes of the screen)."""
    rx = re.compile(re.escape(norm(text)[:25]))
    ns = p.nodes()
    for n in ns:
        if any(rx.search(norm(t)) for t in (n["label"], n.get("text", ""), n.get("content-desc", "")) if t):
            return n, ns
    return None


def verify_on_profile(p: Phone, spec: Spec, text: str, wait_s: int = 150) -> dict:
    """Open our profile and wait until the post is listed (pull to refresh while waiting). Returns the post's counts."""
    open_own_profile(p, spec)
    end = time.time() + wait_s
    while True:
        hit = find_post_row(p, spec, text)
        if hit is not None:
            row, ns = hit
            return {"found": True, "counts": row_counts(row, ns)}
        if time.time() >= end:
            p.fail(f"the new post is not on the {spec.name} profile after {wait_s} s")
        time.sleep(8)
        p.wake()
        p.adb("shell", "input", "swipe", "360", "500", "360", "1250", "300")  # pull to refresh
        time.sleep(3)


def preview_shot(p: Phone, what: str) -> str:
    """Screenshot of a rehearsal (runs/preview-<what>-<time>-<phone id>.png); returns its path."""
    SHOTS.mkdir(exist_ok=True)
    tag = os.environ.get("PHONE_ID", "")
    shot = SHOTS / f"preview-{what}-{time.strftime('%m%d-%H%M%S')}{'-' + tag if tag else ''}.png"
    p.d.screenshot(str(shot))
    return str(shot)


def post(p: Phone, spec: Spec, handle: str, text: str, media: Path | None = None, verify_wait_s: int = 150, preview: bool = False) -> dict:
    """The whole post flow on an already connected phone. Returns the RESULT dict.
    preview=True: everything up to just before Post (account checked, text typed and read back, media attached), screenshot,
    then discard the compose screen. Post is never tapped."""
    check_text(spec, text)
    remote = None
    posted = False
    try:
        print("1. open the app and check the account", flush=True)
        ensure_account(p, spec, handle)
        print("2. compose", flush=True)
        open_compose(p, spec)
        type_text(p, spec, text)
        if media is not None:
            print(f"3. attach {media_kind(media)}", flush=True)
            remote = p.push_video(media)
            attach_media(p, spec, media)
        if preview:
            shot = preview_shot(p, spec.key)
            print(f"4. PREVIEW  (@{handle}, {len(text)} chars{', with media' if media else ''}): compose screen is filled, NOT posting; "
                  f"screenshot {shot}", flush=True)
            btn = post_button(spec, p.nodes())
            if btn is None:
                p.fail("the Post button was not found on the filled compose screen")
            discard_compose(p, spec)
            return {"app": spec.key, "account": handle, "preview": True, "shot": shot, "chars": len(text),
                    "media": media_kind(media) if media else None, "post_button_enabled": btn.get("enabled") != "false"}
        print(f"4. POST  (@{handle}, {len(text)} chars{', with media' if media else ''})", flush=True)
        tap_post(p, spec)
        posted = True
        wait_sent(p, spec)
        print("5. confirm it on the profile", flush=True)
        found = verify_on_profile(p, spec, text, verify_wait_s)
    except Abort:
        if not posted:
            try:
                discard_compose(p, spec)
            except Abort:
                pass
        raise
    finally:
        if remote:
            p.remove_video(remote)
            print("  removed the copied media from the phone", flush=True)
    # The permalink is not retrievable yet: it sits behind Share -> Copy link and Android keeps the clipboard from adb.
    return {"app": spec.key, "account": handle, "verified": True, "permalink": None, "chars": len(text),
            "media": media_kind(media) if media else None, "counts": found["counts"]}


# ---------------------------------------------------------------- stats
COUNT_WORDS = {"view": "views", "like": "likes", "repl": "replies", "comment": "replies", "repost": "reposts", "retweet": "reposts",
               "quote": "quotes", "bookmark": "bookmarks", "share": "shares"}
_NUM = r"(\d[\d.,]*\s?[KMB]?)"
_WORD = r"(views?|likes?|repl(?:y|ies)|comments?|reposts?|retweets?|quotes?|bookmarks?|shares?)"
_AFTER = re.compile(rf"{_NUM}\s+{_WORD}\b", re.I)  # "345 views", "2 reposts"
_BEFORE = re.compile(rf"^{_WORD}\W+{_NUM}$", re.I)  # "Like 12", "Reply, 3"


def to_int(text: str | None) -> int | None:
    m = re.search(r"(\d[\d.,]*)\s*([KMB]?)", (text or "").strip(), re.I)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    return int(n * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()])


def parse_counts(labels: list[str]) -> dict:
    """Engagement numbers out of the accessibility labels of one post row. X reads like
    'Name @h, 2h, text. 1 reply. 2 reposts. 10 likes. 345 views'; Threads has buttons like 'Like 12' / 'Reply 3'.
    Missing numbers stay absent (never invented, never 0 by guess)."""
    out: dict = {}
    for lab in labels:
        for num, word in _AFTER.findall(lab):
            key = next(v for k, v in COUNT_WORDS.items() if word.lower().startswith(k))
            out.setdefault(key, to_int(num))
        m = _BEFORE.match(lab.strip())
        if m:
            key = next(v for k, v in COUNT_WORDS.items() if m.group(1).lower().startswith(k))
            out.setdefault(key, to_int(m.group(2)))
    return {k: v for k, v in out.items() if v is not None}


def row_counts(row: dict, ns: list[dict]) -> dict:
    """Parse the row's own label and every node whose centre lies inside the row's box (Threads keeps the numbers in
    child buttons; X in the row's description)."""
    x1, y1, x2, y2 = row["box"]
    inside = [n for n in ns if x1 <= n["center"][0] <= x2 and y1 <= n["center"][1] <= y2]
    return parse_counts(labels_of([row, *inside]))


def read_stats(p: Phone, spec: Spec, text: str) -> dict:
    """Find the post by the first 25 characters of its text on our profile (newest first, scrolling a little) and read its counts."""
    open_own_profile(p, spec)
    for _ in range(4):
        hit = find_post_row(p, spec, text)
        if hit is not None:
            row, ns = hit
            counts = row_counts(row, ns)
            return {"views": counts.get("views"), "likes": counts.get("likes"), "replies": counts.get("replies"),
                    "reposts": counts.get("reposts"), "quotes": counts.get("quotes"), "bookmarks": counts.get("bookmarks"),
                    "raw": {k: v for k, v in counts.items()}}
        p.adb("shell", "input", "swipe", "360", "1300", "360", "500", "350")
        time.sleep(2)
    p.fail(f"no post starting {norm(text)[:25]!r} among the newest on the {spec.name} profile")
    raise AssertionError  # unreachable


# ---------------------------------------------------------------- command line (x_post.py / threads_post.py)
def cli(spec: Spec) -> None:
    import argparse
    import json
    import sys

    from phone import SERIAL

    ap = argparse.ArgumentParser(description=f"Post one text post (+ optional image/video) on {spec.name}")
    ap.add_argument("text")
    ap.add_argument("--media", type=Path, help="an image or video to attach")
    ap.add_argument("--account", required=True, help="the handle that must be logged in (checked; never logged into)")
    ap.add_argument("--preview", action="store_true", help="rehearse: fill the compose screen, screenshot, cancel; never tap Post")
    a = ap.parse_args()
    try:
        if a.media is not None and not a.media.is_file():
            raise Abort(f"media file {a.media} does not exist")
        result = post(Phone(SERIAL), spec, a.account.lstrip("@"), a.text, a.media, preview=a.preview)
    except Abort as e:
        print(f"ABORTED: {e}", flush=True)
        sys.exit(1)
    print("RESULT " + json.dumps(result), flush=True)
