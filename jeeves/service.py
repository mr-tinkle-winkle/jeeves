"""Starting and stopping the daemon -- the on/off switch.

Off stops the daemon process (every model server, listener and overlay goes with
it). On starts a fresh one: through systemd when the ``jeeves`` user service exists
(NixOS module), otherwise as a detached ``jeeves daemon``. A daemon that starts
while Jeeves is switched off exits straight away, so logging in with Jeeves off
keeps it off.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

from . import config, ipc, paths

UNIT = "jeeves.service"


def running() -> bool:
    try:
        return ipc.call("ping") == "pong"
    except ipc.DaemonError:
        return False


def _old_pid() -> int | None:
    """The daemon process that's still there (also one shutting down -- it no longer answers)."""
    try:
        pid = int((paths.runtime_dir() / "daemon.pid").read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    try:                                            # (a recycled pid that isn't ours)
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ")
        if b"jeeves" not in cmd:
            return None
    except OSError:
        pass
    return pid


def wait_gone(timeout: float = 15.0) -> bool:
    """Until the old daemon has exited. Switching on while it was still shutting down (up to 6 s) did
    nothing: systemd still counted the unit as running, so "start" was a no-op -- and then it exited."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _old_pid() is None and not _unit_busy():
            return True
        time.sleep(0.2)
    return False


def _unit_busy() -> bool:
    if not has_unit():
        return False
    try:
        st = subprocess.run(["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True,
                            timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    return st in ("deactivating", "activating") and not running()


def has_unit() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        return subprocess.run(["systemctl", "--user", "cat", UNIT], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _daemon_argv() -> list[str]:
    exe = os.environ.get("JEEVES_BIN") or shutil.which("jeeves")
    return [exe, "daemon"] if exe else [sys.executable, "-m", "jeeves.cli", "daemon"]


def _why() -> str:
    """The unit's state and its last few log lines, for a start that didn't work."""
    try:
        state = subprocess.run(["systemctl", "--user", "show", UNIT, "-p", "ActiveState,SubState,Result"],
                               capture_output=True, text=True, timeout=10).stdout.split()
        tail = subprocess.run(["journalctl", "--user", "-u", UNIT, "-n", "8", "--no-pager", "-o", "cat"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return " (" + ", ".join(state) + ")" + ("\nLast lines of its log:\n" + tail if tail else "")


def start(timeout: float = 60.0) -> None:
    """Start the daemon and wait until it answers."""
    if running():
        return
    wait_gone()
    if has_unit():
        try:
            subprocess.run(["systemctl", "--user", "reset-failed", UNIT], capture_output=True, timeout=10)
            # --no-block: queue the start and come back, rather than waiting on systemd's job queue here
            out = subprocess.run(["systemctl", "--user", "start", "--no-block", UNIT], capture_output=True, text=True,
                                 timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("systemd didn't answer when asked to start Jeeves (your user session's systemd is "
                               "busy or stuck)") from exc
        if out.returncode != 0:
            raise RuntimeError(f"systemctl couldn't start Jeeves: {out.stderr.strip()}")
    else:
        log = paths.state_dir() / "daemon.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as f:
            subprocess.Popen(_daemon_argv(), stdin=subprocess.DEVNULL, stdout=f, stderr=f, start_new_session=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if running():
            return
        time.sleep(0.3)
    raise RuntimeError("Jeeves didn't answer within %d s%s\n(more: `journalctl --user -u jeeves -e`)"
                       % (timeout, _why() if has_unit() else ""))


def _save_enabled(on: bool) -> None:
    s = config.Settings()
    if s.is_locked("general.enabled"):
        raise config.LockedError("general.enabled is set in your NixOS configuration")
    s.set("general.enabled", on)


def power_on() -> None:
    """Switch on: remember it, then start a fresh daemon."""
    if running():
        ipc.call("power.set", on=True)
        return
    _save_enabled(True)
    start()


def power_off() -> None:
    """Switch off: the daemon saves it and exits. Without a daemon, just remember it."""
    if running():
        try:
            ipc.call("power.set", on=False)
        except ipc.DaemonUnavailable:
            pass                       # it exited before answering: that's the point
        deadline = time.time() + 15
        while time.time() < deadline and running():
            time.sleep(0.2)
        wait_gone(max(1.0, deadline - time.time()))   # (gone, not just no longer answering: see wait_gone)
        return
    _save_enabled(False)


def is_on() -> bool:
    if running():
        try:
            return bool(ipc.call("power.get"))
        except ipc.DaemonError:
            return False
    return False


def toggle() -> bool:
    """Returns the new state."""
    if is_on():
        power_off()
        return False
    power_on()
    return True
