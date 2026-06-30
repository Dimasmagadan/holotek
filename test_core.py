import time
import pytest
from unittest.mock import patch, MagicMock
from core import decide, zone, validate, load_config, CONFIG_PATH, MESSAGES, marker_for, read_sensors, detect_trend


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
        assert marker_for("green") == "\u25CF"

    def test_yellow(self):
        assert marker_for("yellow") == "\U0001F7E1"

    def test_red(self):
        assert marker_for("red") == "\U0001F534"

    def test_unknown_fallback(self):
        assert marker_for("bogus") == "\u25CF"


# \u2500\u2500 read_sensors() \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

def _make_packet(op, val):
    val_hi = (val >> 8) & 0xFF
    val_lo = val & 0xFF
    chk = (op + val_hi + val_lo) & 0xFF
    return [op, val_hi, val_lo, chk, 0x0D, 0, 0, 0]


def _make_mon(path=b"/dev/fake"):
    mon = MagicMock()
    mon._info = {"path": path}
    return mon


class TestReadSensors:
    def _mock_hid(self, packets):
        h = MagicMock()
        h.read.side_effect = packets + [[]]
        dev_cls = MagicMock(return_value=h)
        return dev_cls, h

    def test_returns_co2_ppm(self):
        dev_cls, _ = self._mock_hid([_make_packet(0x50, 750)])
        with patch("hid.device", dev_cls):
            ppm, temp = read_sensors(_make_mon())
        assert ppm == 750
        assert temp is None

    def test_returns_both_co2_and_temp(self):
        # val=4722 \u2192 4722 * 0.0625 - 273.15 = 22.0125
        packets = [_make_packet(0x50, 750), _make_packet(0x42, 4722)]
        dev_cls, _ = self._mock_hid(packets)
        with patch("hid.device", dev_cls):
            ppm, temp = read_sensors(_make_mon())
        assert ppm == 750
        assert temp == pytest.approx(4722 * 0.0625 - 273.15, abs=0.01)

    def test_temp_conversion_formula(self):
        packets = [_make_packet(0x50, 800), _make_packet(0x42, 4739)]
        dev_cls, _ = self._mock_hid(packets)
        with patch("hid.device", dev_cls):
            _, temp = read_sensors(_make_mon())
        assert temp == pytest.approx(4739 * 0.0625 - 273.15, abs=0.01)

    def test_bad_end_marker_skipped(self):
        bad = [0x50, 0x02, 0xEE, 0x40, 0xFF, 0, 0, 0]  # end != 0x0D
        good = _make_packet(0x50, 750)
        dev_cls, _ = self._mock_hid([bad, good])
        with patch("hid.device", dev_cls):
            ppm, _ = read_sensors(_make_mon())
        assert ppm == 750

    def test_bad_checksum_skipped(self):
        bad = _make_packet(0x50, 750)
        bad[3] = 0x00  # corrupt checksum
        good = _make_packet(0x50, 900)
        dev_cls, _ = self._mock_hid([bad, good])
        with patch("hid.device", dev_cls):
            ppm, _ = read_sensors(_make_mon())
        assert ppm == 900

    def test_open_failure_returns_none_none(self):
        dev_cls = MagicMock()
        dev_cls.return_value.open_path.side_effect = OSError("no device")
        with patch("hid.device", dev_cls):
            ppm, temp = read_sensors(_make_mon(), retries=1)
        assert ppm is None
        assert temp is None

    def test_read_ppm_shim_returns_integer(self):
        from core import read_ppm
        dev_cls, _ = self._mock_hid([_make_packet(0x50, 650)])
        with patch("hid.device", dev_cls):
            result = read_ppm(_make_mon())
        assert result == 650


# \u2500\u2500 detect_trend() \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

TREND_CFG = {
    **DEFAULTS,
    "trend_window_seconds": 600,
    "trend_alert_ppm_per_min": 5.0,
    "trend_cooldown_seconds": 1800,
}


def mktrend():
    return {"last_zone": None, "last_notified_at": None, "last_notified_ppm": None}


class TestDetectTrend:
    def test_single_reading_returns_none(self):
        s = mktrend()
        assert detect_trend(s, 700, 0.0, TREND_CFG) is None

    def test_less_than_one_minute_elapsed_returns_none(self):
        s = mktrend()
        detect_trend(s, 700, 0.0, TREND_CFG)
        # 30s later, even with a big jump, elapsed < 1 min
        assert detect_trend(s, 800, 30.0, TREND_CFG) is None

    def test_rising_fires_when_rate_exceeds_threshold(self):
        s = mktrend()
        t0 = 0.0
        detect_trend(s, 700, t0, TREND_CFG)
        # 10 minutes later, +100 ppm \u2192 10 ppm/min > 5.0 threshold
        result = detect_trend(s, 800, t0 + 600.0, TREND_CFG)
        assert result == "rising"

    def test_falling_fires_when_rate_below_negative_threshold(self):
        s = mktrend()
        t0 = 0.0
        detect_trend(s, 900, t0, TREND_CFG)
        result = detect_trend(s, 800, t0 + 600.0, TREND_CFG)
        assert result == "falling"

    def test_stable_co2_returns_none(self):
        s = mktrend()
        t0 = 0.0
        detect_trend(s, 700, t0, TREND_CFG)
        # Only 1 ppm/min \u2014 below 5.0 threshold
        result = detect_trend(s, 710, t0 + 600.0, TREND_CFG)
        assert result is None

    def test_old_entries_pruned_outside_window(self):
        s = mktrend()
        # old reading at t=0
        detect_trend(s, 700, 0.0, TREND_CFG)
        # advance 601s past window \u2014 old entry pruned
        # new reading at t=601, second at t=601+30s: not enough elapsed (30s < 1min)
        detect_trend(s, 750, 601.0, TREND_CFG)
        result = detect_trend(s, 760, 631.0, TREND_CFG)
        assert result is None  # only 30s elapsed since t0 in pruned window

    def test_cooldown_suppresses_repeat_alert(self):
        s = mktrend()
        t0 = 0.0
        detect_trend(s, 700, t0, TREND_CFG)
        # First alert fires
        result1 = detect_trend(s, 800, t0 + 600.0, TREND_CFG)
        assert result1 == "rising"
        # Second call within cooldown (1800s) suppressed
        result2 = detect_trend(s, 900, t0 + 601.0, TREND_CFG)
        assert result2 is None

    def test_cooldown_expires_and_refires(self):
        s = mktrend()
        t0 = 0.0
        detect_trend(s, 700, t0, TREND_CFG)
        detect_trend(s, 800, t0 + 600.0, TREND_CFG)  # fires, sets trend_last_notified_at=600
        # Intermediate reading keeps history alive for the next window
        detect_trend(s, 850, t0 + 1800.0, TREND_CFG)  # within cooldown, suppressed
        # t=2400: cooldown elapsed (2400-600=1800, not < 1800); hist has [1800→2400] in window
        result = detect_trend(s, 1000, t0 + 2400.0, TREND_CFG)
        assert result == "rising"
