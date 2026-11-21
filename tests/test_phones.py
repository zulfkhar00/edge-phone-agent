"""Phone config: parsing, ownership, validation, transport discovery, keeper helpers. No phones, no adb."""

from __future__ import annotations

import tomllib

import pytest

import phones
from conftest import OLD_CONFIG, TWO_PHONES
from phones import ConfigError


def load(text: str):
    return phones.load_phones(tomllib.loads(text))


def test_old_single_phone_config_is_one_phone():
    [p] = load(OLD_CONFIG)
    assert p.id == "phone-1"
    assert p.hw_serial == "HW-LEFT" and p.wifi_serial == "10.0.0.11:5555"
    assert p.usb == "HW-LEFT"  # no usb_serial: the hw serial is the cable serial
    assert p.owns("instagram", "anything") and p.owns("tiktok", "else")  # owns every account


def test_phones_array_parses():
    left, right = load(TWO_PHONES)
    assert (left.id, left.label, left.usb, left.wifi_serial) == ("left", "Left phone", "HW-LEFT", "10.0.0.11:5555")
    assert right.usb == "USB-RIGHT" and right.hw_serial == "HW-RIGHT" and right.wifi_serial == ""
    assert left.owns("instagram", "alpha") and left.owns("tiktok", "alpha") and not left.owns("instagram", "beta")
    assert right.quotas == {"posts_per_day": 1}


def test_account_specs_accept_prefixes_with_or_without_at():
    assert phones.parse_account("ig:@X") == ("instagram", "X")
    assert phones.parse_account("tiktok:y") == ("tiktok", "y")
    for bad in ("alpha", "yt:@a", "ig:", "ig:@"):
        with pytest.raises(ConfigError):
            phones.parse_account(bad)


def test_two_phones_same_account_rejected():
    cfg = TWO_PHONES.replace('accounts = ["ig:@beta", "tt:@gamma"]', 'accounts = ["ig:@beta", "IG:@Alpha"]')
    with pytest.raises(ConfigError, match="two phones"):
        load(cfg)


def test_same_account_other_app_is_fine():
    load(TWO_PHONES.replace('accounts = ["ig:@beta", "tt:@gamma"]', 'accounts = ["ig:@beta", "tt:@alpha2"]'))


@pytest.mark.parametrize("edit, msg", [
    (lambda c: c.replace('id = "right"', 'id = "left"'), "used twice"),
    (lambda c: c.replace('hw_serial = "HW-RIGHT"\n', ""), "hw_serial is required"),
    (lambda c: c.replace('hw_serial = "HW-RIGHT"', 'hw_serial = "HW-LEFT"'), "already used"),
    (lambda c: c.replace('id = "right"', 'id = "bad id!"'), "not a short name"),
    (lambda c: c.replace("quotas = { posts_per_day = 1 }", "quotas = 3"), "quotas"),
])
def test_invalid_phone_entries_rejected(edit, msg):
    with pytest.raises(ConfigError, match=msg):
        load(edit(TWO_PHONES))


def test_both_phone_and_phones_rejected():
    with pytest.raises(ConfigError, match="both"):
        load(TWO_PHONES + '\n[phone]\nserial = "x"\n')


def test_no_phone_section_rejected():
    with pytest.raises(ConfigError):
        load("[dashboard]\nport = 1\n")


def test_ownership_unowned_and_quotas():
    cfg = tomllib.loads(TWO_PHONES.replace('accounts = ["ig:@beta", "tt:@gamma"]', 'accounts = ["ig:@beta"]'))
    ps = phones.load_phones(cfg)
    assert phones.owner_of(ps, "instagram", "beta").id == "right"
    assert phones.owner_of(ps, "tiktok", "gamma") is None
    assert phones.unowned_accounts(cfg, ps) == [("tiktok", "gamma")]
    phones.apply_quotas(cfg, ps)
    assert cfg["accounts"]["instagram"]["beta"]["posts_per_day"] == 1  # right's quota
    assert cfg["accounts"]["instagram"]["alpha"]["posts_per_day"] == 4  # left has none


# ---------------------------------------------------------------- transport discovery
async def test_usb_is_preferred_over_wifi(adb):
    [left, _] = load(TWO_PHONES)
    adb.plug("HW-LEFT")
    adb.wifi_up("10.0.0.11:5555", "HW-LEFT")
    assert await phones.discover(left, adb) == ("HW-LEFT", "")


async def test_wifi_fallback_when_cable_out(adb):
    [left, _] = load(TWO_PHONES)
    adb.unplug("HW-LEFT")
    adb.wifi_up("10.0.0.11:5555", "HW-LEFT")
    assert await phones.discover(left, adb) == ("10.0.0.11:5555", "")


async def test_usb_wifi_switch_both_ways(adb):
    [left, _] = load(TWO_PHONES)
    adb.wifi_up("10.0.0.11:5555", "HW-LEFT")
    adb.plug("HW-LEFT")
    assert (await phones.discover(left, adb))[0] == "HW-LEFT"
    adb.unplug("HW-LEFT")
    assert (await phones.discover(left, adb))[0] == "10.0.0.11:5555"
    adb.plug("HW-LEFT")
    assert (await phones.discover(left, adb))[0] == "HW-LEFT"
    adb.unplug("HW-LEFT")
    adb.wifi_down("10.0.0.11:5555")
    serial, why = await phones.discover(left, adb)
    assert serial is None and "not answering" in why


async def test_hw_serial_not_found(adb):
    [left, _] = load(TWO_PHONES)
    adb.plug("SOMEONE-ELSE")  # another phone is on the hub, ours is not
    serial, why = await phones.discover(left, adb)
    assert serial is None
    assert "HW-LEFT" in why


async def test_wifi_address_of_a_different_phone_is_refused(adb):
    """DHCP gave left's old address to the right phone: driving it would post to the wrong accounts."""
    [left, _] = load(TWO_PHONES)
    adb.wifi_up("10.0.0.11:5555", "HW-RIGHT")
    serial, why = await phones.discover(left, adb)
    assert serial is None and "different phone" in why and "HW-RIGHT" in why
    assert ("disconnect", "10.0.0.11:5555") in adb.calls


async def test_fresh_wifi_address_overrides_config(adb):
    [left, _] = load(TWO_PHONES)
    adb.wifi_up("10.0.0.99:5555", "HW-LEFT")  # the keeper moved it
    assert (await phones.discover(left, adb, wifi="10.0.0.99:5555"))[0] == "10.0.0.99:5555"


# ---------------------------------------------------------------- keeper helpers
def test_set_wifi_serial_in_phones_array_touches_only_that_phone():
    out = phones.set_wifi_serial(TWO_PHONES, "right", "10.0.0.22:5555")
    left, right = load(out)
    assert right.wifi_serial == "10.0.0.22:5555" and left.wifi_serial == "10.0.0.11:5555"
    out2 = phones.set_wifi_serial(out, "left", "10.0.0.33:5555")
    left, right = load(out2)
    assert left.wifi_serial == "10.0.0.33:5555" and right.wifi_serial == "10.0.0.22:5555"


def test_set_wifi_serial_legacy_section():
    out = phones.set_wifi_serial(OLD_CONFIG, "phone-1", "10.0.0.55:5555")
    assert load(out)[0].wifi_serial == "10.0.0.55:5555"
    assert out.count("serial = ") == 2  # serial + hw_serial: nothing duplicated


def test_set_wifi_serial_unknown_phone():
    with pytest.raises(ConfigError):
        phones.set_wifi_serial(TWO_PHONES, "nope", "1.2.3.4:5555")


def test_keeper_cli(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text(TWO_PHONES)
    assert phones._cli(["keeper-list", str(cfg)]) == 0
    assert capsys.readouterr().out.splitlines() == ["left|HW-LEFT|HW-LEFT|10.0.0.11:5555", "right|USB-RIGHT|HW-RIGHT|"]
    assert phones._cli(["wifi", "left", str(cfg)]) == 0
    assert capsys.readouterr().out.strip() == "10.0.0.11:5555"
    assert phones._cli(["set-wifi", "right", "10.0.0.7:5555", str(cfg)]) == 0
    assert load(cfg.read_text())[1].wifi_serial == "10.0.0.7:5555"
