"""Interface with Puppetry (the keyboard/mouse macro daemon).

Decision (SPEC.md open question): **Macros interface with Puppetry**; Control
Mode reuses Puppetry's approach (uinput virtual devices) in Jeeves' own code.

* Macros live in Puppetry, so they keep working with its editor, combos,
  profiles and categories. Jeeves talks to it exactly the way Puppetry's own
  CLI and GUI do: the JSON-line control socket
  (``~/.config/macro-daemon/control.sock``: FIRE / ABORT / PAUSE / RESUME) and
  the shared config files (``macros.json``, ``profiles/*.json``,
  ``state.json``). New or changed macros are validated with Puppetry's own
  compiler (``puppetry-daemon --check``) and applied the way Puppetry applies
  saves: ``systemctl --user restart macro-daemon``.
* Control Mode needs held-key state Jeeves can release on Abort and devices
  named jeeves-keyboard / jeeves-mouse / jeeves-controller, so it creates its
  own uinput devices (``control.py``) instead of routing every key through
  Puppetry's macro runner.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import uuid
from pathlib import Path
from typing import Any

from ..functions.base import FunctionError
from ..util import best_match

# Used when Puppetry's own reference.py can't be found. Condensed from
# Puppetry's DICTIONARY_TEXT (gui/reference.py).
FALLBACK_DICTIONARY = """\
tap(key, time_=0.1)            key/button down, wait, up. tap(KEY_A), tap(BTN_LEFT)
kd(key) / ku(key)              down only / up only -- pair them
combo(*keys, time_=0.1)        chord, e.g. combo(KEY_LEFTCTRL, KEY_C); pass time_ as keyword
type(text, time_per_letter=0.05, async_=False)   types a string (US QWERTY)
wait(time_, precise=False)     pause time_ seconds (scaled by speed())
speed(multiplier)              scales every duration for the rest of this run
move_mouse(x, y, time_=0.25, easing="inout", async_=False, move_to=False)
                               relative move, or absolute with move_to=True (KDE)
wheel(amount)                  scroll; positive = up
command(cmd, *args)            run a shell command; args fill {0} {1} placeholders (shell-quoted)
ignore(what)                   toggle blocking real "keyboard" / "mouse_buttons" / "mouse_movement" / "mouse"
ignore_keys(*keys)             toggle blocking specific keys
actAs(key, ignore, *acting_keys)  remap a key while the macro runs
waitForPress(*keys)            wait until one of the keys is pressed
getMousePosition()             saved absolute position, usable with move_mouse(pos)
getButtonsHeld()               list of held keys/buttons
getAxis(axis) / axis(axis, value, time_=0)   controller axes LX LY RX RY LT RT DPAD_X DPAD_Y
arguments(name="default", ...) declare macro arguments (passed by `puppetry --name=... args`)
Other macros can be called by name as functions. Python control flow (if/for/while/def) works.
Key names: KEY_A..KEY_Z, KEY_1.., KEY_ENTER, KEY_SPACE, KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_LEFTALT,
KEY_LEFTMETA, KEY_F1.., BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA. Simplified names
(A, ENTER, LMB) work when simplified_names is on.
"""


def blank_macro() -> dict[str, Any]:
    # same fields as Puppetry's editor_page.blank_macro()
    return {
        "id": None, "name": "New Macro", "description": "", "repeat_mode": "none",
        "combo": [], "trigger_edge": "down", "locked": False, "code": "",
        "simplified_names": True, "python_on": True,
        "ignore_keyboard": False, "ignore_mouse_buttons": False, "ignore_mouse_movement": False,
    }


class Puppetry:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    # ---- locations -------------------------------------------------------
    @property
    def config_dir(self) -> Path:
        return Path(os.path.expanduser(self.settings.get("puppetry.config_dir", "~/.config/macro-daemon")))

    def installed(self) -> bool:
        return self.config_dir.is_dir() or shutil.which("puppetry") is not None

    def _wrapper_var(self, name: str) -> str | None:
        """Puppetry's Nix wrapper sets PUPPETRY_BIN_DIR / PYTHONPATH; read them
        from the wrapper script so Jeeves finds the same files."""
        if os.environ.get(name):
            return os.environ[name]
        exe = shutil.which("puppetry")
        if not exe:
            return None
        try:
            text = Path(exe).read_text(errors="replace")
        except OSError:
            return None
        # makeWrapper writes: export NAME='/nix/store/...'
        m = re.search(rf"\b{name}=['\"]?([^'\"\s]+)", text)
        return m.group(1) if m else None

    def find_binary(self, name: str) -> str | None:
        d = self._wrapper_var("PUPPETRY_BIN_DIR")
        if d and (Path(d) / name).is_file():
            return str(Path(d) / name)
        return shutil.which(name)

    def dictionary_text(self) -> str:
        share = os.environ.get("PUPPETRY_SHARE") or self._wrapper_var("PYTHONPATH")
        for base in [p for p in (share or "").split(":") if p]:
            ref = Path(base) / "reference.py"
            if ref.is_file():
                m = re.search(r'DICTIONARY_TEXT\s*=\s*"""\\?\n?(.*?)"""', ref.read_text(errors="replace"), re.S)
                if m:
                    return m.group(1)
        return FALLBACK_DICTIONARY

    # ---- control socket --------------------------------------------------
    def send(self, payload: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
        sock_path = self.config_dir / "control.sock"
        if not sock_path.exists():
            raise FunctionError("Puppetry's daemon isn't running (no control socket)")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(str(sock_path))
            s.sendall((json.dumps(payload) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except OSError as exc:
            raise FunctionError(f"couldn't reach Puppetry: {exc}") from exc
        finally:
            s.close()
        try:
            resp = json.loads(buf.decode() or "{}")
        except ValueError:
            resp = {"ok": False, "error": buf.decode(errors="replace")}
        return resp

    def fire(self, name: str, args: list[str] | None = None) -> None:
        resp = self.send({"cmd": "FIRE", "name": name, "args": list(args or [])})
        if not resp.get("ok"):
            raise FunctionError(resp.get("error", "Puppetry refused"))

    def abort(self) -> None:
        try:
            self.send({"cmd": "ABORT"}, timeout=1.0)
        except FunctionError:
            pass

    # ---- macros.json -----------------------------------------------------
    def _read(self, name: str, default: Any) -> Any:
        try:
            return json.loads((self.config_dir / name).read_text())
        except (OSError, ValueError):
            return default

    def _write(self, name: str, data: Any) -> None:
        path = self.config_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".jeeves.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)

    def macros(self) -> list[dict[str, Any]]:
        return self._read("macros.json", {"macros": []}).get("macros", [])

    def macro_names(self) -> list[str]:
        return [m.get("name", "") for m in self.macros() if m.get("name")]

    def resolve_name(self, name: str) -> str | None:
        if not name:
            return None
        return best_match(name, self.macro_names(), cutoff=0.55)

    def find(self, name: str) -> dict[str, Any] | None:
        return next((m for m in self.macros() if m.get("name") == name), None)

    def most_recent_macro(self, ctx: Any) -> str | None:
        for item in reversed(ctx.engine.history.recent(int(ctx.settings.get("memory.recent_count", 3)) + 5,
                                                       exclude=ctx.request.get("id"))):
            if item.get("function") == "macros":
                args = item.get("args") or {}
                found = self.resolve_name(args.get("name", "")) or self.resolve_name(args.get("_made", ""))
                if found:
                    return found
        return None

    def check(self, macro: dict[str, Any]) -> tuple[bool, str]:
        daemon = self.find_binary("puppetry-daemon")
        if daemon:
            try:
                r = subprocess.run([daemon, "--check"], input=json.dumps(macro), capture_output=True, text=True,
                                   timeout=15)
                return r.returncode == 0, (r.stdout.strip() or r.stderr.strip() or "OK")
            except (OSError, subprocess.TimeoutExpired) as exc:
                return False, f"couldn't run Puppetry's checker: {exc}"
        import textwrap
        try:
            compile("def _macro(*_a, **_k):\n" + textwrap.indent(macro.get("code") or "pass", "    ") + "\n",
                    "<macro>", "exec")
        except SyntaxError as exc:
            return False, f"SyntaxError: {exc}"
        return True, "OK (only Python syntax checked; puppetry-daemon not found)"

    def save_macro(self, macro: dict[str, Any]) -> dict[str, Any]:
        data = self._read("macros.json", {"macros": []})
        items = data.setdefault("macros", [])
        if not macro.get("id"):
            macro["id"] = str(uuid.uuid4())
        existing = next((m for m in items if m.get("id") == macro["id"]), None)
        if existing is None:
            items.append(macro)
            state = self._read("state.json", {})
            profile_id = state.get("active_profile") or "profile_1"
            prof_name = f"profiles/{profile_id}.json"
            profile = self._read(prof_name, {"name": profile_id, "enabled": {}})
            profile.setdefault("enabled", {})[macro["id"]] = True   # new macros start enabled, like Puppetry's editor
            self._write(prof_name, profile)
        else:
            existing.clear()
            existing.update(macro)
        self._write("macros.json", data)
        self.restart()
        return macro

    def restart(self) -> None:
        unit = self.settings.get("puppetry.service", "macro-daemon.service")
        try:
            subprocess.run(["systemctl", "--user", "restart", unit], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass

    # ---- model-written macros --------------------------------------------
    def _write_code(self, ctx: Any, instruction: str, current: str = "") -> str:
        system = ("You write macros for Puppetry, a Linux keyboard/mouse macro daemon. Macros are Python "
                  "snippets using ONLY these primitives:\n\n" + self.dictionary_text() +
                  "\n\nReply with the macro code only -- no explanations, no markdown fences.")
        prompt = (f"Current macro code:\n{current}\n\nChange it so that: {instruction}" if current
                  else f"Write a macro that: {instruction}")
        last_err = ""
        for _ in range(3):
            code = ctx.engine.models.respond(ctx.agent, prompt + (f"\n\nYour last attempt failed: {last_err}"
                                                                  if last_err else ""),
                                             system=system, ctx=ctx, raw=True)
            if code is None:
                raise FunctionError("no local response model is set up (needed to write macros)")
            code = re.sub(r"^```\w*\n|\n?```$", "", code.strip())
            ok, msg = self.check({**blank_macro(), "code": code})
            if ok:
                return code
            last_err = msg
            ctx.think(f"Macro didn't compile: {msg}")
        raise FunctionError(f"couldn't write a working macro: {last_err}")

    def create_with_model(self, ctx: Any, name: str, description: str) -> dict[str, Any]:
        code = self._write_code(ctx, description)
        macro = blank_macro()
        base = name.strip() or description.strip()[:40]
        final, n = base, 2
        while final in self.macro_names():
            final, n = f"{base} {n}", n + 1
        macro.update({"name": final, "description": description, "code": code})
        ctx.trace("macro", name=final, code=code)
        if isinstance(ctx.entry.get("args"), dict):
            ctx.entry["args"]["_made"] = final
        return self.save_macro(macro)

    def adjust_with_model(self, ctx: Any, name: str, change: str) -> dict[str, Any]:
        macro = self.find(name)
        if macro is None:
            raise FunctionError(f"no macro named '{name}'")
        if macro.get("locked"):
            raise FunctionError(f"'{name}' is locked in Puppetry")
        if not macro.get("python_on", True):
            raise FunctionError(f"'{name}' uses Puppetry's native interpreter; switch it to Python to adjust it here")
        code = self._write_code(ctx, change or "improve it", current=macro.get("code", ""))
        ctx.trace("macro", name=name, old_code=macro.get("code", ""), code=code)
        macro = dict(macro, code=code)
        return self.save_macro(macro)
