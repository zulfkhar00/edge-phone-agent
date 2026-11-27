# edge-phone-agent

Runs noswipe's short-video accounts on a real Android phone, from a Raspberry Pi next to it. It posts the videos
that the nightly run in [noswipe-playbook](https://github.com/zulfkhar00/noswipe-playbook) makes, reads each post's
stats, runs research sessions (search, watch, like, comment), and serves a dashboard with a live phone screen.
Python 3.12, asyncio, `adb` + scrcpy. Everything is driven through the phone's UI: no platform APIs.

## The system

```
 render server (IONOS VM, Tailscale 100.102.103.115)        Raspberry Pi 5 (this repo)                phone (moto g 5G)
 repo noswipe-playbook, cron 00:30 PT                         ~/content-engine, systemd                Instagram / TikTok
   nightly Claude Code run                                     engine.py
     reads ~/feedback/*.db  <------- rsync push every 10 min -- stats.db research.db engine.db
     writes ~/outbox/<day>/ ------- rsync pull every 10 min --> mailbox/inbox/<day>/ -> jobs   -- adb over Wi-Fi -->
                                                               dashboard :8080 (local), :8081 (PIN)
                                                               <- Cloudflare Tunnel <- https://dashboard.zulfkhar.dev
 healthchecks.io  <- nightly start/exit pings                  <- engine heartbeat every 5 min
```

- The Pi always opens the connection (SSH over Tailscale). The server never needs access to the Pi.
- A day's loop: 00:30 PT the server plans and renders that day's videos into `~/outbox/<day>/` (about 1.5 h) ->
  the Pi pulls it and imports it as jobs -> 09:30 preflight -> posts from 12:30 at planned times -> stats reads at
  1h 3h 6h 12h 24h 48h after each post -> pushed back to the server, where the next night learns from them.
- The plan is the only contract between the two repos (see "Plan format"). Stats reach the server as the raw
  SQLite files.

## Layout

| Path | What it does |
|---|---|
| `engine.py` | The whole service: job runner, plan import, sync with the server, alerts (dashboard + email), heartbeat, self-update trigger, auto-resume, web dashboard and API |
| `plan_check.py` | Validates a plan before it becomes jobs (nothing in it is trusted: paths, captions, accounts, limits, video files) |
| `preflight.py` | Daily morning checks: phone, battery, accounts logged in, disks, render server, today's plan |
| `phone.py` | Shared phone helpers: read the screen as labelled nodes, wait for an expected screen, tap by label/id, type. Anything unexpected aborts with a screenshot |
| `instagram_upload.py`, `tiktok_upload.py` | Post one video, checking every screen before tapping |
| `social.py`, `x_post.py`, `threads_post.py` | X and Threads: login gate, text post (+ image/video), verify on profile, per-post counts (see `flows/x_threads.md`) |
| `stats.py` | Read one post's insights (OCR with macOS Vision, tesseract on Linux) |
| `research.py` | Research sessions: search a keyword, watch, like and comment within limits; results in `research.db` |
| `explore.py` | Dev tool: maps an app's screens and how they connect (for writing new flows) |
| `accounts.py`, `delete.py` | Switch accounts in each app; delete test posts |
| `outliers.py`, `reel_record.py` | Niche-outlier scan and reel recording/breakdown (scan is off; used by hand for reference accounts) |
| `updater.py` | Self-update from `origin/deploy` (see "CI/CD") |
| `access.py` | PIN gate for the remote dashboard |
| `scrcpy_bridge.py`, `vendor/scrcpy-server-*.jar` | Live screen and touch input for the dashboard |
| `adb_keeper.sh` | Keeps every phone reachable (one watcher each): reconnects, finds it by hardware serial on any network, updates its Wi-Fi address in `config.toml`, restarts the engine |
| `phones.py` | The phone list: `[[phones]]` parsing and validation, which phone owns which account, USB-then-Wi-Fi transport discovery, helpers for `adb_keeper.sh` |
| `insights.py` | Pure aggregations behind the dashboard's Post stats and Research tabs and `/api/trends` (deltas, series, A/B, formats, rising themes) |
| `research_seeds.py` | Seed keywords for the engine's own daily research sessions (opt-in, `[research_seeds]`) |
| `web/dashboard.html` | The dashboard (single page) |
| `web/mock_server.py`, `web/fixtures/` | Serves the dashboard with sample data and no phone: `python3 web/mock_server.py multi 8099` (the insights tabs run the real `insights.py` over `fixtures/insights.json`) |
| `docs/how-flows-work.md` | The pattern every flow follows: job model, plan, phone helpers, verification, stats, preflight, tests with fakes |
| `flows/*.md` | How each app flow works and what broke before. Read the relevant one before changing an uploader or reader |
| `tests/` | pytest: plan validation, self-update, access gate, outliers, multi-phone (fake adb, no phones needed) |
| `*_chain.sh`, `build_viewer.py`, `publish_viewer.sh` | Old one-off tools from the Mac era; not used by the service |

Local only (gitignored): `config.toml` (secrets), `engine.db` (jobs, alerts, state), `stats.db`, `research.db`,
`mailbox/`, `logs/`, `runs/` (screenshots, recordings).

## Data

- `engine.db` `jobs`: `kind` = post | stats | research | delete | preflight | outliers (X / Threads posts are `kind='post'` with `app` x / threads), `status` = waiting | not_found | running |
  done | failed | unverified | skipped, `run_at` / `not_after` (unix time), `args` JSON, `result` JSON,
  `post_key` (`<plan>/<post id>/<app>`). Also `alerts`, `plans` (imported names) and `state` (k/v JSON: `paused`,
  `touched`, ...).
- `stats.db` `snapshots`: app, account, caption, label (1h ... 48h, manual), taken_at, data JSON (views, viewers,
  average_watch_time_s, skip_rate_pct, follows, like/share/save/comment rates, sources_pct).
- `research.db`: `videos`, `comments` seen in research sessions.
- Posts are matched to stats by the **first 30 characters of the caption**, so those must be unique forever.

## Plan format (from the server)

`mailbox/inbox/<day>/plan.json` + `videos/`. The server writes `~/outbox/.<day>.part/` and renames it when complete.
```json
{"day": "2026-11-06",
 "posts": [{"id": "across-2-a", "at": "12:30", "video": "videos/across-2-a.mp4",
            "caption": "Title under 60 chars. Description. #tag #tag",
            "targets": [{"app": "instagram", "account": "tinyloveshows", "audience": "everyone"}]}],
 "research": [{"app": "instagram", "account": "tinyloveshows", "keyword": "swipe fatigue", "minutes": 12,
               "max_likes": 5, "max_comments": 1, "not_before": "10:00", "not_after": "16:00"}],
 "deletes": [], "stats_offsets_min": [60, 180, 360, 720, 1440, 2880]}
```
`at` = exact time, or `round` 12/16/20 = a random minute of that hour. Captions are one line of plain ASCII
(typed with `adb input text`). Accounts and daily limits come from `config.toml`; only `use = "noswipe"` accounts
may appear in plans. Details and validation rules: `flows/engine.md`, `plan_check.py`.

## How the runner behaves

- Housekeeping loop every 5 s: sync (every 10 min) -> import plans -> schedule preflight -> plan-arrival check ->
  clean-up -> heartbeat -> self-update check. Separately, **each phone has its own runner loop** (every 5 s) that
  runs the next due job of that phone.
- Fixed jobs (post > delete > stats) run at their time; research only fills gaps before the next fixed job.
- A failed job retries once after 5 min, then alerts. A post that fails after tapping Share becomes `unverified` and
  is never re-posted automatically.
- Dashboard "Take over" pauses that phone's runner (the other phones carry on) and hands you the phone. If nobody touches it for
  `[dashboard].resume_after_min` (10) minutes, it resumes by itself.
- Alerts go to the dashboard and by email (Gmail app password). healthchecks.io emails when the heartbeat stops
  (Pi off, offline, loop dead) or when a nightly run fails or never finishes.

## Stats, research and trend export

The **Post stats** tab (`GET /api/insights/stats?days=7|30&app=&account=`) shows, per account, posts / total and median
views / average watch / skip rate / follows for the last N days with the change vs the N days before, a ranked best-posts
list with a thumbnail and a 1 h to 48 h views sparkline per post, series grouping (a label like `whites-2-b` is series
`whites`, part 2, variant b) with per-series totals, and an A vs B comparison (a gap under 5% is a tie). Posts the engine
marked `unverified` and posts with no snapshot yet are shown as such and left out of the averages. TikTok has no skip rate.
Thumbnails are cut from the video with ffmpeg while it is still on disk (`thumbs/`, `GET /api/post/<job id>/thumb.jpg`).

The **Research** tab (`GET /api/insights/research?days=7|14|30|all&app=`) aggregates `research.db`: per keyword (videos
seen, median likes/comments, top creators and captions), rising hashtags and phrases (more sightings in the last 7 days
than the 7 before), "formats that work" (captions clustered by pattern: POV, part N, A/B choice, storytime, tips,
openers, relatable, question; engagement vs the typical video) and promotion ideas (the best video per winning format:
hook, CTA style, and an angle written for an AI dating assistant). Rule-based, no LLM, nothing leaves the Pi.

**Seed research** (off by default): engine-queued keyword sessions, so research does not depend on the nightly plan only.
Add to `config.toml`:

```toml
[research_seeds]
accounts = ["instagram:nomoreawkwardness", "tiktok:nomoreawkwardness"]   # must be in [accounts.*]
per_account_per_day = 3     # keyword sessions queued per account per day (rotating through the list)
minutes = 10
max_likes = 3
max_comments = 0
after = "10:00"
before = "22:00"
# keywords = [...]          # optional; default list in research_seeds.py (dating app burnout, AI dating assistant,
                            # hinge/tinder tips, bumble, first date ideas, situationship, talking stage, meet cute, rizz,
                            # dating in your 20s / 30s, ...)
```
They are ordinary research jobs: they only fill gaps between fixed jobs and the account's `research_minutes_per_day`,
`likes_per_day` and `comments_per_day` still cap them. Both apps' flows (`research.py`) support TikTok and Instagram.

### Trend export

`GET /api/trends?limit=10&days=14&app=tiktok|instagram` returns the top trends as JSON for the video generator (read-only;
the engine never pushes it anywhere). Same auth as the rest of the dashboard (the remote port needs the PIN cookie).

```json
{"generated_at": "2026-11-21T11:07:35-08:00", "window_days": 14, "filters": {"app": null},
 "source": {"videos": 143, "keywords": 12}, "product": "NoSwipe (an AI that swipes, chats and books the dates for you; you just show up)",
 "trends": [{"rank": 1, "theme": "situationship", "kind": "keyword", "score": 100.0, "trending": "rising",
             "posts_seen": 12, "creators": 7, "recent": 6, "prior": 3, "median_likes": 3900,
             "top_format": "explainer", "top_format_label": "Tips / explainer", "hook_example": "How to leave a situationship with dignity",
             "example_posts": [{"app": "tiktok", "creator": "pixelpanda", "caption": "...", "likes": 44000, "comments": 900, "shares": 300, "keyword": "situationship"}],
             "suggested_angle": "The talking stage that never ends: ..."}],
 "formats": [{"id": "series", "label": "Part N series", "posts": 11, "median_likes": 7400, "lift": 1.68}]}
```
`kind` is `keyword` (a research keyword), `hashtag` or `phrase` (two words). `score` is 0-100 relative to the best theme in
the response: `log10(1 + mean engagement of the theme's top 5 videos) x (1 + min(recent, 10) / 10) x 1.25 if rising`, where
a video's engagement is `likes + 3 x comments + 5 x shares`. A theme needs 2+ videos from 2+ creators; themes covering the
same videos under another spelling are merged. `trending` is `rising` when more of its videos were seen in the last half of
the window than the half before. An empty research database gives `"trends": []`.

## X and Threads (automated), Reddit (listed only)

X and Threads post text (+ an optional image or video), read per-post stats and are login-checked each morning. Pattern:
`docs/how-flows-work.md`; screens, status and the live-verification checklist: `flows/x_threads.md`.

```toml
[[phones]]
id = "phone-1"
accounts = ["ig:@tinyloveshows", "x:@noswipe_app", "threads:@noswipe_app", "reddit:u/noswipe_team"]

[accounts.x.noswipe_app]          # limits and `use`, like Instagram / TikTok
use = "noswipe"
posts_per_day = 3
[accounts.threads.noswipe_app]
use = "noswipe"
posts_per_day = 3
```
Plan post: `{"id": "t1", "at": "13:00", "text": "<= 280 chars (X) / 500 (Threads), plain ASCII", "media": "videos/p.jpg", "targets": [{"app": "x", "account": "noswipe_app"}, {"app": "threads", "account": "noswipe_app"}]}`
(`media` optional; no media needed when every target is X / Threads). The engine never logs in: a logged-out app or another account
fails the job at once with a `login` alert and a red "Needs login" pill; the owner logs in with **Take over**, then retries the job.
No research or delete flow for them. **The post / compose / media / profile steps have not been run on a logged-in phone yet**
(`LIVE-VERIFY` in `social.py`).

Reddit accounts can still be listed on a phone so they show in the dashboard. **Nothing runs for them** (no flow, plans naming
them are rejected, the preflight skips them); the owner logs in by hand with Take over and the dashboard shows "Login: unknown".

## Several phones

Plug more phones into the Pi's USB hub; each one runs its own accounts and jobs in parallel.

```toml
# before                                   # after (config.toml)
[phone]                                    [[phones]]
serial = "192.168.1.150:5555"              id = "phone-1"                  # the old [phone] was "phone-1"
hw_serial = "ZD222RWWGT"                   hw_serial = "ZD222RWWGT"
                                           wifi_serial = "192.168.1.150:5555"   # the old `serial`
                                           accounts = ["ig:@tinyloveshows", "tt:@nomoreawkwardness"]

                                           [[phones]]
                                           id = "phone-2"
                                           label = "Pixel, right"
                                           hw_serial = "<serial of the second phone>"
                                           accounts = ["ig:@secondaccount"]
```
`[phone]` alone still works (one phone, id `phone-1`, owns every account). Use `[[phones]]` **instead of** `[phone]`
(having both is refused). Rules, checked at start-up (the engine refuses to start on a bad setup): `hw_serial` is
required and unique, ids are unique, and an account lives on one phone only. `usb_serial` (optional) is for a phone
whose `adb devices` serial differs from `hw_serial`; `quotas = { posts_per_day = 2 }` overrides that phone's
account limits. Keep the `[accounts.<app>.<handle>]` sections: they still hold each account's limits and `use`.

How it works:
- A plan's post / research / delete goes to the phone that owns the account (`jobs.phone_id`); an account no phone owns
  raises an alert and its jobs are skipped, the rest of the plan imports. Each phone gets its own morning preflight.
- Transport per phone: USB first (`usb_serial`, else `hw_serial`), then Wi-Fi (`wifi_serial`). A Wi-Fi address is only
  trusted when the device behind it reports that phone's `hw_serial`, so a re-used IP never drives the wrong phone.
- A phone unplugged mid-job fails that job (a post already tapped is `unverified`) and alerts; the other phones go on.
  Take over, pause, auto-resume and the live screen are per phone too.
- `/api/state` keeps its old top-level fields (= the first phone) and adds `phones: [...]` with each phone's status,
  jobs, alerts, accounts, `heartbeat` / `heartbeat_age_s` / `alive` of its runner loop. Per-phone API:
  `GET /api/phones`, `GET /api/phones/<id>/state`, `POST /api/phones/<id>/takeover|resume|input`,
  `GET /api/phones/<id>/screen.png`, WebSocket `/api/phones/<id>/stream` (starts that phone's scrcpy session only
  while someone watches). The old endpoints (`/api/takeover`, `/api/resume`, `/api/input`, `/api/screen.png`,
  `/ws/screen`) act on the first phone, or on `?phone=<id>` / `"phone": "<id>"`.

Plug in a second phone and check it:
1. Enable USB debugging on it, plug it into the hub, tap "Allow" for this Pi's key.
2. `adb devices` must list both phones as `device`. Get its serial: `adb -s <serial> shell getprop ro.serialno`.
3. Add its `[[phones]]` entry with its accounts (log in to those accounts on that phone first), remove the old `[phone]`,
   then `sudo systemctl restart content-engine adb-keeper` (the keeper starts one watcher per phone).
4. Open the dashboard: both phones are listed. `curl 127.0.0.1:8080/api/phones` shows each phone's `serial`,
   `transport` (usb / wifi) and `alive`; `tail logs/adb_keeper.log` lines are tagged `[phone-1]`, `[phone-2]`.
5. Run each phone's morning check: the 09:30 preflight queues one per phone (see `jobs` where `kind='preflight'`).

## NoSwipe (dating accounts)

The rig can run the dating profiles [noswipe.app](https://www.noswipe.app) creates for its clients. Contract:
`docs/automation.md` in `zulfkhar00/date-finder`. **Off by default**: nothing below happens unless `[noswipe] enabled = true`.

```toml
[[phones]]
id = "phone-3"
hw_serial = "..."
noswipe_phone_id = "pi-rack-1-phone-3"   # optional: the id NoSwipe's admin Run panel assigned (default: id)

[noswipe]
enabled = true
base_url = "https://www.noswipe.app/api/automation/v1"
api_key_env = "NOSWIPE_AUTOMATION_KEY"   # the key itself lives only in the engine's environment, never in config.toml
```
All options (poll interval, session length, caps, quiet hours, pacing) are in `config.example.toml`.

What is **real** (and tested against a fake NoSwipe server, `tests/test_noswipe.py`):
- `noswipe.py` polls `GET /runs?phoneId=<noswipe_phone_id>` for every phone each `poll_every_min` and acts only on
  `shouldRun = true`. Each wanted run becomes a `dating_session` job (`jobs.kind`) on that phone, one open at a time per
  account, spaced by `session_every_min`. The runner fits it in the gap before the next fixed job, like research.
- A run turning `shouldRun = false` (client paused, unassigned, held): its waiting session is skipped at once and a running one
  gets SIGTERM and ends at its next step. If NoSwipe cannot be reached for `stale_after_min`, no new session starts and running
  ones stop (a stale list must never keep an account swiping). Take over stops sessions too.
- Reporting goes through an on-disk outbox (`noswipe_outbox.db`, shared by the engine and session processes). `POST /activity`
  is retried with exponential backoff (30 s up to 1 h); every event has a deterministic idempotency key (hash of client, app, kind
  and the real event's id), so a replay (lost answer, restart) is a `duplicate` and never a second count. `invalid` / `no_account`
  are dropped; `bad_time` is retried a few times with the server clock correction (read from the `Date` header), `occurredAt` is
  kept inside the accepted window (never ahead of the server clock, events older than 7 days are dropped).
- Session lifecycle in `dating.py`: quiet hours in the client's timezone (`timezone` on the run if NoSwipe sends one, else
  `default_timezone`), a time budget, a daily swipe cap per account (client-local day, counted in the outbox so it spans
  sessions), jittered pacing, graceful stop. Not logged in: one `needs_you` ("Sign-in needed for your Hinge account") per
  account per day, nothing else.
- Dashboard: each phone's page has a NoSwipe panel (enabled, API key present, last poll, runs assigned to this phone, outbox
  size, last errors); the phones overview shows a tag. `/api/state` -> `phones[].noswipe`.

What is **skeleton**: the phone steps. Opening Tinder / Hinge / Bumble, checking the login, swiping, reading new matches
sit behind the `DatingUI` interface (`dating.py`); the only implementation, `UnmappedUI`, raises `NotMapped` because the
screens have not been mapped on a real phone yet. A session therefore ends with outcome `not_mapped` and reports **nothing**:
no swipes, matches or chats are ever invented, and the UI's counts are never trusted beyond what was asked. Chat replies, date
booking and `PUT /clients/{id}/apps/{app}` status moves are not built (they need the mapped UI). To finish an app: implement
`DatingUI` for it (screen mapping like the other flows) and return it from `make_ui`.

Turn it on at the Pi: add the `[noswipe]` section (and `noswipe_phone_id` per phone), put
`NOSWIPE_AUTOMATION_KEY=<key>` in the systemd unit's environment (`sudo systemctl edit content-engine`, then
`[Service]` / `Environment=...` or an `EnvironmentFile=`), `sudo systemctl restart content-engine`, and watch the phone's NoSwipe
panel. Until the UI is mapped the sessions will show up as done with `not_mapped` and the outbox stays empty.

## Hub bridge (Hub's posting queue through the rig)

`hub_rig.py` + `[hub]` in config.toml (off by default). Hub cannot reach the Pi, so the rig pulls: every `poll_every_min` it asks
`GET {base_url}/api/rig/plan?phone=<id>` for each phone (items of models set to "via rig" in Hub's `data/rig.json`), downloads the
signed media URL into `mailbox/inbox/hub-<queue id>-a<attempt>/`, validates the item with `plan_check` (plan `source: "hub"`: accounts
with `use = "hub"` or `"practice"` are allowed, quotas and caption rules still apply) and writes a normal `plan.json`. From there it is
an ordinary post job on the phone that owns the account. Each finished job is reported with `POST /api/rig/result`
(`posted` / `unverified` / `failed`, from the job status) through a retry outbox (`hub_rig.db`); a replay is harmless on Hub's side.
Nothing is posted twice: an attempt number (Hub raises it when its Retry button hands a failed item over again) is imported once.
Captions are flattened to one line of ASCII (emoji dropped, `%` spelled out) because they are typed with `adb input text`.

Put the token in the engine's environment (never in config.toml): `HUB_RIG_TOKEN=<token>` in an `EnvironmentFile=` of the unit.

**Dry run** (`dry_run = true`, or an item Hub marks `dry`): the whole path runs (poll, download, plan, job) but the flow stops before
Post / Share, takes `runs/preview-*.png`, cancels the compose screen (Delete / Start over, never a draft) and the job ends `skipped`
("preview"): it does not count against the daily limit, schedules no stats and nothing is reported to Hub.
`python instagram_upload.py v.mp4 --caption "..." --account handle --preview` and `x_post.py` / `threads_post.py ... --preview` do the same by hand.

To add another account to rig mode: log it in on a phone (Take over), add it to that phone's `accounts` and an `[accounts.<app>.<handle>]`
section here, then in Hub's `data/rig.json` give its model the platform (`"instagram": true` or `{"phone": "phone-2", "handle": "x"}`).

## Setup (Raspberry Pi OS Lite 64-bit, Debian 13)

```bash
sudo apt install -y git adb ffmpeg tesseract-ocr rsync sqlite3
# scrcpy 3+ is not in Debian: build from source (meson, libsdl3-dev, prebuilt server) into /usr/local/bin
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up   # same tailnet as the render server
# cloudflared from Cloudflare's apt repo or .deb; tunnel "content-engine" -> ~/.cloudflared/config.yml
git clone -b deploy git@github.com:zulfkhar00/edge-phone-agent.git ~/content-engine   # read-only deploy key
cd ~/content-engine && uv sync
cp config.example.toml config.toml   # fill in: phone serial, accounts, email app password, remote PIN, health URL
```
- SSH key of `pi` must be in `studio@render-server:~/.ssh/authorized_keys` (sync uses BatchMode SSH).
- Phone: Developer options -> USB debugging; authorise this machine's `~/.android/adbkey`; switch adbd to TCP
  once over USB (`adb tcpip 5555`). MacroDroid on the phone turns Wireless debugging back on after reboots.
  Apps logged in to the accounts in `config.toml`.
- systemd units (`/etc/systemd/system/`), all `User=pi`, `Restart=always`, `WorkingDirectory=/home/pi/content-engine`:
  - `content-engine.service`: `uv run python engine.py`, output to `logs/engine.log`
  - `adb-keeper.service`: `/bin/sh ./adb_keeper.sh`, output to `logs/adb_keeper.log`
  - `dashboard-tunnel.service`: `cloudflared --no-autoupdate tunnel run content-engine`
- `pi` has passwordless sudo (the keeper restarts the engine with `sudo -n systemctl`).
- Wi-Fi: NetworkManager. Keep the Pi and the phone on the **same single network** (others saved with autoconnect off),
  or connect the phone by USB (then `[phone].serial` is the USB serial and the keeper is not needed).

Run locally on a Mac for development: same `uv sync` + `uv run python engine.py` (OCR uses macOS Vision there).

## Operate

```bash
ssh pi@100.84.144.56                         # Tailscale (LAN address changes with the network)
systemctl status content-engine adb-keeper dashboard-tunnel
tail -f ~/content-engine/logs/engine.log
sqlite3 ~/content-engine/engine.db "select id,kind,label,status,datetime(run_at,'unixepoch','localtime') from jobs order by run_at desc limit 20"
curl -X POST 127.0.0.1:8080/api/resume       # un-pause; /api/takeover pauses; /api/job/<id>/now|retry|skip
adb devices                                  # phone must show "device"
```
Dashboard: `http://127.0.0.1:8080` on the Pi (no PIN, local only) or `https://dashboard.zulfkhar.dev` (PIN,
`[remote].pin`; changing it logs everyone out). API routes are listed at the bottom of `engine.py`.

## CI/CD

- **CI** (`.github/workflows/ci.yml`, every push and PR): compiles every module, runs `pytest`.
- **CD**: green `main` -> CI moves `deploy`. The engine checks `origin/deploy` every 15 min and fast-forwards only
  while idle with no fixed job due within `[update].quiet_min` (20) min; re-runs `uv sync` when the lock changed;
  rolls back code that does not compile; never touches a checkout with local edits; alerts, then restarts itself.
  `adb-keeper` does not restart itself: `sudo systemctl restart adb-keeper` after changing `adb_keeper.sh`.
- Roll back with `git revert` on `main`.

## Contributing

- Work on `main` or a PR into it; never edit files on the Pi (local edits block self-update; `config.toml` is the
  only file that changes there).
- Never commit `config.toml` or databases. Secrets live only in `config.toml`.
- UI flows can't run in CI. After changing an uploader, reader or research flow, run it once by hand against the
  practice account (`use = "practice"`) and update its `flows/*.md`.
- Safety rules the code keeps: research on the live account stays within `config.toml` daily limits; the outliers
  scan only browses with the practice account (never like, follow, save or send); never tap paid upsells.
