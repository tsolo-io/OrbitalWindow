# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbitalwindow.config import Settings
from orbitalwindow.ui import common

UI_DIR = Path(common.__file__).parent


@pytest.fixture
def zone(service, monkeypatch):
    """Register a service whose time zone the test sets."""
    monkeypatch.setattr(common, "_service", service)

    def use(name):
        """Switch the service to time zone ``name``."""
        monkeypatch.setattr(service, "settings", Settings(root=service.settings.root, tz=name))

    return use


def test_stored_utc_is_shown_in_the_server_time_zone(zone):
    """Naive values are UTC; they are shown in TZ, with seconds when asked."""
    stored = datetime(2026, 10, 11, 6, 30, 15)
    zone("Africa/Johannesburg")
    assert common.local_time(stored) == "2026-10-11 08:30"
    assert common.local_time(stored, "%Y-%m-%d %H:%M:%S") == "2026-10-11 08:30:15"
    assert common.local_time(datetime(2026, 10, 11, 6, 30, tzinfo=UTC)) == "2026-10-11 08:30"
    zone("Etc/UTC")
    assert common.local_time(stored) == "2026-10-11 06:30"


def test_missing_values_and_unknown_zones(zone):
    """None shows the placeholder; an unknown zone falls back to UTC."""
    zone("Africa/Johannesburg")
    assert common.local_time(None) == "" and common.local_time(None, empty="–") == "–"
    zone("Mars/Olympus")
    assert common.local_time(datetime(2026, 1, 1, 12, 0)) == "2026-01-01 12:00"


def test_pages_format_times_only_through_local_time():
    """Formatting a timestamp directly on a page would show UTC."""
    pattern = re.compile(r"\.strftime\(|:%[YHd]")
    offenders = [
        f"{path.name}:{number}"
        for path in sorted(UI_DIR.glob("*.py"))
        if path.name != "common.py"
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []
