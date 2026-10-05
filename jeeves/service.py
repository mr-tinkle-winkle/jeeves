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


def start(timeout: float = 20.0) -> None:
    """Start the daemon and wait until it answers."""
    if running():
        return
    if has_unit():
        subprocess.run(["systemctl", "--user", "reset-failed", UNIT], capture_output=True, timeout=5)
        out = subprocess.run(["systemctl", "--user", "start", UNIT], capture_output=True, text=True, timeout=30)
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
    raise RuntimeError("Jeeves didn't start (see `journalctl --user -u jeeves` or the daemon log)")


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
