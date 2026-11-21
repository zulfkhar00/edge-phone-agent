"""Fixtures for the multi-phone tests: a throwaway engine (own config, own engine.db, own folders) per test.
No real phone is touched: adb is replaced by tests/fake_adb.py. Account handles in fixtures are made up."""

from __future__ import annotations

import importlib.util
import itertools
import sys
from pathlib import Path

import pytest

from fake_adb import FakeAdb

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))  # fake_adb

BASE = """
[dashboard]
host = "127.0.0.1"
port = 8080
resume_after_min = 10

[mailbox]
inbox = "mailbox/inbox"

[schedule]
stats_offsets_min = [60, 180]
tiktok_detailed_from_min = 1440
research_margin_min = 4
retry_after_min = 5
max_attempts = 2

[preflight]
at = "00:00"

[accounts.instagram.alpha]
use = "noswipe"
posts_per_day = 4

[accounts.tiktok.alpha]
use = "noswipe"
posts_per_day = 4

[accounts.instagram.beta]
use = "noswipe"
posts_per_day = 4
likes_per_day = 20
research_minutes_per_day = 60

[accounts.tiktok.gamma]
use = "practice"
posts_per_day = 3
"""

TWO_PHONES = BASE + """
[[phones]]
id = "left"
label = "Left phone"
hw_serial = "HW-LEFT"
wifi_serial = "10.0.0.11:5555"
accounts = ["ig:@alpha", "tt:@alpha"]

[[phones]]
id = "right"
label = "Right phone"
hw_serial = "HW-RIGHT"
usb_serial = "USB-RIGHT"
accounts = ["ig:@beta", "tt:@gamma"]
quotas = { posts_per_day = 1 }
"""

OLD_CONFIG = BASE + """
[phone]
serial = "10.0.0.11:5555"
hw_serial = "HW-LEFT"
"""

_n = itertools.count()


@pytest.fixture
def adb() -> FakeAdb:
    return FakeAdb()


@pytest.fixture
def make_engine(tmp_path, monkeypatch):
    """make_engine(config_text) -> a freshly imported engine module working in tmp_path."""

    def build(config: str = TWO_PHONES):
        cfg = tmp_path / "config.toml"
        cfg.write_text(config)
        monkeypatch.setenv("ENGINE_CONFIG", str(cfg))
        spec = importlib.util.spec_from_file_location(f"engine_under_test_{next(_n)}", REPO / "engine.py")
        eng = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = eng
        spec.loader.exec_module(eng)
        eng.ROOT = tmp_path
        eng.DB = tmp_path / "engine.db"
        eng.LOGS = tmp_path / "logs"
        eng.INBOX = tmp_path / "mailbox" / "inbox"
        for d in (eng.LOGS, eng.INBOX, tmp_path / "runs"):
            d.mkdir(parents=True, exist_ok=True)
        eng.db()
        return eng

    return build
