import logging
import threading
import time

import AppKit
from Foundation import NSObject, NSTimer, NSRunLoop, NSDefaultRunLoopMode, NSBundle

from core import load_config, zone, decide, read_sensors, detect_trend

log = logging.getLogger("holotek.menubar")


def _backoff_sleep(attempt, cap=60, base=10):
    delay = min(base * (2 ** attempt), cap)
    time.sleep(delay)


MARKERS = {
    "green": "\U0001F7E2",
    "yellow": "\U0001F7E1",
    "red": "\U0001F534",
}


class _AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):
        self._app_ref.on_launched()

    def tick_(self, timer):
        self._app_ref._update_ui(timer)

    def quit_(self, sender):
        self._app_ref._quit(sender)


class HolotekApp:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        self.cfg = load_config(self.config_path)
        self.mon = None
        self.state = {"last_zone": None, "last_notified_at": None, "last_notified_ppm": None}
        self._latest_ppm = None
        self._latest_temp = None
        self._latest_zone = None
        self._latest_time = None
        self._pending_notify = []
        self._status_item = None
        self._delegate = None
        self._un_center = None

    def on_launched(self):
        # Set bundle identifier so UNUserNotificationCenter can deliver notifications
        _bi = NSBundle.mainBundle().infoDictionary()
        if _bi is not None and not _bi.get("CFBundleIdentifier"):
            _bi["CFBundleIdentifier"] = "com.holotek.menubar"

        try:
            import UserNotifications as UN
            center = UN.UNUserNotificationCenter.currentNotificationCenter()
            center.requestAuthorizationWithOptions_completionHandler_(
                UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound,
                lambda granted, err: log.info("notification auth granted=%s", granted),
            )
            self._un_center = center
        except Exception as e:
            log.warning("UNUserNotificationCenter unavailable: %s", e)

        status_bar = AppKit.NSStatusBar.systemStatusBar()
        self._status_item = status_bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        btn = self._status_item.button()
        btn.setTitle_(MARKERS["green"])

        menu = AppKit.NSMenu.alloc().init()

        self._info_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "starting…", None, ""
        )
        self._temp_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "", None, ""
        )
        self._time_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "", None, ""
        )
        quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", b"quit:", ""
        )
        quit_item.setTarget_(self._delegate)

        menu.addItem_(self._info_item)
        menu.addItem_(self._temp_item)
        menu.addItem_(self._time_item)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        menu.addItem_(quit_item)

        self._status_item.setMenu_(menu)

        threading.Thread(target=self._poll_loop, daemon=True).start()

        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self._delegate, b"tick:", None, True
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(self._timer, NSDefaultRunLoopMode)

    def _quit(self, sender):
        AppKit.NSApplication.sharedApplication().terminate_(None)

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

            ppm, temp_c = read_sensors(self.mon)
            if ppm is None:
                log.warning("no CO2 reading this tick")
            else:
                z = zone(ppm, self.cfg["thresholds"])
                self._latest_ppm = ppm
                self._latest_temp = temp_c
                self._latest_zone = z
                self._latest_time = time.strftime("%H:%M:%S")
                now = time.time()
                out = decide(self.state, ppm, now, self.cfg)
                log.info("CO2=%s ppm zone=%s notify=%s", ppm, z, bool(out))
                if out:
                    self._pending_notify.append(out)
                trend = detect_trend(self.state, ppm, now, self.cfg)
                if trend == "rising":
                    self._pending_notify.append(("CO₂ rising fast", f"{ppm} ppm"))

            time.sleep(self.cfg.get("poll_interval_seconds", 120))

    def _deliver_notification(self, title, body):
        if self._un_center is not None:
            try:
                import UserNotifications as UN
                content = UN.UNMutableNotificationContent.alloc().init()
                content.setTitle_(title)
                content.setBody_(body)
                req = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
                    f"holotek-{time.time()}", content, None
                )
                self._un_center.addNotificationRequest_withCompletionHandler_(
                    req, lambda err: None
                )
                return
            except Exception as e:
                log.warning("UNUserNotificationCenter delivery failed: %s", e)
        note = AppKit.NSUserNotification.alloc().init()
        note.setTitle_(title)
        note.setInformativeText_(body)
        AppKit.NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(note)

    def _update_ui(self, timer):
        z = self._latest_zone or "green"
        self._status_item.button().setTitle_(MARKERS.get(z, MARKERS["green"]))
        if self._latest_ppm is not None:
            self._info_item.setTitle_(f"CO₂: {self._latest_ppm} ppm ({self._latest_zone or ''})")
            if self._latest_temp is not None:
                self._temp_item.setTitle_(f"Temp: {self._latest_temp:.1f}°C")
            self._time_item.setTitle_(f"as of {self._latest_time}")
        while self._pending_notify:
            title, body = self._pending_notify.pop(0)
            self._deliver_notification(title, body)

    def run(self):
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

        delegate = _AppDelegate.alloc().init()
        delegate._app_ref = self
        self._delegate = delegate
        app.setDelegate_(delegate)

        app.run()
