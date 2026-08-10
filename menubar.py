import logging
import threading
import time

import AppKit
from Foundation import NSObject, NSTimer, NSRunLoop, NSDefaultRunLoopMode, NSBundle

from core import load_config, reconnect, poll_step

log = logging.getLogger("holotek.menubar")


MARKERS = {
    "green": "\U0001F7E2",
    "yellow": "\U0001F7E1",
    "red": "\U0001F534",
    "green_up": "🟢↑",
    "green_down": "🟢↓",
    "yellow_up": "🟡↑",
    "yellow_down": "🟡↓",
    "red_up": "🔴↑",
    "red_down": "🔴↓",
}


class _AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):
        self._app_ref.on_launched()

    def tick_(self, timer):
        self._app_ref._update_ui(timer)

    def quit_(self, sender):
        self._app_ref._quit(sender)

    def refreshNow_(self, sender):
        self._app_ref._refresh_now(sender)


class HolotekApp:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        self.cfg = load_config(self.config_path)
        self.mon = None
        self.state = {"last_zone": None, "last_notified_at": None, "last_notified_ppm": None}
        self._latest = None  # (ppm, temp_c, zone, timestamp, trend), set atomically
        self._pending_notify = []
        self._status_item = None
        self._delegate = None
        self._un_center = None
        self._sensor_unavailable = False

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
        refresh_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Refresh Now", b"refreshNow:", ""
        )
        refresh_item.setTarget_(self._delegate)
        quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", b"quit:", ""
        )
        quit_item.setTarget_(self._delegate)

        menu.addItem_(self._info_item)
        menu.addItem_(self._temp_item)
        menu.addItem_(self._time_item)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        menu.addItem_(refresh_item)
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

    def _poll_loop(self):
        while True:
            try:
                self.cfg = load_config(self.config_path)
            except Exception as e:
                log.warning("config reload failed: %s", e)

            if self.mon is None or not self.mon.is_alive:
                log.warning("device gone; reconnecting")
                self.mon = reconnect(self.cfg)
                if self.mon is None:
                    time.sleep(self.cfg.get("poll_interval_seconds", 120))
                    continue

            result = poll_step(self.mon, self.state, self.cfg)
            if result.ppm is None:
                log.warning("no CO2 reading this tick")
                self._sensor_unavailable = True
            else:
                self._sensor_unavailable = False
                self._latest = (result.ppm, result.temp_c, result.zone, time.strftime("%H:%M:%S"), result.trend)
                log.info("CO2=%s ppm zone=%s trend=%s notify=%s", result.ppm, result.zone, result.trend, bool(result.notifications))
                self._pending_notify.extend(result.notifications)

            time.sleep(self.cfg.get("poll_interval_seconds", 120))

    def _refresh_now(self, sender):
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self):
        if self.mon is None or not self.mon.is_alive:
            self.mon = reconnect(self.cfg, attempts=1)
        if self.mon is None:
            self._sensor_unavailable = True
            return
        result = poll_step(self.mon, self.state, self.cfg)
        if result.ppm is None:
            self._sensor_unavailable = True
            return
        self._sensor_unavailable = False
        self._latest = (result.ppm, result.temp_c, result.zone, time.strftime("%H:%M:%S"), result.trend)
        self._pending_notify.extend(result.notifications)

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
        latest = self._latest
        z = latest[2] if latest else "green"
        trend = latest[4] if latest else None
        
        if trend == "rising":
            marker_key = f"{z}_up"
        elif trend == "falling":
            marker_key = f"{z}_down"
        else:
            marker_key = z
        
        self._status_item.button().setTitle_(MARKERS.get(marker_key, MARKERS["green"]))
        if self._sensor_unavailable:
            self._info_item.setTitle_("CO₂: sensor unavailable")
            self._temp_item.setTitle_("")
            self._time_item.setTitle_("")
        elif latest is not None:
            ppm, temp_c, zone_name, ts, _ = latest
            self._info_item.setTitle_(f"CO₂: {ppm} ppm ({zone_name or ''})")
            self._temp_item.setTitle_(f"Temp: {temp_c:.1f}°C" if temp_c is not None else "")
            self._time_item.setTitle_(f"as of {ts}")
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
