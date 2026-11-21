"""Phones: which Android phones this box drives, which accounts live on which phone, and how to reach each one.

config.toml describes them as an array of tables:

    [[phones]]
    id = "phone-1"                    # short name used in the dashboard, logs and the jobs table
    label = "Moto G, left"            # shown in the dashboard (optional)
    hw_serial = "ZD222RWWGT"          # the phone's own serial (adb shell getprop ro.serialno): required
    usb_serial = "ZD222RWWGT"         # optional: its serial on the cable, when that differs from hw_serial
    wifi_serial = "192.168.1.150:5555" # optional: Wi-Fi adb address, the fallback (adb_keeper.sh keeps it current)
    accounts = ["ig:@tinyloveshows", "tt:@nomoreawkwardness"]   # each account lives on exactly one phone
    quotas = { posts_per_day = 2 }    # optional: overrides the [accounts.*] limits of this phone's accounts

An old config with only [phone] keeps working: it is one phone with id "phone-1" that owns every account.

Also a tiny CLI for adb_keeper.sh (plain bash cannot read the [[phones]] array):
    python3 phones.py keeper-list [config.toml]       one line per phone: id|usb|hw|wifi
    python3 phones.py wifi <id> [config.toml]         the current Wi-Fi address of one phone
    python3 phones.py set-wifi <id> <ip:5555> [config.toml]
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

LEGACY_ID = "phone-1"
APPS = {"ig": "instagram", "instagram": "instagram", "tt": "tiktok", "tiktok": "tiktok"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


class ConfigError(ValueError):
    """The phone configuration is unusable: the engine refuses to start rather than guess which phone owns what."""


@dataclass
class PhoneCfg:
    id: str
    label: str
    hw_serial: str = ""
    usb_serial: str = ""
    wifi_serial: str = ""
    accounts: frozenset[tuple[str, str]] | None = None  # (app, handle lower-cased); None = owns every account (legacy)
    quotas: dict = field(default_factory=dict)
    index: int = 0

    @property
    def usb(self) -> str:
        """The serial over the cable: preferred transport."""
        return self.usb_serial or self.hw_serial

    def owns(self, app: str, account: str) -> bool:
        return self.accounts is None or (app, account.lstrip("@").casefold()) in self.accounts


def parse_account(spec: str) -> tuple[str, str]:
    """'ig:@name' / 'tt:name' / 'instagram:@name' -> ('instagram', 'name')."""
    app, sep, handle = str(spec).partition(":")
    handle = handle.strip().lstrip("@")
    if not sep or app.strip().lower() not in APPS or not handle:
        raise ConfigError(f"bad account {spec!r}: expected \"ig:@handle\" or \"tt:@handle\"")
    return APPS[app.strip().lower()], handle


def load_phones(cfg: dict) -> list[PhoneCfg]:
    """Validate and normalise the phone section(s) of a parsed config. Raises ConfigError."""
    multi, legacy = cfg.get("phones"), cfg.get("phone")
    if multi is not None and legacy is not None:
        raise ConfigError("config.toml has both [phone] and [[phones]]: move the old [phone] into a [[phones]] entry and delete it")
    if multi is None:
        if not isinstance(legacy, dict):
            raise ConfigError("config.toml needs a [phone] section or one or more [[phones]] entries")
        return [PhoneCfg(id=LEGACY_ID, label=legacy.get("label") or "Phone", hw_serial=legacy.get("hw_serial", ""),
                         usb_serial=legacy.get("usb_serial", ""), wifi_serial=legacy.get("serial", ""), accounts=None,
                         index=0)]
    if not isinstance(multi, list) or not multi:
        raise ConfigError("[[phones]] must have at least one entry")
    phones: list[PhoneCfg] = []
    seen_id: dict[str, int] = {}
    seen_serial: dict[str, str] = {}
    owner: dict[tuple[str, str], str] = {}
    for i, p in enumerate(multi):
        pid = str(p.get("id", "")).strip()
        if not ID_RE.match(pid):
            raise ConfigError(f"[[phones]] #{i + 1}: id {pid!r} is missing or not a short name (letters, digits, - _)")
        if pid in seen_id:
            raise ConfigError(f"[[phones]]: id {pid!r} is used twice")
        seen_id[pid] = i
        hw = str(p.get("hw_serial", "")).strip()
        if not hw:
            raise ConfigError(f"phone {pid}: hw_serial is required (adb shell getprop ro.serialno)")
        usb, wifi = str(p.get("usb_serial", "")).strip(), str(p.get("wifi_serial", "")).strip()
        for kind, s in (("hw_serial", hw), ("usb_serial", usb), ("wifi_serial", wifi)):
            if not s:
                continue
            if s in seen_serial and seen_serial[s] != pid:
                raise ConfigError(f"phone {pid}: {kind} {s!r} is already used by phone {seen_serial[s]}")
            seen_serial[s] = pid
        accts = set()
        for spec in p.get("accounts", []):
            app, handle = parse_account(spec)
            key = (app, handle.casefold())
            if key in owner and owner[key] != pid:
                raise ConfigError(f"account {spec!r} is on two phones ({owner[key]} and {pid}): an account lives on one phone only")
            owner[key] = pid
            accts.add(key)
        quotas = p.get("quotas") or {}
        if not isinstance(quotas, dict):
            raise ConfigError(f"phone {pid}: quotas must be a table, e.g. quotas = {{ posts_per_day = 2 }}")
        phones.append(PhoneCfg(id=pid, label=str(p.get("label") or pid), hw_serial=hw, usb_serial=usb, wifi_serial=wifi,
                               accounts=frozenset(accts), quotas=dict(quotas), index=i))
    return phones


def owner_of(phones: list[PhoneCfg], app: str, account: str) -> PhoneCfg | None:
    """The phone that owns this account, or None when no phone does."""
    for p in phones:
        if p.owns(app, account):
            return p
    return None


def unowned_accounts(cfg: dict, phones: list[PhoneCfg]) -> list[tuple[str, str]]:
    """[accounts.*] entries no phone owns (jobs for them would never run)."""
    return [(app, name) for app, accts in cfg.get("accounts", {}).items() for name in accts
            if owner_of(phones, app, name) is None]


def apply_quotas(cfg: dict, phones: list[PhoneCfg]) -> None:
    """Fold each phone's quotas into the [accounts.*] limits of the accounts it owns (in place)."""
    for app, accts in cfg.get("accounts", {}).items():
        for name, lim in accts.items():
            p = owner_of(phones, app, name)
            if p and p.quotas:
                lim.update(p.quotas)


# ---------------------------------------------------------------- transport discovery
Adb = Callable[..., Awaitable[str]]


async def discover(phone: PhoneCfg, adb: Adb, wifi: str | None = None) -> tuple[str | None, str]:
    """Find the adb transport of one phone: the USB cable first, Wi-Fi as the fallback.
    Returns (serial, "") or (None, why). A Wi-Fi address (or any serial that is not hw_serial) is only accepted when
    the device behind it reports hw_serial: with several phones on one network a re-used IP must never make us
    drive the wrong phone (and so the wrong accounts)."""
    wifi = wifi if wifi is not None else phone.wifi_serial
    candidates = list(dict.fromkeys(s for s in (phone.usb, wifi) if s))
    if not candidates:
        return None, "no serial configured"
    why: list[str] = []
    for s in candidates:
        ok = await adb("-s", s, "get-state") == "device"
        if not ok and ":" in s:
            await adb("connect", s)
            ok = await adb("-s", s, "get-state") == "device"
        if not ok:
            why.append(f"{s}: not answering")
            continue
        if phone.hw_serial and s != phone.hw_serial:
            got = (await adb("-s", s, "shell", "getprop", "ro.serialno")).strip()
            if got != phone.hw_serial:
                why.append(f"{s}: is a different phone ({got or 'unknown'}, expected {phone.hw_serial})")
                if ":" in s:
                    await adb("disconnect", s)
                continue
        return s, ""
    return None, "; ".join(why)


# ---------------------------------------------------------------- config.toml text editing (keeps comments)
def _blocks(text: str) -> list[tuple[str, int, int]]:
    """(header, start, end) of every table in a TOML text, by line offsets."""
    lines = text.splitlines(keepends=True)
    out, pos, cur = [], 0, None
    for ln in lines:
        m = re.match(r"\s*(\[\[?[^\]]+\]\]?)", ln)
        if m:
            if cur:
                out.append((cur[0], cur[1], pos))
            cur = (m.group(1), pos)
        pos += len(ln)
    if cur:
        out.append((cur[0], cur[1], pos))
    return out


def set_wifi_serial(text: str, phone_id: str, addr: str) -> str:
    """Return config text with the Wi-Fi address of one phone replaced (or added)."""
    legacy = tomllib.loads(text).get("phone") is not None and "phones" not in tomllib.loads(text)
    for header, a, b in _blocks(text):
        body = text[a:b]
        if legacy and header == "[phone]":
            key = "serial"
        elif not legacy and header == "[[phones]]" and re.search(rf'^\s*id\s*=\s*"{re.escape(phone_id)}"', body, re.M):
            key = "wifi_serial"
        else:
            continue
        line = f'{key} = "{addr}"'
        if re.search(rf"^{key}\s*=", body, re.M):
            body = re.sub(rf'^{key}\s*=\s*"[^"]*"', line, body, count=1, flags=re.M)
        else:
            nl = body.index("\n") + 1
            body = body[:nl] + line + "\n" + body[nl:]
        return text[:a] + body + text[b:]
    raise ConfigError(f"no phone {phone_id!r} in the config")


def _cli(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "keeper-list":
        path = Path(argv[1] if len(argv) > 1 else Path(__file__).parent / "config.toml")
        for p in load_phones(tomllib.loads(path.read_text())):
            print("|".join([p.id, p.usb, p.hw_serial, p.wifi_serial]))
        return 0
    if cmd == "wifi" and len(argv) >= 2:
        path = Path(argv[2] if len(argv) > 2 else Path(__file__).parent / "config.toml")
        print(next((p.wifi_serial for p in load_phones(tomllib.loads(path.read_text())) if p.id == argv[1]), ""))
        return 0
    if cmd == "set-wifi" and len(argv) >= 3:
        path = Path(argv[3] if len(argv) > 3 else Path(__file__).parent / "config.toml")
        path.write_text(set_wifi_serial(path.read_text(), argv[1], argv[2]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
