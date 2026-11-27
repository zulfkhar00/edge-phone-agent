# Reading post stats — verified 2026-11-02 18:00–18:17 (stats.py)

## TikTok
- **Live counts (text, real time)** on our own post screen: "N views" (bottom bar), like/comment/share labels,
  saves = number under the Favorites button. Good for the 3 h / 12 h snapshots.
- **Post analysis** = ••• (unlabeled, between the saves count and the sound disc; banners move it) → `Analytics`.
  It's a web view (SparkActivity): **no text in the UI tree**, read with OCR (macOS Vision via `ocrmac` on the Mac,
  tesseract on Linux). Read each screenshot separately — never merge OCR boxes across screenshots.
  Shows "Updated on <date>": refreshes about **once a day**, so only read it from the 24 h snapshot on.
  Overview: post views, total play time, average watch time + watched full video (videos), photos viewed
  (photo posts), new followers, traffic sources (For You / Search / …), search queries. Tabs: Viewers, Engagement.
  OCR misses a lone large "0" (e.g. New followers) → stored as null.
- The music disc right under ••• opens the sound page — never tap blind in that column.

## Instagram
- Our reel shows "N views" (bottom right) → tap → **Reel insights**, native text:
  likes/comments/reposts/shares/saves row, Views, Viewers, Average watch time, Follows,
  **Skip rate** (vs "Your typical reel"), Share/Like/Save/Repost/Comment rate, sources (Reels tab / Explore / …).
  Real time, so 3 h / 12 h / 24 h snapshots all work.

## 2026-11-02 additions
- Insights need a professional account (@tinyloveshows was personal: no insights at all; switched to professional by the user).
- The reel's way into insights varies: "<N> views" link, an "Insights" button (brand-new reel), or a "<N> • View insights" bar
  under the caption. Next to that bar is "Boost reel" (paid promotion): never tap it.

## Finding the post (2026-11-21)
- The reel is found by caption, not by position: the profile grid is scrolled and up to 30 reels are opened, newest
  first. Pinned tiles ("Pinned" in the tile description / badge) are skipped. A 24-48 h old reel is often 7-15 tiles down.
- Caption match: first 30 characters of letters/digits only (case, emoji, punctuation, line breaks and a trailing
  "... more" are ignored; an emoji-only caption falls back to its emoji). Code: `stats_lookup.py`.
- A full scan without a match prints `NOT_FOUND: ...` and exits 3 (failure screenshot in `runs/`). The engine records
  the stats job as `not_found` (terminal, no retry) and lists it in ONE "stats" alert per phone and day. Phone off,
  app crash, login wall, empty grid or the time budget (7 min) still abort normally: retry once, then alert.
- Not done: opening the reel by its link. Nothing stores a permalink at publish time (the share flow never reads it).
