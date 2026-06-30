import time
import pytest
from core import decide, zone, validate, load_config, CONFIG_PATH, MESSAGES, marker_for


DEFAULTS = {
    "thresholds": {"green_max": 800, "yellow_max": 1200},
    "poll_interval_seconds": 120,
    "notification_cooldown_seconds": 1800,
    "green_reentry_drop_ppm": 200,
    "bypass_decrypt": False,
}

COOLDOWN_PAST = 9999  # large enough to always be past cooldown
COOLDOWN_WITHIN = 0  # zero → now - last_notified_at >= 0, which is < cooldown when cooldown > 0


def mkstate(**kw):
    s = {"last_zone": None, "last_notified_at": None, "last_notified_ppm": None}
    s.update(kw)
    return s


# ── zone() ──────────────────────────────────────────────────────────────────

class TestZone:
    def test_green(self):
        assert zone(0, DEFAULTS["thresholds"]) == "green"
        assert zone(800, DEFAULTS["thresholds"]) == "green"

    def test_yellow(self):
        assert zone(801, DEFAULTS["thresholds"]) == "yellow"
        assert zone(1200, DEFAULTS["thresholds"]) == "yellow"

    def test_red(self):
        assert zone(1201, DEFAULTS["thresholds"]) == "red"


# ── decide() ────────────────────────────────────────────────────────────────

class TestDecide:
    def now(self):
        return 10_000_000.0

    def test_first_sample_no_notify(self):
        s = mkstate()
        out = decide(s, 700, self.now(), DEFAULTS)
        assert out is None
        assert s["last_zone"] == "green"
        assert s["last_notified_ppm"] == 700
        assert s["last_notified_at"] is None

    def test_cold_start_yellow_notifies_on_second_tick(self):
        s = mkstate(last_zone="yellow", last_notified_ppm=900, last_notified_at=None)
        now = self.now()
        out = decide(s, 900, now, DEFAULTS)
        assert out is not None

    def test_escalation_green_to_yellow_fires_immediately(self):
        s = mkstate(last_zone="green", last_notified_ppm=500, last_notified_at=0)
        out = decide(s, 900, 1, DEFAULTS)
        assert out is not None
        assert out[0] == "CO2 rising"

    def test_escalation_yellow_to_red_fires_immediately(self):
        s = mkstate(last_zone="yellow", last_notified_ppm=900, last_notified_at=0)
        out = decide(s, 1300, 1, DEFAULTS)
        assert out is not None
        assert out[0] == "CO2 HIGH"

    def test_escalation_green_to_red_fires_immediately(self):
        s = mkstate(last_zone="green", last_notified_ppm=500, last_notified_at=0)
        out = decide(s, 1300, 1, DEFAULTS)
        assert out is not None
        assert out[0] == "CO2 HIGH"

    def test_same_zone_yellow_within_cooldown_suppressed(self):
        s = mkstate(last_zone="yellow", last_notified_ppm=900, last_notified_at=0)
        out = decide(s, 950, COOLDOWN_WITHIN, DEFAULTS)
        assert out is None

    def test_same_zone_yellow_past_cooldown_refires(self):
        s = mkstate(last_zone="yellow", last_notified_ppm=900, last_notified_at=0)
        out = decide(s, 950, COOLDOWN_PAST, DEFAULTS)
        assert out is not None
        assert out[0] == "CO2 YELLOW"

    def test_red_to_yellow_improving_within_cooldown_suppressed(self):
        s = mkstate(last_zone="red", last_notified_ppm=1500, last_notified_at=0)
        out = decide(s, 1000, COOLDOWN_WITHIN, DEFAULTS)
        assert out is None

    def test_red_to_yellow_improving_past_cooldown_fires(self):
        s = mkstate(last_zone="red", last_notified_ppm=1500, last_notified_at=0)
        out = decide(s, 1000, COOLDOWN_PAST, DEFAULTS)
        assert out is not None
        assert out[0] == "CO2 improving"
        assert s["last_notified_ppm"] == 1500  # baseline preserved

    def test_red_to_green_big_drop_bypasses_cooldown(self):
        s = mkstate(last_zone="red", last_notified_ppm=1500, last_notified_at=0)
        out = decide(s, 700, COOLDOWN_WITHIN, DEFAULTS)
        assert out is not None
        assert out[0] == "CO2 back to normal"

    def test_yellow_to_green_big_drop_bypasses_cooldown(self):
        s = mkstate(last_zone="yellow", last_notified_ppm=900, last_notified_at=0)
        out = decide(s, 500, COOLDOWN_WITHIN, DEFAULTS)
        assert out is not None
        assert out[0] == "CO2 back to normal"

    def test_yellow_to_green_small_drop_within_cooldown_suppressed(self):
        s = mkstate(last_zone="yellow", last_notified_ppm=900, last_notified_at=0)
        out = decide(s, 750, COOLDOWN_WITHIN, DEFAULTS)
        assert out is None
        assert s["last_zone"] == "yellow"

    def test_yellow_to_green_small_drop_past_cooldown_fires(self):
        s = mkstate(last_zone="yellow", last_notified_ppm=900, last_notified_at=0)
        out = decide(s, 750, COOLDOWN_PAST, DEFAULTS)
        assert out is not None
        assert out[0] == "CO2 back to normal"
        assert s["last_zone"] == "green"

    def test_suppressed_green_then_refires_after_cooldown(self):
        s = mkstate(last_zone="yellow", last_notified_ppm=900, last_notified_at=0)
        now = 0
        # Tick 1: small-drop green re-entry suppressed (within cooldown)
        out = decide(s, 750, now, DEFAULTS)
        assert out is None
        assert s["last_zone"] == "yellow"
        # Tick 2: still within cooldown, still suppressed
        out = decide(s, 750, now + 100, DEFAULTS)
        assert out is None
        assert s["last_zone"] == "yellow"
        # Tick 3: past cooldown, fires "back to normal"
        out = decide(s, 750, COOLDOWN_PAST, DEFAULTS)
        assert out is not None
        assert out[0] == "CO2 back to normal"
        assert s["last_zone"] == "green"
        assert s["last_notified_ppm"] == 750

    def test_green_to_green_nothing(self):
        s = mkstate(last_zone="green", last_notified_ppm=500, last_notified_at=0)
        out = decide(s, 600, 1, DEFAULTS)
        assert out is None

    def test_baseline_preserved_across_red_to_yellow_for_green_check(self):
        s = mkstate(last_zone="red", last_notified_ppm=1500, last_notified_at=0)
        now = COOLDOWN_PAST
        out = decide(s, 1000, now, DEFAULTS)
        assert out is not None
        assert out[0] == "CO2 improving"
        assert s["last_notified_ppm"] == 1500  # baseline not reset
        assert s["last_zone"] == "yellow"
        last_not = s["last_notified_at"]

        out2 = decide(s, 700, now + 1, DEFAULTS)
        assert out2 is not None
        assert out2[0] == "CO2 back to normal"

    def test_green_back_to_normal_message(self):
        s = mkstate(last_zone="yellow", last_notified_ppm=900, last_notified_at=0)
        out = decide(s, 700, COOLDOWN_PAST, DEFAULTS)
        assert out[0] == "CO2 back to normal"

    @pytest.mark.parametrize("prev,curr,expected", [
        ("yellow", "green", "CO2 back to normal"),
        ("red", "yellow", "CO2 improving"),
        ("red", "green", "CO2 back to normal"),
        ("green", "yellow", "CO2 rising"),
        ("yellow", "red", "CO2 HIGH"),
        ("green", "red", "CO2 HIGH"),
    ])
    def test_all_message_titles(self, prev, curr, expected):
        assert MESSAGES[(prev, curr)] == expected

    def test_red_repeat_within_cooldown_suppressed(self):
        s = mkstate(last_zone="red", last_notified_ppm=1300, last_notified_at=0)
        out = decide(s, 1500, COOLDOWN_WITHIN, DEFAULTS)
        assert out is None

    def test_red_repeat_past_cooldown_refires(self):
        s = mkstate(last_zone="red", last_notified_ppm=1300, last_notified_at=0)
        out = decide(s, 1500, COOLDOWN_PAST, DEFAULTS)
        assert out is not None
        assert out[0] == "CO2 RED"


# ── validate() ──────────────────────────────────────────────────────────────

class TestValidate:
    def test_valid_config(self):
        validate(DEFAULTS)

    def test_green_max_equal_yellow_max_allowed(self):
        v = dict(DEFAULTS)
        v["thresholds"]["green_max"] = 800
        v["thresholds"]["yellow_max"] = 800
        validate(v)

    def test_green_max_greater_than_yellow_max_rejected(self):
        v = dict(DEFAULTS)
        v["thresholds"]["green_max"] = 1000
        v["thresholds"]["yellow_max"] = 800
        with pytest.raises(ValueError):
            validate(v)

    def test_negative_thresholds_rejected(self):
        v = dict(DEFAULTS)
        v["thresholds"]["green_max"] = -1
        with pytest.raises(ValueError):
            validate(v)

    def test_non_int_thresholds_rejected(self):
        v = dict(DEFAULTS)
        v["thresholds"]["green_max"] = "800"
        with pytest.raises(ValueError):
            validate(v)

    def test_bypass_decrypt_must_be_bool(self):
        v = dict(DEFAULTS)
        v["bypass_decrypt"] = "yes"
        with pytest.raises(ValueError):
            validate(v)

    def test_poll_interval_zero_rejected(self):
        v = dict(DEFAULTS)
        v["poll_interval_seconds"] = 0
        with pytest.raises(ValueError):
            validate(v)

    def test_poll_interval_negative_rejected(self):
        v = dict(DEFAULTS)
        v["poll_interval_seconds"] = -10
        with pytest.raises(ValueError):
            validate(v)

    def test_notification_cooldown_negative_rejected(self):
        v = dict(DEFAULTS)
        v["notification_cooldown_seconds"] = -1
        with pytest.raises(ValueError):
            validate(v)

    def test_green_reentry_drop_negative_rejected(self):
        v = dict(DEFAULTS)
        v["green_reentry_drop_ppm"] = -1
        with pytest.raises(ValueError):
            validate(v)


# ── marker_for() ─────────────────────────────────────────────────────────────

class TestMarker:
    def test_green(self):
        assert marker_for("green") == "\u26AA"

    def test_yellow(self):
        assert marker_for("yellow") == "\U0001F7E1"

    def test_red(self):
        assert marker_for("red") == "\U0001F534"

    def test_unknown_fallback(self):
        assert marker_for("bogus") == "\u26AA"
