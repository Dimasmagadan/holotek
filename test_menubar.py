import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from core import TickResult
from menubar import HolotekApp, _format_age


DEFAULT_CFG = {
    "thresholds": {"green_max": 800, "yellow_max": 1200},
    "poll_interval_seconds": 120,
    "notification_cooldown_seconds": 1800,
    "green_reentry_drop_ppm": 200,
}


def _make_app(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(DEFAULT_CFG))
    return HolotekApp(config_path=str(config_path))


# ── startup config failure (M3) ──────────────────────────────────────────

class TestStartupConfig:
    def test_missing_config_raises_systemexit(self, tmp_path):
        with pytest.raises(SystemExit):
            HolotekApp(config_path=str(tmp_path / "does-not-exist.json"))

    def test_invalid_config_raises_systemexit(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({**DEFAULT_CFG, "poll_interval_seconds": -1}))
        with pytest.raises(SystemExit):
            HolotekApp(config_path=str(config_path))


# ── refresh/poll serialization (H2) ──────────────────────────────────────

class TestRefreshSerialization:
    def test_do_refresh_calls_do_not_overlap(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path)
        app.mon = MagicMock(is_alive=True)

        active = 0
        max_active = 0
        guard = threading.Lock()

        def fake_poll_step(mon, state, cfg):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with guard:
                active -= 1
            return TickResult(700, None, "green", [])

        monkeypatch.setattr("menubar.poll_step", fake_poll_step)

        threads = [threading.Thread(target=app._do_refresh) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert max_active == 1


# ── _format_age (L3) ──────────────────────────────────────────────────────

class TestFormatAge:
    def test_seconds(self):
        assert _format_age(5) == "5s ago"

    def test_minutes(self):
        assert _format_age(125) == "2m ago"

    def test_hours(self):
        assert _format_age(7300) == "2h ago"

    def test_negative_clamped_to_zero(self):
        assert _format_age(-5) == "0s ago"
