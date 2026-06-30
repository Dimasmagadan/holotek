import json
import subprocess
import time
from collections import deque

CONFIG_PATH = "config.json"
SEVERITY = {"green": 0, "yellow": 1, "red": 2}


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
    if cfg["poll_interval_seconds"] <= 0:
        raise ValueError("poll_interval_seconds must be > 0")
    if cfg["notification_cooldown_seconds"] < 0:
        raise ValueError("notification_cooldown_seconds must be >= 0")
    if cfg["green_reentry_drop_ppm"] < 0:
        raise ValueError("green_reentry_drop_ppm must be >= 0")
    v = cfg.get("bypass_decrypt", False)
    if not isinstance(v, bool):
        raise ValueError("bypass_decrypt must be a boolean")


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
        if not (curr_zone == "green" and prev_zone in ("yellow", "red")):
            state["last_zone"] = z
        return None

    title = MESSAGES.get((prev_zone, z), f"CO2 {z.upper()}")
    state["last_zone"] = z
    state["last_notified_at"] = now
    if reset_baseline:
        state["last_notified_ppm"] = ppm
    return title, f"{ppm} ppm"


def read_sensors(mon, retries=3):
    """Return (co2_ppm, temp_c) from direct HID read. Either may be None."""
    import hid
    for _ in range(retries):
        try:
            h = hid.device()
            h.open_path(mon._info["path"])
        except Exception:
            return None, None
        try:
            ppm = temp_c = None
            for _ in range(20):
                raw = h.read(8, timeout_ms=2000)
                if not raw:
                    break
                op, val_hi, val_lo, chk, end = raw[0], raw[1], raw[2], raw[3], raw[4]
                if end != 0x0D or raw[5] != 0 or raw[6] != 0 or raw[7] != 0:
                    continue
                if (op + val_hi + val_lo) & 0xFF != chk:
                    continue
                val = (val_hi << 8) | val_lo
                if op == 0x50:
                    ppm = val
                elif op == 0x42:
                    temp_c = val * 0.0625 - 273.15
                if ppm is not None and temp_c is not None:
                    break
            if ppm is not None:
                return ppm, temp_c
        finally:
            h.close()
    return None, None


def read_ppm(mon, retries=3):
    return read_sensors(mon, retries)[0]


MARKERS = {"green": "\u25CF", "yellow": "\U0001F7E1", "red": "\U0001F534"}


def marker_for(zone_name):
    return MARKERS.get(zone_name, "\u25CF")


def send_notification(title, body):
    """Fire a macOS notification via osascript. Fixed 2-arg signature."""
    esc = lambda s: str(s).replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{esc(body)}" with title "{esc(title)}"'
    subprocess.run(["osascript", "-e", script], check=False)


def detect_trend(state, ppm, now, cfg):
    """Return 'rising' | 'falling' | None based on recent ppm history. Updates state."""
    window = cfg.get("trend_window_seconds", 600)
    threshold = cfg.get("trend_alert_ppm_per_min", 5.0)
    cooldown = cfg.get("trend_cooldown_seconds", 1800)

    hist = state.setdefault("trend_history", deque())
    hist.append((now, ppm))
    while hist and (now - hist[0][0]) > window:
        hist.popleft()

    last = state.get("trend_last_notified_at")
    if last is not None and (now - last) < cooldown:
        return None
    if len(hist) < 2:
        return None
    t0, p0 = hist[0]
    elapsed = (now - t0) / 60.0
    if elapsed < 1.0:
        return None
    rate = (ppm - p0) / elapsed
    if abs(rate) >= threshold:
        state["trend_last_notified_at"] = now
        return "rising" if rate > 0 else "falling"
    return None
