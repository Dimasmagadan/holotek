import fcntl
import os

import pytest

from holotek import _pid_alive, acquire_lock


# ── _pid_alive() ──────────────────────────────────────────────────────────

class TestPidAlive:
    def test_own_pid_is_alive(self):
        assert _pid_alive(os.getpid()) is True

    def test_dead_pid_is_not_alive(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        assert _pid_alive(pid) is False

    def test_permissionerror_means_alive(self, monkeypatch):
        def fake_kill(pid, sig):
            raise PermissionError

        monkeypatch.setattr(os, "kill", fake_kill)
        assert _pid_alive(12345) is True


# ── acquire_lock() ───────────────────────────────────────────────────────

class TestAcquireLock:
    def test_free_path_succeeds(self, tmp_path):
        path = str(tmp_path / "test.lock")
        lock = acquire_lock(path)
        assert int(open(path).read().strip()) == os.getpid()
        lock.close()

    def test_second_lock_on_live_pid_exits(self, tmp_path):
        path = str(tmp_path / "test.lock")
        held = acquire_lock(path)  # holds the flock for this (live) pid
        with pytest.raises(SystemExit):
            acquire_lock(path)
        held.close()

    def test_stale_lock_with_dead_pid_is_stolen(self, tmp_path):
        path = str(tmp_path / "test.lock")
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        with open(path, "w") as f:
            f.write(str(pid))
        # No live flock is held (the dead process's fd was closed by the
        # kernel on exit), so this must succeed and take over the file.
        lock = acquire_lock(path)
        assert int(open(path).read().strip()) == os.getpid()
        lock.close()

    def test_steal_attempt_that_cannot_actually_lock_exits_without_hanging(
        self, tmp_path, monkeypatch
    ):
        path = str(tmp_path / "test.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR)
        holder = open(fd, "r+")
        holder.write("999999")
        holder.flush()
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

        import holotek
        monkeypatch.setattr(holotek, "_pid_alive", lambda pid: False)

        with pytest.raises(SystemExit):
            acquire_lock(path)
        holder.close()
