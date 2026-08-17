import copy
import time
from collections import deque
import pytest
from unittest.mock import patch, MagicMock
from core import (
    decide, zone, validate, load_config, CONFIG_PATH, MESSAGES, read_sensors,
    detect_trend, poll_step, reconnect, TickResult, _drain_hid, _DeviceHandle,
    send_notification,
)


DEFAULTS = {
    "thresholds": {"green_max": 800, "yellow_max": 1200},
    "poll_interval_seconds": 120,
    "notification_cooldown_seconds": 1800,
    "green_reentry_drop_ppm": 200,
}

COOLDOWN_PAST = 9999  # large enough to always be past cooldown
COOLDOWN_WITHIN = 0  # zero → now - last_notified_at >= 0, which is < cooldown when cooldown > 0


def mkstate(**kw):
    s = {"last_zone": None, "last_notified_at": None, "last_notified_ppm": None}
    s.update(kw)
    return s


def mkconfig():
    """Deep copy of DEFAULTS — a shallow dict(DEFAULTS) shares the nested
    'thresholds' dict, so mutating v['thresholds'][...] would corrupt the
    shared global for every test that runs afterward."""
    return copy.deepcopy(DEFAULTS)


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
        assert s["last_zone"] == "red"  # held back so "improving" can fire later

    def test_suppressed_improving_then_fires_improving_after_cooldown(self):
        s = mkstate(last_zone="red", last_notified_ppm=1500, last_notified_at=0)
        out1 = decide(s, 1000, COOLDOWN_WITHIN, DEFAULTS)
        assert out1 is None
        assert s["last_zone"] == "red"
        out2 = decide(s, 1000, COOLDOWN_PAST, DEFAULTS)
        assert out2 is not None
        assert out2[0] == "CO2 improving"
        assert s["last_notified_ppm"] == 1500

    def test_suppressed_improving_then_red_refires_high_semantics(self):
        s = mkstate(last_zone="red", last_notified_ppm=1500, last_notified_at=0)
        out1 = decide(s, 1000, COOLDOWN_WITHIN, DEFAULTS)
        assert out1 is None
        assert s["last_zone"] == "red"
        out2 = decide(s, 1600, COOLDOWN_PAST, DEFAULTS)
        assert out2 is not None
        assert out2[0] == "CO2 RED"

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
        v = mkconfig()
        v["thresholds"]["green_max"] = 800
        v["thresholds"]["yellow_max"] = 800
        validate(v)

    def test_green_max_greater_than_yellow_max_rejected(self):
        v = mkconfig()
        v["thresholds"]["green_max"] = 1000
        v["thresholds"]["yellow_max"] = 800
        with pytest.raises(ValueError):
            validate(v)

    def test_negative_thresholds_rejected(self):
        v = mkconfig()
        v["thresholds"]["green_max"] = -1
        with pytest.raises(ValueError):
            validate(v)

    def test_non_int_thresholds_rejected(self):
        v = mkconfig()
        v["thresholds"]["green_max"] = "800"
        with pytest.raises(ValueError):
            validate(v)

    def test_poll_interval_zero_rejected(self):
        v = mkconfig()
        v["poll_interval_seconds"] = 0
        with pytest.raises(ValueError):
            validate(v)

    def test_poll_interval_negative_rejected(self):
        v = mkconfig()
        v["poll_interval_seconds"] = -10
        with pytest.raises(ValueError):
            validate(v)

    def test_notification_cooldown_negative_rejected(self):
        v = mkconfig()
        v["notification_cooldown_seconds"] = -1
        with pytest.raises(ValueError):
            validate(v)

    def test_green_reentry_drop_negative_rejected(self):
        v = mkconfig()
        v["green_reentry_drop_ppm"] = -1
        with pytest.raises(ValueError):
            validate(v)

    def test_trend_keys_absent_is_valid(self):
        validate(DEFAULTS)  # DEFAULTS has no trend_* keys

    def test_trend_window_below_minimum_rejected(self):
        v = mkconfig()
        v["trend_window_seconds"] = 60
        with pytest.raises(ValueError):
            validate(v)

    def test_trend_rate_zero_rejected(self):
        v = mkconfig()
        v["trend_alert_ppm_per_min"] = 0
        with pytest.raises(ValueError):
            validate(v)

    def test_trend_rate_non_numeric_rejected(self):
        v = mkconfig()
        v["trend_alert_ppm_per_min"] = "5.0"
        with pytest.raises(ValueError):
            validate(v)

    def test_trend_cooldown_negative_rejected(self):
        v = mkconfig()
        v["trend_cooldown_seconds"] = -1
        with pytest.raises(ValueError):
            validate(v)

    @pytest.mark.parametrize("key", [
        "poll_interval_seconds", "notification_cooldown_seconds", "green_reentry_drop_ppm",
    ])
    def test_bool_numeric_fields_rejected(self, key):
        """bool is an int subclass \u2014 True/False must not silently pass as 1/0."""
        v = mkconfig()
        v[key] = True
        with pytest.raises(ValueError):
            validate(v)

    @pytest.mark.parametrize("key", [
        "poll_interval_seconds", "notification_cooldown_seconds", "green_reentry_drop_ppm",
    ])
    def test_string_numeric_fields_rejected_with_valueerror(self, key):
        v = mkconfig()
        v[key] = "120"
        with pytest.raises(ValueError):
            validate(v)

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


def _make_handle_mon(packets, path=b"/dev/fake"):
    """Real _DeviceHandle with a mocked HID handle pre-loaded with `packets`.

    Bypasses `_ensure_open` (which would `send_feature_report` on the mock),
    so the test controls the buffer exactly. read_sensors uses mon.drain()
    which delegates to _drain_hid on the mock handle."""
    mon = _DeviceHandle(path)
    h = MagicMock()
    h.read.side_effect = packets + [[]]
    mon._h = h
    return mon


# ── _drain_hid() ────────────────────────────────────────────────────────────

class TestDrainHid:
    """Drives the buffer-draining primitive directly. The whole fix lives
    here: every valid packet overwrites the previous so we return the
    FRESHEST reading, not the stale head-of-queue one."""

    def _h(self, packets):
        h = MagicMock()
        h.read.side_effect = packets + [[]]
        return h

    def test_returns_last_co2_when_multiple_in_buffer(self):
        packets = [_make_packet(0x50, 700), _make_packet(0x50, 750), _make_packet(0x50, 800)]
        ppm, _ = _drain_hid(self._h(packets))
        assert ppm == 800

    def test_returns_last_co2_and_last_temp_interleaved(self):
        packets = [
            _make_packet(0x50, 700),
            _make_packet(0x42, 4722),  # 22.01 °C
            _make_packet(0x50, 800),
            _make_packet(0x42, 4739),  # 22.11 °C
        ]
        ppm, temp = _drain_hid(self._h(packets))
        assert ppm == 800
        assert temp == pytest.approx(4739 * 0.0625 - 273.15, abs=0.01)

    def test_bad_end_marker_skipped(self):
        bad = [0x50, 0x02, 0xEE, 0x40, 0xFF, 0, 0, 0]  # end != 0x0D
        good = _make_packet(0x50, 750)
        ppm, _ = _drain_hid(self._h([bad, good]))
        assert ppm == 750

    def test_bad_checksum_skipped(self):
        bad = _make_packet(0x50, 750)
        bad[3] = 0x00  # corrupt checksum
        good = _make_packet(0x50, 900)
        ppm, _ = _drain_hid(self._h([bad, good]))
        assert ppm == 900

    def test_empty_buffer_returns_none_none(self):
        h = MagicMock()
        h.read.return_value = []
        ppm, temp = _drain_hid(h)
        assert ppm is None
        assert temp is None

    def test_first_read_uses_first_timeout_then_rest(self):
        """Verify timeouts: first read uses first_timeout_ms, the rest use
        rest_timeout_ms. This is what lets the buffer drain quickly without
        hanging on an empty queue."""
        h = MagicMock()
        h.read.side_effect = [_make_packet(0x50, 750), []]
        _drain_hid(h, first_timeout_ms=2000, rest_timeout_ms=200)
        assert h.read.call_args_list[0].kwargs["timeout_ms"] == 2000
        assert h.read.call_args_list[1].kwargs["timeout_ms"] == 200


# ── read_sensors() ─────────────────────────────────────────────────────────

class TestReadSensors:
    def test_returns_co2_ppm(self):
        mon = _make_handle_mon([_make_packet(0x50, 750)])
        ppm, temp = read_sensors(mon)
        assert ppm == 750
        assert temp is None

    def test_returns_both_co2_and_temp(self):
        # val=4722 → 4722 * 0.0625 - 273.15 = 22.0125
        packets = [_make_packet(0x50, 750), _make_packet(0x42, 4722)]
        mon = _make_handle_mon(packets)
        ppm, temp = read_sensors(mon)
        assert ppm == 750
        assert temp == pytest.approx(4722 * 0.0625 - 273.15, abs=0.01)

    def test_temp_conversion_formula(self):
        packets = [_make_packet(0x50, 800), _make_packet(0x42, 4739)]
        mon = _make_handle_mon(packets)
        _, temp = read_sensors(mon)
        assert temp == pytest.approx(4739 * 0.0625 - 273.15, abs=0.01)

    def test_returns_last_co2_when_multiple_in_buffer(self):
        """Stale packets at the head of the HID buffer must not leak into
        the reading — the FRESHEST CO2 wins."""
        packets = [_make_packet(0x50, 700), _make_packet(0x50, 750), _make_packet(0x50, 800)]
        mon = _make_handle_mon(packets)
        ppm, _ = read_sensors(mon)
        assert ppm == 800

    def test_drain_exception_invalidates_and_retries(self, monkeypatch):
        """If drain raises, read_sensors must invalidate the handle and
        retry — the next tick's reconnect flow can re-open it."""
        monkeypatch.setattr("core.time.sleep", lambda _: None)
        mon = _DeviceHandle(b"/dev/fake")
        mon.drain = MagicMock(side_effect=[OSError("io error"), (750, None)])
        mon.invalidate = MagicMock()
        ppm, temp = read_sensors(mon, retries=2)
        assert ppm == 750
        assert mon.drain.call_count == 2
        mon.invalidate.assert_called_once()

    def test_all_drains_empty_returns_none(self, monkeypatch):
        monkeypatch.setattr("core.time.sleep", lambda _: None)
        mon = _DeviceHandle(b"/dev/fake")
        mon.drain = MagicMock(return_value=(None, None))
        mon.invalidate = MagicMock()
        ppm, temp = read_sensors(mon, retries=3)
        assert ppm is None
        assert temp is None
        assert mon.drain.call_count == 3

    def test_first_drain_empty_second_succeeds(self, monkeypatch):
        monkeypatch.setattr("core.time.sleep", lambda _: None)
        mon = _DeviceHandle(b"/dev/fake")
        mon.drain = MagicMock(side_effect=[(None, None), (900, None)])
        mon.invalidate = MagicMock()
        ppm, temp = read_sensors(mon, retries=3)
        assert ppm == 900


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

    def test_falling_returns_falling(self):
        s = mktrend()
        t0 = 0.0
        detect_trend(s, 900, t0, TREND_CFG)
        # 10 minutes later, -100 ppm → 10 ppm/min drop > 5.0 threshold
        result = detect_trend(s, 800, t0 + 600.0, TREND_CFG)
        assert result == "falling"

    def test_falling_does_not_consume_cooldown(self):
        s = mktrend()
        t0 = 0.0
        detect_trend(s, 900, t0, TREND_CFG)
        # Fast fall: returns "falling" but must not set trend_last_notified_at
        result1 = detect_trend(s, 800, t0 + 120.0, TREND_CFG)
        assert result1 == "falling"
        assert s.get("trend_last_notified_at") is None
        # Fast rise shortly after: must still fire (cooldown untouched)
        result2 = detect_trend(s, 1000, t0 + 240.0, TREND_CFG)
        assert result2 == "rising"

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


# ── poll_step() ──────────────────────────────────────────────────────────

class TestPollStep:
    def test_good_read_returns_ppm_temp_zone(self):
        packets = [_make_packet(0x50, 750), _make_packet(0x42, 4722)]
        mon = _make_handle_mon(packets)
        s = mkstate()
        result = poll_step(mon, s, DEFAULTS, now=0.0)
        assert result.ppm == 750
        assert result.temp_c == pytest.approx(4722 * 0.0625 - 273.15, abs=0.01)
        assert result.zone == "green"
        assert result.notifications == []  # first sample never notifies

    def test_read_failure_returns_empty_tick(self, monkeypatch):
        # drain raises → read_sensors returns (None, None) → empty tick
        monkeypatch.setattr("core.time.sleep", lambda _: None)
        mon = _DeviceHandle(b"/dev/fake")
        mon.drain = MagicMock(side_effect=OSError("no device"))
        mon.invalidate = MagicMock()
        s = mkstate()
        result = poll_step(mon, s, DEFAULTS, now=0.0)
        assert result == TickResult(None, None, None, [])

    def test_escalation_produces_decide_notification(self):
        mon = _make_handle_mon([_make_packet(0x50, 900)])
        s = mkstate(last_zone="green", last_notified_ppm=500, last_notified_at=0)
        result = poll_step(mon, s, DEFAULTS, now=1.0)
        assert result.notifications == [("CO2 rising", "900 ppm")]

    def test_escalation_plus_fast_rise_produces_two_notifications_in_order(self):
        mon = _make_handle_mon([_make_packet(0x50, 900)])
        s = mkstate(last_zone="green", last_notified_ppm=500, last_notified_at=0)
        s["trend_history"] = deque([(0.0, 500)])
        result = poll_step(mon, s, TREND_CFG, now=600.0)
        assert result.notifications == [
            ("CO2 rising", "900 ppm"),
            ("CO₂ rising fast", "900 ppm"),
        ]
        assert result.trend == "rising"

    def test_sustained_rise_does_not_spam_notification_within_cooldown(self):
        # Simulates real poll_step usage: consecutive polls, rate stays above
        # threshold the whole time. Only the first poll should notify; later
        # polls must still report trend="rising" for the UI marker without
        # re-firing the notification until trend_cooldown_seconds elapses.
        s = mkstate(last_zone="green", last_notified_ppm=500, last_notified_at=0)
        s["trend_history"] = deque([(0.0, 700)])
        readings = [760, 820, 880]
        fired = []
        for i, ppm in enumerate(readings):
            now = (i + 1) * 120.0
            mon = _make_handle_mon([_make_packet(0x50, ppm)])
            result = poll_step(mon, s, TREND_CFG, now=now)
            assert result.trend == "rising"
            fired.append(("CO₂ rising fast", f"{ppm} ppm") in result.notifications)
        assert fired == [True, False, False]


# ── send_notification() ────────────────────────────────────────────────

class TestSendNotification:
    def test_logs_warning_on_nonzero_exit(self, caplog):
        fake_result = MagicMock(returncode=1)
        with patch("core.subprocess.run", return_value=fake_result):
            with caplog.at_level("WARNING", logger="holotek.core"):
                send_notification("title", "body")
        assert any("osascript" in r.message for r in caplog.records)

    def test_no_warning_on_success(self, caplog):
        fake_result = MagicMock(returncode=0)
        with patch("core.subprocess.run", return_value=fake_result):
            with caplog.at_level("WARNING", logger="holotek.core"):
                send_notification("title", "body")
        assert caplog.records == []


# ── reconnect() / _DeviceHandle ──────────────────────────────────────────

class TestReconnect:
    def test_gives_up_after_attempts_returns_none(self, monkeypatch):
        import core
        monkeypatch.setattr(core, "_backoff_sleep", lambda *a, **k: None)
        with patch("hid.enumerate", return_value=[]):
            result = reconnect(DEFAULTS, attempts=3)
        assert result is None

    def test_returns_device_handle_when_found(self):
        iface = {"path": b"/dev/fake"}
        fake_h = MagicMock()
        with patch("hid.enumerate", return_value=[iface]), \
             patch("hid.device", MagicMock(return_value=fake_h)):
            handle = reconnect(DEFAULTS, attempts=1)
        assert handle is not None
        assert handle._info["path"] == b"/dev/fake"

    def test_reconnect_sends_magic_feature_report(self):
        """Stream activation on rev 2.00 needs the magic feature-report —
        without it no packets arrive. reconnect must send it once on open."""
        iface = {"path": b"/dev/fake"}
        fake_h = MagicMock()
        with patch("hid.enumerate", return_value=[iface]), \
             patch("hid.device", MagicMock(return_value=fake_h)):
            reconnect(DEFAULTS, attempts=1)
        fake_h.send_feature_report.assert_called_once_with([0x00, 0, 0, 0, 0, 0, 0, 0, 0])

    def test_is_alive_true_when_handle_open(self):
        handle = _DeviceHandle(b"/dev/fake")
        handle._h = MagicMock()  # simulate open
        assert handle.is_alive is True

    def test_is_alive_false_when_handle_not_open(self):
        handle = _DeviceHandle(b"/dev/fake")
        assert handle.is_alive is False

    def test_invalidate_clears_handle(self):
        handle = _DeviceHandle(b"/dev/fake")
        h = MagicMock()
        handle._h = h
        handle.invalidate()
        h.close.assert_called_once()
        assert handle._h is None
        assert handle.is_alive is False

    def test_drain_uses_persistent_handle(self):
        """drain() must reuse the existing handle, not open a new one each
        call — that's the whole reason _DeviceHandle exists."""
        handle = _DeviceHandle(b"/dev/fake")
        h = MagicMock()
        h.read.side_effect = [_make_packet(0x50, 750), []]
        handle._h = h
        ppm, _ = handle.drain()
        assert ppm == 750
        # No new hid.device() should have been created
        assert handle._h is h
