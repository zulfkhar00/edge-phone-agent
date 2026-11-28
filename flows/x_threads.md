# X and Threads flows (`social.py`, `x_post.py`, `threads_post.py`)

Packages: X `com.twitter.android`, Threads `com.instagram.barcelona`. Pattern: `docs/how-flows-work.md`.

## STATUS: not yet run logged in

| Verified | How |
|---|---|
| Logged-out screens of both apps | Read-only on phone-2 (moto g, `ZA223JNSRT`, idle, no accounts), 2026-11-25 |
| Flow logic: login gate, no credentials, no paid taps, no drafts, text limits, media push + clean-up, `tap Post at`, verification, unverified on a missing post, count parsing, plan / quota / engine / preflight / dashboard wiring | `tests/test_social.py`, `tests/test_social_engine.py` (fake screens) |
| **Everything after the login screen** (side drawer, compose, media picker, profile list, count labels) | **Not verified.** Written from the apps' public UI, label-based with fall-backs; every such function in `social.py` carries a `LIVE-VERIFY` comment |

Neither app is logged in on any phone yet. The first supervised run (after the owner logs in by hand with Take over)
must go through the checklist at the bottom, on a practice account, before `use = "noswipe"` accounts post.

## Mapped logged-out screens (real)

- **X**: cold start shows the Android notification-permission dialog first (`grant_dialog`: "Allow X to send you
  notifications?", buttons `permission_allow_button` "Allow" / `permission_deny_button` "Don't allow"). The flow does not
  answer system permission dialogs in `open_app` except the harmless "Don't allow" entry in the popup list. Behind it is the
  logged-out welcome (not reachable without answering the dialog, so not mapped).
- **Threads**: `login_screen_login_with_ig_button` ("Log in with Instagram"), `login_screen_create_ig_account_button`
  ("Create Instagram account"). Threads signs in through an Instagram account. Both ids are in `THREADS.login_ids`.

## What the flow does

`x_post.py "text" --account handle [--media file]` / `threads_post.py ...` -> `social.post`:
1. `check_text`: one line of ASCII, <= 280 (X) / 500 (Threads) characters. Nothing on the phone is touched before this passes.
2. `open_app`: force-start; a logged-out screen (login labels / ids) -> **NeedsLogin**; safe nag dialogs dismissed, promo sheets
   (Premium / Meta Verified / Subscribe) closed with Back.
3. `ensure_account`: X reads `@handle` in the side drawer; Threads looks for the username in the own-profile header. Mismatch
   -> switch only if the account is in the account list (X: drawer account switcher, Threads: long-press Profile tab), never
   "Add an existing account" / "Create a new account". Else **NeedsLogin**.
4. `open_compose` -> `type_text` (field read back and compared) -> `attach_media` (media pushed to `DCIM/flagd` + media scan,
   newest tile, confirm button, an attached-preview node must appear) -> `tap_post` (`tap Post at` printed here).
5. `wait_sent` (error banners abort), then `verify_on_profile`: own profile, post row whose text starts like ours, pull to refresh up
   to 150 s. Counts parsed from the row are returned.
6. `finally`: the pushed copy is removed. A failure before Post discards the compose screen (Delete / Discard, never Save).

**needs login**: the script exits with `ABORTED: needs login: ...` + screenshot. The engine fails the job at once (no retry),
raises one `login` alert ("log in by hand with Take over ... then retry the job") and the dashboard shows a red "Needs login" pill
on the account. A later successful job turns it green. The engine never types credentials.

**After Post**: any failure leaves the job `unverified` (never re-posted); stats jobs still run and show whether it exists.

## Plan format

```json
{"id": "t1", "at": "13:00", "text": "Up to 280 chars of plain ASCII.", "media": "videos/pic.jpg",
 "targets": [{"app": "x", "account": "handle"}, {"app": "threads", "account": "handle"}]}
```
`text` (or `caption`) is the post; `media` (or `video`) is optional: an image (jpg/png/webp, <= 5 MB) or a video (mp4/mov,
0.5-140 s, any orientation). A post with only X / Threads targets needs no media; Instagram / TikTok targets still need the
vertical video. Limits are per target (a 300-character text goes to Threads only). Same `at` / `round`, caption-key and daily
`posts_per_day` rules as the other apps. Research and deletes for X / Threads are rejected.

## Config

```toml
[[phones]]
accounts = ["x:@handle", "threads:@handle"]

[accounts.x.handle]
use = "noswipe"          # or "practice"
posts_per_day = 3

[accounts.threads.handle]
use = "noswipe"
posts_per_day = 3
```

## Stats

`stats.py x|threads "start of the text" --account handle --label 1h`: finds the post on our profile and parses the row's
accessibility labels (`N replies / reposts / likes / views`, or Threads' `Like 12` buttons). Stored as `{views, likes, replies,
reposts, quotes, bookmarks}`; numbers the app does not show stay empty. The dashboard's Post stats tab maps replies -> comments,
reposts -> shares. **Deferred**: Threads' own insights screen (views), X's analytics screen, and the **permalink** (it sits behind
Share -> Copy link and Android does not let adb read the clipboard; `permalink` is `null` in the result).

## Not built

Delete flow, research, permalink, Threads views, replies/threads (multi-part posts), polls, links previews, emoji / non-ASCII text.

## LIVE-VERIFY checklist (after the owner logs in on a practice account)

1. Open X: record the real ids of the avatar / drawer button, the "@handle" node, "Profile", the compose button (`composer_write`?),
   the text field (`tweet_text`?), `button_tweet`, the add-media button, the picker tile labels, the confirm button, the attached
   preview node. Fix `X` in `social.py` where the guess differs; update this page.
2. Same for Threads: bottom tabs (is "Create" the compose entry?), compose field id, Post button, media button and tiles, the
   profile header username node, how a post row exposes its counts.
3. Run `python x_post.py "test post" --account <practice>` by hand; check each printed `tap ...` line; delete the post afterwards.
4. Run once with `--media photo.jpg`, once with a video (check the file picker shows the pushed file first).
5. Run `stats.py x "test post" --account <practice>`: do the numbers match the screen?
6. Check the notification / photo-access permission prompts: answer them once by hand.
7. Then flip `use = "noswipe"` and let the preflight and a plan run.

## Threads: verified on phone-2 (2026-11-28, real dumps in `tests/fixtures/`)

- Bottom tabs are plain Views (not clickable in the dump, no id) labelled Feed / Messages / Create / Activity / Profile. Tap by label.
- Compose ("New thread"): EditText `new_thread_screen_composer` (bare resource id, no package prefix), Post is a plain TextView "Post".
  Closing with text asks "Save to drafts?": discard with "Don’t save" (never "Save").
- Login detection needs two consecutive log-in reads, runs after permission / notification sheets are dismissed ("Don’t allow"),
  and another app in the foreground (a system notification-settings page) brings Threads back instead of reading as logged out.

## Instagram post verification (instagram_upload.py)
After Share: wait for the share screen to close (notices printed), reach the Profile tab through sheets / viewers, then poll the
Reels grid up to 6 min (pull to refresh) and open the newest 3 tiles, matching the caption. Any view count matches (was: only 0).
