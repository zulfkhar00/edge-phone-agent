"""Morning preflight: find problems hours before the first post, not at noon.

Checks (each one independently; all problems are reported together):
  phone      adb connected, screen wakes, battery level / temperature, TikTok + Instagram installed
  accounts   every account with use = "noswipe" is still logged in (switch to it and confirm the profile)
  engine     free disk space on this machine
  server     the render server answers over Tailscale, its disk has room, the last nightly run ended cleanly
  plan       today's plan has arrived (only when [sync].expect_plans is on)

Exit 0 and print "RESULT {...}" when all is well; otherwise print "ABORTED: <all problems>" and exit 1, so the engine
retries once and then emails the problems.

    python preflight.py            # all checks
    python preflight.py --no-accounts
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path

import phones as phonecfg

ROOT = Path(__file__).parent
CFG = tomllib.loads(Path(os.environ.get("ENGINE_CONFIG") or ROOT / "config.toml").read_text())
PKGS = {"tiktok": "com.zhiliaoapp.musically", "instagram": "com.instagram.android"}


PHONES = phonecfg.load_phones(CFG)


def this_phone() -> phonecfg.PhoneCfg:
    """The phone this check is for: the engine passes PHONE_ID (the first phone when run by hand)."""
    pid = os.environ.get("PHONE_ID")
    return next((p for p in PHONES if p.id == pid), PHONES[0])


def serial() -> str:
    """The engine passes the live transport in PHONE_SERIAL."""
    return os.environ.get("PHONE_SERIAL") or this_phone().usb or this_phone().wifi_serial


def adb(*args: str, timeout: int = 20) -> str:
    try:
        return subprocess.run(["adb", "-s", serial(), *args], capture_output=True, text=True, timeout=timeout).stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


def check_phone(problems: list[str], info: dict) -> bool:
    if adb("get-state") != "device":
        problems.append(f"phone {this_phone().id} ({serial()}) is not connected over adb")
        return False
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    bat = adb("shell", "dumpsys", "battery")
    level = re.search(r"level: (\d+)", bat)
    temp = re.search(r"temperature: (\d+)", bat)
    powered = re.search(r"(AC|USB|Wireless) powered: true", bat)
    info["battery_pct"] = int(level.group(1)) if level else None
    info["battery_c"] = int(temp.group(1)) / 10 if temp else None
    info["charging"] = bool(powered)
    if info["battery_pct"] is not None and info["battery_pct"] < 30 and not powered:
        problems.append(f"phone battery at {info['battery_pct']}% and not charging")
    if info["battery_c"] is not None and info["battery_c"] >= 45:
        problems.append(f"phone battery is hot: {info['battery_c']} C")
    pkgs = adb("shell", "pm", "list", "packages")
    for app, pkg in PKGS.items():
        if f"package:{pkg}" not in pkgs:
            problems.append(f"{app} is not installed on the phone")
    return True


def check_accounts(problems: list[str], info: dict) -> None:
    for app, accts in CFG.get("accounts", {}).items():
        for handle, lim in accts.items():
            if lim.get("use") != "noswipe" or not this_phone().owns(app, handle):  # only this phone's accounts
                continue
            r = subprocess.run([sys.executable, "accounts.py", app, handle], cwd=ROOT, capture_output=True, text=True,
                               timeout=240)
            ok = r.returncode == 0 and ("now on" in r.stdout or "already on" in r.stdout)
            info[f"{app}@{handle}"] = "ok" if ok else "FAILED"
            if not ok:
                why = next((l for l in r.stdout.splitlines() if l.startswith("ABORTED")), r.stdout.strip()[-150:])
                problems.append(f"{app} @{handle} could not be opened (logged out? challenge screen?): {why}")


def check_engine_disk(problems: list[str], info: dict) -> None:
    free = shutil.disk_usage(ROOT).free / 1e9
    info["engine_free_gb"] = round(free, 1)
    if free < 5:
        problems.append(f"only {free:.1f} GB free on the engine machine")


def check_server(problems: list[str], info: dict) -> None:
    s = CFG.get("sync") or {}
    if not s.get("host"):
        return
    cmd = ("df --output=avail -B1G ~ | tail -1; "
           "f=$(ls -1d ~/nightly/runs/*/ 2>/dev/null | tail -1); [ -n \"$f\" ] && tail -2 \"$f/run.log\" 2>/dev/null")
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", s["host"], cmd],
                           capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        problems.append(f"render server {s['host']} did not answer in time")
        return
    if r.returncode not in (0, 1):
        problems.append(f"render server {s['host']} is unreachable: {r.stderr.strip()[:150]}")
        return
    lines = r.stdout.strip().splitlines()
    free = int(lines[0]) if lines and lines[0].strip().isdigit() else None
    info["server_free_gb"] = free
    if free is not None and free < 10:
        problems.append(f"only {free} GB free on the render server")
    last = " ".join(lines[1:])
    info["last_nightly"] = last[-120:]
    m = re.search(r"exit=(\d+)", last)
    if s.get("expect_plans") and m and m.group(1) != "0":
        problems.append(f"the last nightly run did not end cleanly: {last[-150:]}")


def check_plan(problems: list[str], info: dict) -> None:
    s = CFG.get("sync") or {}
    if not s.get("expect_plans"):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    con = sqlite3.connect(ROOT / "engine.db")
    me = this_phone()
    n = con.execute("select count(*) from jobs j join plans p on p.name = j.plan where p.day=? and j.kind='post' "
                    "and (j.phone_id=? or (j.phone_id is null and ?))",
                    (today, me.id, me is PHONES[0])).fetchone()[0]
    info["posts_today"] = n
    posting = any(lim.get("use") == "noswipe" and me.owns(app, name)
                  for app, accts in CFG.get("accounts", {}).items() for name, lim in accts.items())
    if n == 0 and posting:  # a phone whose accounts are research-only has nothing to post
        problems.append(f"no posts planned for {today} on phone {me.id} (did the nightly plan arrive?)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-accounts", action="store_true")
    a = ap.parse_args()
    problems: list[str] = []
    info: dict = {}
    check_engine_disk(problems, info)
    check_server(problems, info)
    check_plan(problems, info)
    if check_phone(problems, info) and not a.no_accounts:
        check_accounts(problems, info)
    print(json.dumps(info, indent=1))
    if problems:
        print("ABORTED: " + " | ".join(problems), flush=True)
        sys.exit(1)
    print("RESULT " + json.dumps(info), flush=True)


if __name__ == "__main__":
    main()
