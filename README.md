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
| `web/dashboard.html` | The dashboard (single page) |
| `flows/*.md` | How each app flow works and what broke before. Read the relevant one before changing an uploader or reader |
| `tests/` | pytest: plan validation, self-update, access gate, outliers, multi-phone (fake adb, no phones needed) |
| `*_chain.sh`, `build_viewer.py`, `publish_viewer.sh` | Old one-off tools from the Mac era; not used by the service |

Local only (gitignored): `config.toml` (secrets), `engine.db` (jobs, alerts, state), `stats.db`, `research.db`,
`mailbox/`, `logs/`, `runs/` (screenshots, recordings).

## Data

- `engine.db` `jobs`: `kind` = post | stats | research | delete | preflight | outliers, `status` = waiting | running |
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
