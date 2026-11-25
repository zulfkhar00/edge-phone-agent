# How the flows work (the pattern to copy for a new app)

Everything here is read off the existing Instagram / TikTok automation. X and Threads (`social.py`) follow it line by line.
Per-app screen notes live in `flows/*.md`; this page is the shape they all share.

## 1. The job model (`engine.py`)

- One row per thing to do in `engine.db` `jobs`: `kind` (post | stats | research | delete | preflight | ...), `app`, `account`,
  `run_at`, `args` (JSON), `status` (waiting -> running -> done | failed | unverified | skipped), `phone_id`, `post_key`.
- One runner loop per phone, one job at a time. Fixed jobs (post, delete, stats, preflight) run at their time; research only
  fills the gaps. The runner never imports a flow: `Runner.command()` builds `python <flow>.py ... --account <handle>` and
  runs it as a **subprocess** with `PHONE_SERIAL` / `PHONE_ID` in the environment. Output goes to `logs/<id>.log`.
- The subprocess talks back with two line prefixes only: `RESULT {json}` (success, stored on the job) and `ABORTED: why`
  (failure text). Exit code 0 = done.
- Failure policy (`Runner.run`): retry once after 5 min, then `failed` + alert (dashboard, email). A post whose log contains
  `tap Post at` / `tap Share at` (printed by `Phone.tap` when the publish button is tapped) is **never retried**: it becomes
  `unverified`, its stats jobs are still scheduled (they show whether it exists) and an alert asks the owner to look.
  A phone that dropped off adb mid-job fails the job at once (no retry into a dead cable).
- A done post schedules `stats` jobs at the plan's offsets (1h 3h 6h 12h 24h 48h). Stats find the post again by the **first
  30 characters of its caption**, so those must be unique forever (enforced by `plan_check.py`).
- Quotas: `[accounts.<app>.<handle>]` in `config.toml` (`posts_per_day`, `likes_per_day`, ...). `used_today()` counts done +
  unverified posts; `Runner.run` skips a post over its limit, `plan_check` drops it at import. A phone's `quotas = {...}`
  overrides its accounts' limits. An account lives on exactly one phone (`phones.py`).

## 2. The plan (`mailbox/inbox/<day>/plan.json`) and `plan_check.py`

The nightly run on the render server writes the plan; it is the only contract between the repos. Nothing in it is trusted:
`check_plan` validates every post / research / delete on its own, drops the bad ones (reported as one alert), keeps the good
ones. Rules worth knowing: day within 2 days, caption one line of printable ASCII (it is typed with `adb input text`),
caption key unique, video inside the plan folder + ffprobe (vertical, 3-90 s), time `at` HH:MM 09:00-23:00 or `round` 12/16/20,
account present in `config.toml` and `use = "noswipe"` (practice accounts only in `practice` plans), daily post limit, research
capped by the account's like / comment limits, deletes only in practice plans. `engine.import_plans` turns the clean plan into
jobs, one per post x target, on the phone that owns the account.

## 3. Phone helpers (`phone.py`, `scrcpy_bridge.py`)

- `Phone.nodes()` dumps the UI tree (uiautomator2) as dicts: `label` (= content-desc or text), `resource-id`, `box`, `center`,
  `clickable`, `checked`, ... Flows never use fixed coordinates when a node exists. `find(label|regex)`, `by_id(suffix)`.
- `wait(what, *labels)`: poll until every label is on screen, waking the screen each time; otherwise `fail`.
- `tap_node(node, why)` / `tap(x, y, why)` (logs `tap <why> at (x,y)`), `type_text` (`adb input text`), `wake`.
- `push_video` copies the file into `DCIM/flagd` and broadcasts a media scan so it is the newest item in any picker;
  `remove_video` deletes the file and its MediaStore row. Uploaders remove their copy even on failure.
- `fail(msg)` saves `runs/fail-<time>-<phone id>.png` and raises `Abort(msg + screenshot path)`. The engine links that
  screenshot on the failed job (per phone, by file name). **Anything unexpected aborts; the flow never guesses.**
- `scrcpy_bridge.py` only serves the dashboard's live screen / touch input (Take over). Flows do not use it.

## 4. The posting flow (`instagram_upload.py`, `tiktok_upload.py`, `social.py`)

1. Push the media. 2. Start the app fresh (`app_start(pkg, stop=True)`: apps get left in search, comments, editors).
3. `wait` for a recognisable home screen; dismiss nag dialogs from a short **safe list** (`Not now`, `Skip`...); paid upsells
(Meta Verified, Premium, Boost) are closed with Back, never tapped. 4. Open create, pick the newest media tile (check it is
from today), Next. 5. Fill the caption, then **read it back** and compare. 6. Check every toggle (Facebook / Story / AI label
must be off) and the audience / visibility value before Share. 7. Tap Share/Post (the "may have gone out" line). 8. Wait, then
**verify on the profile**: the new item with our caption. Pull to refresh while waiting; never restart the app after Share.
9. `finally`: remove the pushed copy. On a pre-Share abort, discard the half-made post (**Start over / Delete, never Save draft**).

## 5. Stats (`stats.py`, `insights.py`)

Per post, per label (1h...48h): open the post found by its caption, read numbers from native text (TikTok live counts, IG Reel
insights; OCR only where the screen is a web view), `save()` a snapshot row in `stats.db`, print `RESULT`. `insights.py`
(pure, no phone) turns snapshots into the dashboard's Post stats tab; `snap_metrics` maps each app's raw data to common
metric names; a metric the app does not give stays empty (never 0 by guess).

## 6. Research (`research.py`, `research_seeds.py`)

Search a keyword, watch, optionally like / comment within the day's limits, store what was seen in `research.db`. Never taps
Share, Save, Follow. IG / TikTok only; X and Threads have no research flow (plan_check rejects it).

## 7. Accounts and the morning check (`accounts.py`, `preflight.py`)

`accounts.switch(p, app, handle)` starts the app clean, reads which account is active, switches **only between accounts already
logged in** (never "Add account"), and verifies on the profile. Every uploader takes `--account` and calls it first.
`preflight.py` (09:30 per phone): adb up, battery, apps installed, each `use = "noswipe"` account switchable (so logged in),
disk, render server, today's plan arrived. All problems are reported together as one `ABORTED:` so the engine alerts once.

## 8. Alerts, take over, pause

- `alert(kind, text, every_min=...)` writes to the dashboard and emails (Gmail app password); repeated alerts of one kind
  are rate limited. healthchecks.io covers "the engine itself stopped".
- **Take over** (dashboard, per phone) pauses that phone's runner: research / stats stop at once, a post / delete finishes
  first; the live screen then takes taps and keys. It resumes by itself after `resume_after_min` of no touching. A job
  interrupted by a take-over goes back to `waiting` (research / dating end as done "stopped early").
- Self-update: CI moves `deploy` after green tests; the engine fast-forwards only while idle (`updater.py`).

## 9. Tests (`tests/`)

UI flows cannot run in CI, so the logic around them is tested with fakes:
`fake_adb.py` (transport discovery), flow subprocesses replaced by `python -c "print('RESULT ...')"` through
`monkeypatch.setattr(eng.Runner, "command", ...)`, throwaway engines from `conftest.make_engine` (own config, db, folders),
`check_plan` against a temp plan folder, `insights` on fixture snapshots. `tests/fake_phone.py` adds a **screen-graph fake**
(`FakePhone`) so a whole flow runs: nodes per screen, taps move between screens, typing fills the field, and `time.sleep`
advances a fake clock so timeouts are instant. Assert on the *taps made* (what was never tapped matters most) and on the
`tap Post at` line.

## 10. Adding an app, the short version

1. `phones.py`: app alias + `AUTOMATED_APPS`. 2. `config.toml`: `[accounts.<app>.<handle>]` limits. 3. A flow script with
`--account`, `RESULT` / `ABORTED:`, `tap Post at` on the publish tap. 4. `plan_check.py` rules. 5. `Runner.command` + `import_plans`
args. 6. `stats.py` branch + `snap_metrics`. 7. `preflight.py` login check. 8. Dashboard tag / pill. 9. Fake-screen tests, then
one supervised run on a practice account, then update `flows/<app>.md`.
