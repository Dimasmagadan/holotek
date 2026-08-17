import json
import logging
import subprocess
import time
from collections import deque
from typing import NamedTuple, Optional

log = logging.getLogger("holotek.core")

CONFIG_PATH = "config.json"
SEVERITY = {"green": 0, "yellow": 1, "red": 2}

TREND_DEFAULTS = {
    "trend_window_seconds": 600,
    "trend_alert_ppm_per_min": 5.0,
    "trend_cooldown_seconds": 1800,
}


def load_config(path=CONFIG_PATH):
    with open(path) as f:
        cfg = json.load(f)
    validate(cfg)
    return cfg


def validate(cfg):
    t = cfg["thresholds"]
    for key in ("green_max", "yellow_max"):
        v = t[key]
        if not isinstance(v, int) or v < 0:
            raise ValueError(f"{key} must be a non-negative int")
    if t["green_max"] > t["yellow_max"]:
        raise ValueError("green_max must be <= yellow_max")
    v = cfg["poll_interval_seconds"]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise ValueError("poll_interval_seconds must be a number > 0")
    v = cfg["notification_cooldown_seconds"]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise ValueError("notification_cooldown_seconds must be a number >= 0")
    v = cfg["green_reentry_drop_ppm"]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise ValueError("green_reentry_drop_ppm must be a number >= 0")
    if "trend_window_seconds" in cfg:
        v = cfg["trend_window_seconds"]
        if not isinstance(v, int) or isinstance(v, bool) or v < 120:
            raise ValueError("trend_window_seconds must be an int >= 120")
    if "trend_alert_ppm_per_min" in cfg:
        v = cfg["trend_alert_ppm_per_min"]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            raise ValueError("trend_alert_ppm_per_min must be a positive number")
    if "trend_cooldown_seconds" in cfg:
        v = cfg["trend_cooldown_seconds"]
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ValueError("trend_cooldown_seconds must be an int >= 0")


def zone(ppm, t):
    if ppm <= t["green_max"]:
        return "green"
    if ppm <= t["yellow_max"]:
        return "yellow"
    return "red"


MESSAGES = {
    ("yellow", "green"): "CO2 back to normal",
    ("red", "yellow"): "CO2 improving",
    ("red", "green"): "CO2 back to normal",
    ("green", "yellow"): "CO2 rising",
    ("yellow", "red"): "CO2 HIGH",
    ("green", "red"): "CO2 HIGH",
}


def decide(state, ppm, now, cfg):
    """Return (title, body) to fire, or None to suppress. Updates state in place."""
    z = zone(ppm, cfg["thresholds"])
    cooldown = cfg["notification_cooldown_seconds"]
    drop = cfg["green_reentry_drop_ppm"]

    if state["last_zone"] is None:
        state["last_zone"] = z
        state["last_notified_ppm"] = ppm
        return None

    prev_zone, curr_zone = state["last_zone"], z
    last_not = state["last_notified_at"]
    within = last_not is not None and (now - last_not) < cooldown

    fire = False
    reset_baseline = True

    if curr_zone in ("yellow", "red"):
        if SEVERITY[curr_zone] > SEVERITY[prev_zone]:
            fire = True
        elif SEVERITY[curr_zone] == SEVERITY[prev_zone] and not within:
            fire = True
        elif SEVERITY[curr_zone] < SEVERITY[prev_zone] and not within:
            fire = True
            reset_baseline = False
    elif curr_zone == "green" and prev_zone in ("yellow", "red"):
        big = (state["last_notified_ppm"] - ppm) >= drop
        if big or not within:
            fire = True

    if not fire:
        if SEVERITY[curr_zone] >= SEVERITY[prev_zone]:
            state["last_zone"] = z
        return None

    title = MESSAGES.get((prev_zone, z), f"CO2 {z.upper()}")
    state["last_zone"] = z
    state["last_notified_at"] = now
    if reset_baseline:
        state["last_notified_ppm"] = ppm
    return title, f"{ppm} ppm"


_VID = 0x04d9
_PID = 0xa052
_MAGIC_FEATURE_REPORT = [0x00, 0, 0, 0, 0, 0, 0, 0, 0]


def _valid_packet(raw):
    """True if `raw` is a well-formed plaintext zyTemp packet (rev 2.00)."""
    if len(raw) < 8:
        return False
    if raw[4] != 0x0D or raw[5] != 0 or raw[6] != 0 or raw[7] != 0:
        return False
    op, val_hi, val_lo, chk = raw[0], raw[1], raw[2], raw[3]
    return (op + val_hi + val_lo) & 0xFF == chk


def _drain_hid(h, first_timeout_ms=2000, rest_timeout_ms=200):
    """Read packets until the HID buffer is empty, return (last_ppm, last_temp).

    The first read uses a longer timeout in case the buffer is empty and we
    must wait for the next packet. Every valid packet OVERWRITES the previous
    value, so the result is the freshest reading in the buffer — not the
    stalest, which is what FIFO reads used to return."""
    ppm = temp_c = None
    raw = h.read(8, timeout_ms=first_timeout_ms)
    while raw:
        if _valid_packet(raw):
            op, val_hi, val_lo = raw[0], raw[1], raw[2]
            val = (val_hi << 8) | val_lo
            if op == 0x50:
                ppm = val
            elif op == 0x42:
                temp_c = val * 0.0625 - 273.15
        raw = h.read(8, timeout_ms=rest_timeout_ms)
    return ppm, temp_c


def read_sensors(mon, retries=3):
    """Return (co2_ppm, temp_c) from the freshest packets in the HID buffer.

    Uses the persistent handle on `mon`. On HID errors, invalidates the
    handle so the caller's reconnect logic can re-open it on the next
    tick. Each call drains the WHOLE buffer — every valid packet
    overwrites the previous, so the result is the freshest reading, not
    the stale head-of-queue packet that used to leak through."""
    for _ in range(retries):
        try:
            ppm, temp_c = mon.drain()
        except Exception as e:
            log.warning("drain failed: %s", e)
            mon.invalidate()
            continue
        if ppm is not None:
            return ppm, temp_c
        # Buffer drained empty. The device streams ~1 CO2 packet per 1.5s;
        # wait briefly so the next drain has something to read instead of
        # burning all retries in a tight loop.
        time.sleep(0.5)
    return None, None


def send_notification(title, body):
    """Fire a macOS notification via osascript. Fixed 2-arg signature."""
    esc = lambda s: str(s).replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{esc(body)}" with title "{esc(title)}"'
    result = subprocess.run(["osascript", "-e", script], check=False)
    if result.returncode != 0:
        log.warning("osascript notification failed with exit code %d", result.returncode)


def detect_trend(state, ppm, now, cfg):
    """Return 'rising', 'falling', or None based on recent ppm history. Updates state.

    Always returns 'rising'/'falling' for the caller's UI marker, regardless of
    cooldown. Only updates trend_last_notified_at (and thus whether poll_step
    treats this as a fresh, notification-worthy rise) when outside the cooldown
    window. Falling trends never touch the cooldown — a fast drop is already
    covered by the green-reentry notification.
    """
    window = cfg.get("trend_window_seconds", TREND_DEFAULTS["trend_window_seconds"])
    threshold = cfg.get("trend_alert_ppm_per_min", TREND_DEFAULTS["trend_alert_ppm_per_min"])
    cooldown = cfg.get("trend_cooldown_seconds", TREND_DEFAULTS["trend_cooldown_seconds"])

    hist = state.setdefault("trend_history", deque())
    hist.append((now, ppm))
    while hist and (now - hist[0][0]) > window:
        hist.popleft()

    if len(hist) < 2:
        return None
    t0, p0 = hist[0]
    elapsed = (now - t0) / 60.0
    if elapsed < 1.0:
        return None
    rate = (ppm - p0) / elapsed

    if rate >= threshold:
        last = state.get("trend_last_notified_at")
        if last is None or (now - last) >= cooldown:
            state["trend_last_notified_at"] = now
        return "rising"
    if rate <= -threshold:
        return "falling"
    return None


def _backoff_sleep(attempt, cap=60, base=10):
    delay = min(base * (2 ** attempt), cap)
    time.sleep(delay)


class _DeviceHandle:
    """Holds a PERSISTENT HID handle to the zyTemp device.

    The handle must stay open between polls: closing it lets macOS stop
    streaming from the device, and re-activating the stream with the magic
    feature-report costs ~2s of latency on the next read. With a persistent
    handle, drain takes ~100ms and always returns fresh packets.
    """

    def __init__(self, path):
        self._info = {"path": path}
        self._h = None

    def _ensure_open(self):
        if self._h is None:
            import hid
            h = hid.device()
            h.open_path(self._info["path"])
            # The magic feature-report starts the device streaming on rev
            # 2.00. AGENTS.md claimed it disrupts streaming — that's wrong:
            # without it, no packets ever arrive after a fresh open.
            h.send_feature_report(_MAGIC_FEATURE_REPORT)
            self._h = h

    def drain(self, first_timeout_ms=2000, rest_timeout_ms=200):
        """Read packets until the buffer is empty. Returns (last_ppm, last_temp)."""
        self._ensure_open()
        return _drain_hid(self._h, first_timeout_ms, rest_timeout_ms)

    def invalidate(self):
        """Drop the current handle — caller will reconnect or retry."""
        if self._h is not None:
            try:
                self._h.close()
            except Exception:
                pass
            self._h = None

    @property
    def is_alive(self):
        """Cheap liveness probe — just checks the handle is still open.

        A real IOError surfaces only on the next read; read_sensors handles
        that by invalidating and reconnecting on the next tick."""
        return self._h is not None


def _find_device_path():
    """Return the HID path of the first zyTemp CO2 device, or None."""
    import hid
    ifaces = hid.enumerate(_VID, _PID)
    return ifaces[0]["path"] if ifaces else None


def reconnect(cfg, attempts=10):
    """Find the CO2 device and open a persistent handle to it.

    Returns a _DeviceHandle on success, or None when `attempts` is finite
    and exhausted. `attempts=None` retries forever (used for the blocking
    startup connect)."""
    attempt = 0
    while attempts is None or attempt < attempts:
        path = _find_device_path()
        if path is not None:
            handle = _DeviceHandle(path)
            try:
                handle._ensure_open()
            except Exception as e:
                log.warning("initial stream activation failed: %s", e)
            return handle
        log.error("reconnect failed: no CO2 device found")
        _backoff_sleep(attempt)
        attempt += 1
    return None


class TickResult(NamedTuple):
    ppm: Optional[int]
    temp_c: Optional[float]
    zone: Optional[str]
    notifications: list
    trend: Optional[str] = None


def poll_step(mon, state, cfg, now=None):
    """One poll tick: read sensors, run decide() and detect_trend().

    Never sleeps and never touches the UI — callers own delivery and
    timing. `ppm` is None (with an empty notifications list) on a
    failed read.
    """
    if now is None:
        now = time.time()
    ppm, temp_c = read_sensors(mon)
    if ppm is None:
        return TickResult(None, None, None, [])
    z = zone(ppm, cfg["thresholds"])
    notifications = []
    out = decide(state, ppm, now, cfg)
    if out:
        notifications.append(out)
    prev_notified_at = state.get("trend_last_notified_at")
    trend = detect_trend(state, ppm, now, cfg)
    if trend == "rising" and state.get("trend_last_notified_at") != prev_notified_at:
        notifications.append(("CO₂ rising fast", f"{ppm} ppm"))
    return TickResult(ppm, temp_c, z, notifications, trend)
