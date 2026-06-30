import logging
import threading
import time

import rumps

from core import load_config, zone, decide, read_ppm, marker_for

log = logging.getLogger("holotek.menubar")


def _backoff_sleep(attempt, cap=60, base=10):
    delay = min(base * (2 ** attempt), cap)
    time.sleep(delay)


class HolotekApp(rumps.App):
    def __init__(self, config_path="config.json"):
        super().__init__("\u25CF", quit_button=None)
        self.config_path = config_path
        self.cfg = load_config(self.config_path)
        self.mon = None
        self.state = {"last_zone": None, "last_notified_at": None, "last_notified_ppm": None}
        self._latest_ppm = None
        self._latest_zone = None
        self._latest_time = None
        self._pending_notify = None

        self.info_item = rumps.MenuItem("starting\u2026")
        self.time_item = rumps.MenuItem("")
        self.menu = [
            self.info_item,
            self.time_item,
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]

        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _reconnect(self):
        from co2meter import CO2monitor
        attempt = 0
        while attempt < 10:
            try:
                self.mon = CO2monitor(bypass_decrypt=self.cfg.get("bypass_decrypt", False))
                return True
            except Exception as e:
                log.error("reconnect failed: %s", e)
                _backoff_sleep(attempt)
                attempt += 1
        return False

    def _poll_loop(self):
        while True:
            try:
                self.cfg = load_config(self.config_path)
            except Exception as e:
                log.warning("config reload failed: %s", e)

            if self.mon is None or not self.mon.is_alive:
                log.warning("device gone; reconnecting")
                if not self._reconnect():
                    time.sleep(self.cfg.get("poll_interval_seconds", 120))
                    continue

            ppm = read_ppm(self.mon)
            if ppm is None:
                log.warning("no CO2 reading this tick")
            else:
                z = zone(ppm, self.cfg["thresholds"])
                self._latest_ppm = ppm
                self._latest_zone = z
                self._latest_time = time.strftime("%H:%M:%S")
                now = time.time()
                out = decide(self.state, ppm, now, self.cfg)
                log.info("CO2=%s ppm zone=%s notify=%s", ppm, z, bool(out))
                if out:
                    self._pending_notify = out

            time.sleep(self.cfg.get("poll_interval_seconds", 120))

    @rumps.timer(1)
    def _update_ui(self, _sender):
        if self._latest_zone:
            self.title = marker_for(self._latest_zone)
        if self._latest_ppm is not None:
            z = self._latest_zone or ""
            self.info_item.title = f"CO\u2082: {self._latest_ppm} ppm ({z})"
            self.time_item.title = f"as of {self._latest_time}"
        if self._pending_notify:
            title, body = self._pending_notify
            self._pending_notify = None
            rumps.notification(title=title, subtitle=body, message="", sound=True)
