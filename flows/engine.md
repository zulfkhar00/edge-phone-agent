# Content engine (job runner + dashboard)

Start: `cd ~/content-engine && uv run python engine.py` → http://127.0.0.1:8080
Settings: `config.toml` (phones, accounts + daily limits, stats offsets, email alerts).

## Plans
Drop `mailbox/inbox/<name>/plan.json` (+ `videos/`). Imported once, turned into jobs in `engine.db`.
```json
{"day": "2026-11-03",
 "posts": [{"id": "h1a", "round": 12, "video": "videos/h1a.mp4", "caption": "...",
            "targets": [{"app": "tiktok", "account": "x", "visibility": "everyone"},
                        {"app": "instagram", "account": "y", "audience": "everyone"}]}],
 "research": [{"app": "tiktok", "account": "x", "keyword": "texting anxiety", "minutes": 10,
               "max_likes": 3, "max_comments": 1, "not_before": "09:00", "not_after": "23:00"}],
 "deletes": [{"app": "tiktok", "account": "x", "caption": "...", "at": "23:30"}],
 "stats_offsets_min": [60, 180, 360, 720, 1440, 2880]}
```
- `round` 12/16/20 → random minute of that hour; `"at": "HH:MM"` for an exact time.
- After a post succeeds, stats jobs are added at the offsets (TikTok Post analysis from 24h on).
- Research fills gaps: it only starts when the next fixed job is at least `research_margin_min` + 2 min away,
  and is shortened to fit. Daily limits per account cap likes, comments, research minutes, posts.

## Failures
- A failed job retries once after 5 min, then is marked failed + alert (dashboard; email when an app password is set).
- A post that failed after tapping Post/Share is marked `unverified` (never re-posted automatically); stats still run.
- Each job's log: `logs/<id>.log`; failure screenshot linked in the dashboard.

## Take over
Dashboard → Take over: research/stats stop at once, a post/delete finishes first. Then the live screen (scrcpy)
accepts taps, swipes, keys and text. Resume hands the phone back.
Live video needs WebCodecs = https or localhost. On the Pi: `tailscale serve --bg 8080`.

## Lessons from the 2026-11-02 practice day
- Every job starts the app fresh (force-stop + start): apps get left in search, comments, editors.
- TikTok keeps an interrupted post ("Continue editing this post?" Save draft / Edit). Never Save draft:
  Edit → Back (becomes a draft) → delete the newest draft, only if dated today. Pop-ups can appear seconds after
  the profile opens: keep checking while waiting.
- TikTok editor: left button is now "Story" + counter (was "Your Story"). Post screen has a new "Add link" row and
  location chips load late: tap the visibility *text* once the layout stops moving.
- TikTok saves each posted video to DCIM/Camera: the uploader removes it (same size, last 15 min) and always removes
  its own pushed copy, even on failure.
- Instagram: new reels have no "N views" yet, only an "Insights" button. Cold start can sit on the splash for 10+ s.

## Disk clean-up
- Engine (hourly): a plan's `videos/` are deleted once none of its posts is waiting/running; `logs/*.log` > 30 days
  and `runs/*.png` > 14 days are deleted.
- Render VM (root@77.68.97.177, cron `/etc/cron.d/render-cleanup`, script `/usr/local/bin/render-cleanup`):
  renders in `/root/noswipe-videos/out` > 24 h, Remotion/Chrome temp files in `/tmp` > 6 h; if the disk passes 85%,
  renders > 2 h.
- Secrets: `config.toml` (Gmail app password) is gitignored; copy `config.example.toml` to start.

## Sync with the render server (Tailscale)
Every `[sync].every_min` (10) minutes the engine, over SSH to `[sync].host` (studio@100.102.103.115 = render-vm):
- pushes consistent copies of `stats.db`, `research.db`, `engine.db` to `~/feedback/` (the nightly session reads them),
- pulls finished plans from `~/outbox/<name>/` into `mailbox/inbox/<name>/` (staged in `mailbox/incoming/` first) and
  deletes them on the server. The server writes `~/outbox/.<name>.part/` and renames it when complete.
The engine starts both directions; the server never needs access to the engine machine.

## Several phones
`[[phones]]` in `config.toml` (see README "Several phones"). One runner task per phone; `jobs.phone_id` says which
phone runs a job (NULL = jobs from before multi-phone: the first phone's). Per-phone state lives in the `state` table
as `paused:<id>`, `touched:<id>`, `job_pgid:<id>`. Flow scripts get `PHONE_SERIAL` (the live transport) and `PHONE_ID`
from the engine; failure screenshots are named `fail-<time>-<id>.png` so each runner links only its own. Daily limits
stay per account, so two phones never share a limit; one account is never on two phones.
