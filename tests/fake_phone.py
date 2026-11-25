"""A stand-in for phone.Phone that models an app as a small screen graph, so a flow (social.py) can run end to end in
a test: the flow reads nodes, taps by label and types; the fake moves between screens exactly like a real app would.

    screens = {"home": [N("Home", clickable=True), N("New post", rid="composer_write", go="compose")], "compose": [...]}
    phone = FakePhone(screens, start="home")

N(label, rid=, go=<screen name or callable(phone)>, text=, cls=, desc=True) describes a node. `go` runs when it is tapped.
The real Phone methods find / wait / tap_node / by_id / fail run unchanged (they only need nodes(), tap() and d).
"""

from __future__ import annotations

from pathlib import Path

from phone import Phone


def N(label: str = "", rid: str = "", go=None, clickable: bool = False, text: str = "", cls: str = "android.view.View",
      box=None, enabled: bool = True) -> dict:
    return {"label": label, "rid": rid, "go": go, "clickable": clickable or go is not None, "text": text, "cls": cls,
            "box": box, "enabled": enabled}


class FakeDevice:
    def __init__(self, phone: "FakePhone"):
        self.phone = phone
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)
        nxt = self.phone.on_back.get(self.phone.screen)
        if nxt is not None:
            self.phone.go(nxt)

    def app_start(self, pkg: str, stop: bool = False) -> None:
        self.phone.started.append(pkg)
        self.phone.go(self.phone.start)

    def app_current(self) -> dict:
        return {"package": self.phone.started[-1] if self.phone.started else ""}

    def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"png")

    def clear_text(self) -> None:
        self.phone.state["text"] = ""

    def window_size(self):
        return (720, 1612)


class FakePhone(Phone):
    def __init__(self, screens: dict, start: str, on_back: dict | None = None):
        self.serial = "FAKE"
        self.screens, self.start, self.screen = screens, start, start
        self.on_back = on_back or {}
        self.d = FakeDevice(self)
        self.h = 1612
        self.state: dict = {"text": ""}
        self.taps: list[str] = []      # the `why` of every tap, in order
        self.typed: list[str] = []
        self.adb_calls: list[tuple] = []
        self.started: list[str] = []

    # -- model
    def go(self, screen: str) -> None:
        self.screen = screen

    def nodes(self) -> list[dict]:
        out = []
        for i, spec in enumerate(self.screens[self.screen]):
            spec = spec(self) if callable(spec) else spec
            if spec is None:
                continue
            b = spec["box"] or [0, 100 + i * 100, 700, 180 + i * 100]
            text = self.state["text"] if spec["cls"] == "android.widget.EditText" else spec["text"]
            out.append({"label": spec["label"] or text, "text": text, "content-desc": spec["label"] if not spec["text"] else "",
                        "resource-id": f"app:id/{spec['rid']}" if spec["rid"] else "", "class": spec["cls"],
                        "clickable": "true" if spec["clickable"] else "false", "enabled": "true" if spec["enabled"] else "false",
                        "box": b, "center": ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2), "_spec": spec})
        return out

    def tap(self, x: int, y: int, why: str) -> None:
        print(f"  tap {why} at ({x},{y})", flush=True)  # same text as phone.Phone.tap: the engine greps it
        self.taps.append(why)
        for n in self.nodes():
            b = n["box"]
            if b[0] <= x <= b[2] and b[1] <= y <= b[3] and n["_spec"]["go"] is not None:
                go = n["_spec"]["go"]
                go(self) if callable(go) else self.go(go)
                return

    def type_text(self, text: str) -> None:
        self.typed.append(text)
        self.state["text"] = text

    def wake(self) -> None:
        pass

    def adb(self, *args: str, timeout: int = 300) -> str:
        self.adb_calls.append(args)
        return ""
