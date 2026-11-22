"""Finding one of our posts on a profile grid: caption matching, pinned tiles, and the scan itself.

Pure logic (no phone imports) so it is testable with a fake screen. stats.py wires it to the real phone.

A post is identified by the first KEY_LEN characters of its caption after normalisation: Unicode-folded,
lower-case, letters and digits only (so emoji, punctuation, line breaks and a trailing "... more" never matter).
A caption with no letters or digits at all (emoji only) falls back to its non-space characters.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import Counter
from typing import Callable

KEY_LEN = 30
MAX_TILES = 30          # reels opened per lookup, newest first (the account posts up to 6 a day, so 24-48 h = ~7-15 down)
MIN_TRUNCATED = 12      # a truncated caption on screen must be at least this long to count as a prefix match
_MORE = re.compile(r"(?:…|\.{3})\s*(?:more|ещё|еще|көбірек)?\s*$", re.I)
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁯︀-️﻿]")


class NoTiles(Exception):
    """The grid showed no reel tiles at all: the profile did not load (or a wall is in the way). A real failure."""


class NotFound(Exception):
    """The whole scan finished and the post is not on the profile (deleted, hidden, never published).
    Not a failure of the phone or the app: the caller records it once instead of retrying and alerting."""


def normalize(text: str | None) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = _INVISIBLE.sub("", _MORE.sub("", t.strip()))
    alnum = "".join(c for c in t.casefold() if unicodedata.category(c)[0] in "LN")
    if alnum:
        return alnum
    return "".join(c for c in t if not c.isspace())  # emoji-only caption


def caption_key(caption: str | None) -> str:
    return normalize(caption)[:KEY_LEN]


def caption_matches(key: str, screen_texts) -> bool:
    """True if any on-screen text contains the caption key, or is a truncated start of the caption."""
    if not key:
        return False
    for s in screen_texts:
        n = normalize(s)
        if not n:
            continue
        if key in n or (len(n) >= MIN_TRUNCATED and key.startswith(n)):
            return True
    return False


def node_texts(nodes: list[dict]) -> list[str]:
    out: list[str] = []
    for n in nodes:
        for t in (n.get("label"), n.get("text"), n.get("content-desc")):
            if t and t not in out:
                out.append(t)
    return out


def is_pinned(label: str) -> bool:
    return bool(re.search(r"\bpinned\b", label or "", re.I))


def scan_reels(get_tiles: Callable[[], list[dict]], open_tile: Callable[[dict, int], None],
               is_match: Callable[[], bool], close: Callable[[], None], scroll: Callable[[], None],
               limit: int = MAX_TILES, deadline: float | None = None, max_stalls: int = 2) -> int:
    """Open the profile's reel tiles newest first, scrolling the grid, until is_match() is true.

    get_tiles() returns the visible tiles (dicts with "label"), top-left first. Pinned tiles are skipped (they sit
    on top regardless of age). Tiles are told apart by label (a repeated label is counted); the end of the grid is
    when scrolling does not change what is visible. Returns how many reels were opened to find it; raises NotFound
    after a full scan, TimeoutError if the deadline passes first (an incomplete scan proves nothing)."""
    seen: Counter = Counter()
    opened = stalls = 0
    last_sig = None
    while opened < limit:
        if deadline is not None and time.time() > deadline:
            raise TimeoutError(f"stopped after {opened} reels (time budget used up)")
        tiles = get_tiles()
        count: Counter = Counter()
        fresh = None
        for t in tiles:
            count[t["label"]] += 1
            if count[t["label"]] > seen[t["label"]]:
                seen[t["label"]] += 1
                if is_pinned(t["label"]):
                    continue
                fresh = t
                break
        if fresh is not None:
            stalls = 0
            opened += 1
            open_tile(fresh, opened)
            if is_match():
                return opened
            close()
            continue
        sig = tuple(t["label"] for t in tiles)
        stalls = stalls + 1 if sig == last_sig else 0
        if stalls >= max_stalls:
            break  # scrolling no longer changes the grid: that was all of it
        last_sig = sig
        scroll()
    if not seen:  # not even a pinned tile was ever visible
        raise NoTiles("no reel tiles on the profile grid")
    raise NotFound(f"no reel with this caption among {opened} reels scanned")
