"""A stand-in for `adb`: the multi-phone code takes an async adb callable (Runner.adb / phones.discover), so tests
hand it this instead of running a real adb against real phones."""

from __future__ import annotations


class FakeAdb:
    """Phones are identified by their hardware serial. A phone is reachable over USB under its hw serial, and over
    Wi-Fi under an address you register with wifi_up()."""

    def __init__(self):
        self.usb: dict[str, list] = {}          # serial on the cable -> [hw serial, plugged in]
        self.wifi: dict[str, str] = {}          # "ip:5555" -> hw serial of the phone that answers there
        self.connected: set[str] = set()        # Wi-Fi addresses after `adb connect`
        self.calls: list[tuple] = []

    # -- test controls
    def plug(self, hw: str, usb_serial: str | None = None) -> None:
        """Plug the phone in; it shows up as usb_serial (default: its hw serial, like a real phone)."""
        self.usb[usb_serial or hw] = [hw, True]

    def unplug(self, hw: str) -> None:
        for v in self.usb.values():
            if v[0] == hw:
                v[1] = False

    def wifi_up(self, addr: str, hw: str) -> None:
        self.wifi[addr] = hw

    def wifi_down(self, addr: str) -> None:
        self.wifi.pop(addr, None)
        self.connected.discard(addr)

    def serial_of(self, s: str) -> str | None:
        """hw serial of the device reachable as `s`, or None."""
        if s in self.usb:
            return self.usb[s][0] if self.usb[s][1] else None
        if s in self.wifi and s in self.connected:
            return self.wifi[s]
        return None

    # -- adb itself
    async def __call__(self, *args: str, timeout: float = 15) -> str:
        self.calls.append(args)
        a = list(args)
        if a[:1] == ["connect"]:
            if a[1] in self.wifi:
                self.connected.add(a[1])
                return f"connected to {a[1]}"
            return f"failed to connect to {a[1]}"
        if a[:1] == ["disconnect"]:
            self.connected.discard(a[1])
            return ""
        if a[:1] == ["-s"]:
            hw = self.serial_of(a[1])
            if hw is None:
                return f"error: device '{a[1]}' not found"
            rest = a[2:]
            if rest == ["get-state"]:
                return "device"
            if rest == ["shell", "getprop", "ro.serialno"]:
                return hw
        return ""
