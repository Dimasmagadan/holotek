import argparse
import fcntl
import logging
import os
import signal
import sys
import time

import co2meter

from core import load_config, decide, read_ppm, send_notification

log = logging.getLogger("holotek")


def _backoff_sleep(attempt, cap=60, base=10):
    delay = min(base * (2 ** attempt), cap)
    time.sleep(delay)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()
    config_path = args.config

    logging.basicConfig(level=logging.INFO)

    try:
        fd = os.open("/tmp/holotek.lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW)
    except OSError:
        sys.exit("lock path is inaccessible")
    lock = open(fd, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("holotek already running")

    cfg = load_config(config_path)

    def init_monitor():
        attempt = 0
        while True:
            try:
                return co2meter.CO2monitor(
                    bypass_decrypt=cfg.get("bypass_decrypt", False)
                )
            except Exception as e:
                log.error("device init failed: %s", e)
                _backoff_sleep(attempt)
                attempt += 1

    mon = init_monitor()
    state = {"last_zone": None, "last_notified_at": None, "last_notified_ppm": None}

    def on_sigint(*_):
        log.info("bye")
        sys.exit(0)

    signal.signal(signal.SIGINT, on_sigint)

    while True:
        try:
            cfg = load_config(config_path)
        except Exception as e:
            log.warning("config reload failed: %s", e)

        if not mon.is_alive:
            log.warning("device gone; reconnecting")
            attempt = 0
            while attempt < 10:
                try:
                    mon = co2meter.CO2monitor(
                        bypass_decrypt=cfg.get("bypass_decrypt", False)
                    )
                    break
                except Exception as e:
                    log.error("reconnect failed: %s", e)
                    _backoff_sleep(attempt)
                    attempt += 1
            else:
                log.error("reconnect exhausted; waiting for next poll cycle")
                time.sleep(cfg["poll_interval_seconds"])
                continue

        ppm = read_ppm(mon)
        if ppm is None:
            time.sleep(cfg["poll_interval_seconds"])
            continue

        out = decide(state, ppm, time.time(), cfg)
        if out:
            send_notification(out[0], out[1])
        time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    main()
