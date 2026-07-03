import argparse
import fcntl
import logging
import os
import signal
import sys
import time

from core import load_config, send_notification, reconnect, poll_step

log = logging.getLogger("holotek")


def _pid_alive(pid):
    """Best-effort liveness check. A PID owned by another user still answers
    with EPERM, which means it's alive — only ESRCH means truly gone."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def acquire_lock(path):
    """Acquire the single-instance lock at `path`, stealing it if the PID
    recorded inside is dead. Never blocks: any contention it can't resolve
    ends in sys.exit, never a hang."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW)
    except OSError:
        sys.exit("lock path is inaccessible")
    lock = open(fd, "r+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            pid = int(lock.read().strip())
        except ValueError:
            pid = None
        if pid and _pid_alive(pid):
            sys.exit("holotek already running")
        # stale lock from a dead process — steal it, but never block: if
        # something still genuinely holds it, exit rather than wait.
        lock.seek(0)
        lock.truncate()
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("holotek already running")
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    return lock


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--menubar", action="store_true", help="run as menu-bar app")
    args = ap.parse_args()
    config_path = args.config

    logging.basicConfig(level=logging.INFO)

    lock = acquire_lock(f"/tmp/holotek-{os.getuid()}.lock")

    if args.menubar:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        from menubar import HolotekApp
        HolotekApp(config_path=config_path).run()
        return

    cfg = load_config(config_path)
    mon = reconnect(cfg, attempts=None)
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
            mon = reconnect(cfg)
            if mon is None:
                log.error("reconnect exhausted; waiting for next poll cycle")
                time.sleep(cfg["poll_interval_seconds"])
                continue

        result = poll_step(mon, state, cfg)
        if result.ppm is None:
            log.warning("no CO2 reading this tick")
        else:
            msg = "CO2=%s ppm zone=%s notify=%s"
            log_args = [result.ppm, result.zone, bool(result.notifications)]
            if result.temp_c is not None:
                msg += " temp=%.1f"
                log_args.append(result.temp_c)
            log.info(msg, *log_args)
            for title, body in result.notifications:
                send_notification(title, body)
        time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    main()
