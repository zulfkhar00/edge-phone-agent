"""Shared phone helpers: read the screen as labelled nodes, wait for expected screens, tap, type.

Every flow script works the same way: wait until the expected labels are on screen, then tap a node
found by its label or id. Anything unexpected aborts with a screenshot instead of guessing.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path

import uiautomator2 as u2



def current_serial() -> str:
    """The phone this process drives: the engine sets PHONE_SERIAL (the live transport of the job's phone)."""
    return os.environ.get("PHONE_SERIAL", "192.168.1.150:5555")


SERIAL = current_serial()  # kept for the flow scripts; the helpers below take a serial and never read it
GALLERY_DIR = "/storage/emulated/0/DCIM/flagd"
SHOTS = Path(__file__).parent / "runs"


class Abort(Exception):
    pass


class Phone:
    def __init__(self, serial: str | None = None):
        self.serial = serial or current_serial()
        self.d = u2.connect(serial)
        self.h = self.d.window_size()[1]

    def adb(self, *args: str, timeout: int = 300) -> str:
        return subprocess.run(["adb", "-s", self.serial, *args], capture_output=True, text=True, timeout=timeout).stdout

    def nodes(self) -> list[dict]:
        xml = self.d.dump_hierarchy()
        out = []
        for m in re.finditer(r"<node [^>]*>", xml):
            a = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(0)))
            b = [int(v) for v in re.findall(r"\d+", a.get("bounds", ""))]
            if len(b) == 4:
                a["label"] = (a.get("content-desc") or a.get("text") or "").strip()
                a["box"] = b
                a["center"] = ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
                out.append(a)
        return out

    def find(self, label: str | re.Pattern, nodes: list[dict] | None = None, clickable: bool | None = None) -> dict | None:
        for n in nodes if nodes is not None else self.nodes():
            texts = (n["label"], n.get("text", ""), n.get("content-desc", ""))
            ok = any(re.search(label, t) for t in texts) if isinstance(label, re.Pattern) else label in texts
            if ok and (clickable is None or (n.get("clickable") == "true") == clickable):
                return n
        return None

    def wait(self, what: str, *labels: str, timeout: float = 20) -> list[dict]:
        """Wait until every label is on screen; abort with a screenshot otherwise."""
        end = time.time() + timeout
        while time.time() < end:
            self.wake()  # the screen times out quickly; a dark screen has no nodes
            ns = self.nodes()
            have = {t for n in ns for t in (n["label"], n.get("text", ""), n.get("content-desc", ""))}
            if all(lbl in have for lbl in labels):
                return ns
            time.sleep(1)
        self.fail(f"expected {what} (labels {labels}) but did not see it")

    def tap(self, x: int, y: int, why: str) -> None:
        print(f"  tap {why} at ({x},{y})", flush=True)
        self.adb("shell", "input", "tap", str(x), str(y))
        time.sleep(1.5)

    def tap_node(self, n: dict | None, why: str) -> None:
        if n is None:
            self.fail(f"could not find {why}")
        self.tap(*n["center"], why)

    def type_text(self, text: str) -> None:
        # adb input text: spaces become %s; quote for the device shell
        self.adb("shell", "input", "text", shlex.quote(text.replace(" ", "%s")))
        time.sleep(1)

    def wake(self) -> None:
        self.adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        self.adb("shell", "wm", "dismiss-keyguard")

    def push_video(self, video: Path) -> str:
        """Copy a video into the gallery so it shows first in pickers; returns its path on the phone."""
        remote = f"{GALLERY_DIR}/{time.strftime('%Y%m%d-%H%M%S')}-{video.name}"
        self.adb("shell", "mkdir", "-p", GALLERY_DIR)
        t = time.time()
        self.adb("push", str(video), remote, timeout=900)
        self.adb("shell", "touch", remote)
        self.adb("shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", f"file://{remote}")
        print(f"  copied {video.stat().st_size / 1e6:.1f} MB in {time.time() - t:.0f}s", flush=True)
        return remote

    def remove_video(self, remote: str) -> None:
        """Delete a pushed video and its media-index entry, so no stale tile stays in the gallery."""
        self.adb("shell", "rm", "-f", remote)
        self.adb("shell", "content", "delete", "--uri", "content://media/external/file", "--where", f"\"_data='{remote}'\"")

    def by_id(self, rid_suffix: str, nodes: list[dict] | None = None) -> dict | None:
        for n in nodes if nodes is not None else self.nodes():
            if n.get("resource-id", "").endswith(":id/" + rid_suffix):
                return n
        return None

    def fail(self, msg: str) -> None:
        SHOTS.mkdir(exist_ok=True)
        # tagged with the phone id (set by the engine) so each phone's runner finds only its own screenshots
        tag = os.environ.get("PHONE_ID", "")
        shot = SHOTS / f"fail-{time.strftime('%m%d-%H%M%S')}{'-' + tag if tag else ''}.png"
        try:
            self.d.screenshot(str(shot))
        except Exception:
            pass
        raise Abort(f"{msg} (screenshot: {shot})")
