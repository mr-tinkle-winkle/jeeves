"""Keybinds (Text Request, Voice Request, Manual Response Review, Abort) and the
keys held for hold-to-listen (wake_word.hold_to_listen).

Watches every real keyboard read-only through evdev -- the same way Puppetry
does: devices are never grabbed, so typing is unaffected. Jeeves' own virtual
devices are ignored. Mice are read too, but only while a keybind uses one of
their buttons (BTN_SIDE, BTN_EXTRA, ...). Without input-group access this is
off and the CLI commands (bind them in the compositor) do the same thing:

    jeeves --manual_request=text
    jeeves --manual_request=voice --agent=jeeves
    jeeves --review
    jeeves --abort
"""
from __future__ import annotations

import logging
import selectors
import threading
import time
from typing import Any, Callable, Iterable

log = logging.getLogger("jeeves.keyboard")

try:
    import evdev
    from evdev import ecodes as e
except Exception:  # pragma: no cover
    evdev = None  # type: ignore
    e = None  # type: ignore

HISTORY_SECONDS = 60.0       # how far back held_during() can look (a listener running behind)
RECORD_SECONDS = 10.0        # the GUI's Record button waits this long for a key combo
SETTINGS_EVERY = 1.0         # s between re-reading what to watch (not per event: mice send ~1000 a second)
FOREVER = float("inf")


def names_of(code: int) -> tuple[str, ...]:
    """Every name of a key or button code. Some codes have two (KEY_MUTE is also KEY_MIN_INTERESTING,
    BTN_LEFT is BTN_MOUSE), and python-evdev gives those as a tuple -- taken as one name, a held Mute
    key broke the list of held keys and a keybind using it never fired."""
    if e is None:
        return ()
    names = e.KEY.get(code) or e.BTN.get(code)
    if names is None:
        return ()
    return (names,) if isinstance(names, str) else tuple(names)


def code_of(name: str) -> int | None:
    if e is None:
        return None
    code = getattr(e, name, None)
    return code if isinstance(code, int) else None


def best_name(names: Iterable[str]) -> str:
    """The name to show for a key with several: the plain one (KEY_MUTE, not KEY_MIN_INTERESTING;
    BTN_LEFT, not BTN_MOUSE)."""
    return min(names, key=lambda n: (len(n), n))


class Keyboard:
    def __init__(self, settings: Any, on_bind: Callable[[str, dict[str, Any]], None],
                 on_hold: Callable[[bool], None] | None = None) -> None:
        self.settings = settings
        self.on_bind = on_bind
        self.on_hold = on_hold              # the hold-to-listen keys were pressed (True) / let go (False)
        self._held: set[str] = set()
        self._fired: set[str] = set()
        self._spans: dict[str, list[list[float]]] = {}   # key -> [[down, up], ...] (up = FOREVER while held)
        self._holding = False
        self._recording: dict[str, Any] | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = "off"

    # ------------------------------------------------------------------ what to watch
    def keybinds_on(self) -> bool:
        return bool(self.settings.get("general.watch_keyboard_for_keybinds", True))

    def hold_keys(self) -> set[str]:
        """The keys hold-to-listen waits for (empty: it's off)."""
        if not self.settings.get("wake_word.hold_to_listen", False):
            return set()
        keys = self.settings.get("wake_word.hold_keys") or []
        return {str(k).strip().upper() for k in ([keys] if isinstance(keys, str) else keys) if str(k).strip()}

    def wanted(self) -> bool:
        return self.keybinds_on() or bool(self.hold_keys()) or self._recording is not None

    def _bindings(self) -> list[tuple[str, dict[str, Any], set[str]]]:
        s = self.settings
        out: list[tuple[str, dict[str, Any], set[str]]] = []
        abort = s.get("general.abort_key")
        if abort:
            out.append(("abort", {}, {abort} if isinstance(abort, str) else set(abort)))
        mr = s.get("manual_request", {})
        if mr.get("text_keybind_enabled") and mr.get("text_keybind"):
            out.append(("text_request", {}, set(mr["text_keybind"])))
        if mr.get("voice_keybind_enabled"):
            for agent, combo in (mr.get("voice_keybinds") or {}).items():
                if combo:
                    out.append(("voice_request", {"agent": agent}, set(combo)))
        if mr.get("toggle_keybind"):
            out.append(("toggle", {}, set(mr["toggle_keybind"])))
        if mr.get("review_keybind"):
            out.append(("review", {}, set(mr["review_keybind"])))
        return out

    def _buttons(self) -> frozenset[str]:
        """Mouse buttons a keybind or the hold-to-listen keys use: mice are only read for those."""
        names = set(self.hold_keys())
        if self.keybinds_on():
            for _action, _params, combo in self._bindings():
                names |= combo
        return frozenset(n for n in names if n.startswith("BTN_"))

    # ------------------------------------------------------------------ state
    def held(self) -> list[str]:
        """The keys and buttons held down now, each once (by its plain name)."""
        with self._lock:
            held = set(self._held)
        by_code: dict[Any, list[str]] = {}
        for n in held:
            code = code_of(n)
            by_code.setdefault(n if code is None else code, []).append(n)
        return sorted(best_name(v) for v in by_code.values())

    def holding(self, keys: Iterable[str]) -> bool:
        """Are all these keys held down right now?"""
        want = set(keys)
        with self._lock:
            return bool(want) and want <= self._held

    def held_during(self, keys: Iterable[str], t0: float, t1: float) -> bool:
        """Were all these keys held down together at some moment between t0 and t1 (time.time())? Lets a
        listener that runs behind (a busy computer) judge its audio by when it was recorded."""
        want = set(keys)
        if not want:
            return False
        with self._lock:
            windows = [(t0, t1)]
            for key in want:
                spans = self._spans.get(key) or []
                windows = [(max(a, c), min(b, d)) for a, b in windows for c, d in spans if max(a, c) <= min(b, d)]
                if not windows:
                    return False
        return True

    def _down(self, name: str, now: float) -> None:
        self._held.add(name)
        spans = self._spans.setdefault(name, [])
        if not spans or spans[-1][1] != FOREVER:
            spans.append([now, FOREVER])
        while len(spans) > 1 and spans[0][1] < now - HISTORY_SECONDS:
            spans.pop(0)

    def _up(self, name: str, now: float) -> None:
        self._held.discard(name)
        spans = self._spans.get(name)
        if spans and spans[-1][1] == FOREVER:
            spans[-1][1] = max(now, spans[-1][0])

    def _release_all(self) -> None:
        """Not reading the keyboard any more: nothing counts as held."""
        now = time.time()
        with self._lock:
            for name in list(self._held):
                self._up(name, now)
            self._fired.clear()
        self._check_hold()

    def _resync(self, devices: list[Any]) -> None:
        """Which keys are down right now (on starting to read, and after the kernel dropped events): a key
        let go while nobody was reading would otherwise count as held for good -- and hold-to-listen
        would listen for names until it was pressed again."""
        down: set[str] = set()
        for d in devices:
            try:
                for code in d.active_keys():
                    down.update(names_of(code))
            except (OSError, AttributeError):
                pass
        now = time.time()
        with self._lock:
            for name in self._held - down:
                self._up(name, now)
            for name in down - self._held:
                self._down(name, now)
            self._fired = {tag for tag in self._fired if set(tag.split("+")) <= self._held}
        self._check_hold()

    def _check_hold(self) -> None:
        keys = self.hold_keys()
        with self._lock:
            holding = bool(keys) and keys <= self._held
            changed, self._holding = holding != self._holding, holding
        if changed and self.on_hold is not None:
            try:
                self.on_hold(holding)
            except Exception:
                log.exception("hold-to-listen handler failed")

    # ------------------------------------------------------------------ recording a combo (the GUI)
    def record(self, seconds: float = RECORD_SECONDS) -> dict[str, Any]:
        """Start recording a key combo: the next keys held together, in the order pressed, until they're
        let go. Keybinds don't fire meanwhile (recording Meta+Shift+J must not start a Voice Request)."""
        with self._lock:
            self._recording = {"keys": [], "until": time.time() + seconds, "done": False}
        return self.recorded()

    def recorded(self) -> dict[str, Any]:
        with self._lock:
            r = self._recording
            if r is not None and not r["done"] and time.time() > r["until"]:
                r["done"] = True
            out = {"recording": r is not None and not r["done"], "keys": list(r["keys"]) if r else [],
                   "held": self.held(), "status": self.status}
            if r is not None and r["done"]:
                self._recording = None
        return out

    def _record_down(self, names: tuple[str, ...]) -> None:
        r = self._recording
        if r is not None and not r["done"] and not set(names) & set(r["keys"]):
            r["keys"].append(best_name(names))

    def _record_up(self) -> None:
        r = self._recording
        if r is not None and r["keys"] and not self._held:
            r["done"] = True                  # everything let go: that's the combo

    # ------------------------------------------------------------------ reading the devices
    def start(self) -> None:
        if evdev is None:
            self.status = "python-evdev isn't installed"
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="jeeves-keyboard")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _devices(self, buttons: frozenset[str]) -> tuple[list[Any], int]:
        """Every keyboard, and the mice that have one of these buttons -> (devices, how many are mice)."""
        want = {c for c in (code_of(n) for n in buttons) if c is not None}
        keyboards, mice = [], []
        for path in evdev.list_devices():
            try:
                d = evdev.InputDevice(path)
            except OSError:
                continue
            if d.name.startswith("jeeves-") or "macro-daemon" in d.name:
                d.close()
                continue
            caps = d.capabilities().get(e.EV_KEY, [])
            if e.KEY_A in caps and e.KEY_SPACE in caps:
                keyboards.append(d)
            elif want & set(caps):
                mice.append(d)
            else:
                d.close()
        return keyboards + mice, len(mice)

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.wanted():
                self.status = "disabled in settings"
                self._release_all()
                self._stop.wait(SETTINGS_EVERY)
                continue
            buttons = self._buttons()
            try:
                devices, mice = self._devices(buttons)
            except OSError as exc:
                self.status = f"can't list input devices: {exc}"
                self._stop.wait(5)
                continue
            if len(devices) == mice:
                for d in devices:
                    d.close()
                self.status = "no readable keyboard: is the user in the 'input' group?"
                self._release_all()
                self._stop.wait(5)
                continue
            self.status = f"watching {len(devices) - mice} keyboard(s)" + (f" and {mice} mouse/mice" if mice else "")
            self._resync(devices)
            sel = selectors.DefaultSelector()
            for d in devices:
                sel.register(d, selectors.EVENT_READ)
            known = set(evdev.list_devices())
            last_scan = last_check = time.time()
            try:
                while not self._stop.is_set() and sel.get_map():
                    for key, _ in sel.select(timeout=1.0):
                        dev = key.fileobj
                        try:
                            for ev in dev.read():
                                if ev.type == e.EV_KEY:
                                    self._handle(ev.code, ev.value, ev.timestamp())
                                elif ev.type == e.EV_SYN and ev.code == e.SYN_DROPPED:
                                    self._resync(devices)       # events were lost: read the keys again
                        except BlockingIOError:
                            pass
                        except OSError:   # unplugged
                            sel.unregister(dev)
                    now = time.time()
                    if now - last_check >= SETTINGS_EVERY:
                        last_check = now
                        if not self.wanted() or self._buttons() != buttons:
                            break         # turned off, or a keybind started (or stopped) using a mouse button
                        self._expire_recording()
                        self._check_hold()        # (the hold-to-listen keys or setting changed)
                    if now - last_scan > 10:
                        last_scan = now
                        if set(evdev.list_devices()) != known:
                            break         # something was plugged in: rescan
            finally:
                for d in devices:
                    try:
                        d.close()
                    except OSError:
                        pass
                sel.close()

    def _expire_recording(self) -> None:
        with self._lock:
            r = self._recording
            if r is not None and time.time() > r["until"] + 30:
                self._recording = None        # the GUI never came back for it

    def _handle(self, code: int, value: int, at: float | None = None) -> None:
        names = names_of(code)
        if not names or value not in (0, 1):
            return                            # (2: autorepeat)
        now = at if at is not None else time.time()
        recording = False
        with self._lock:
            if value == 0:
                for n in names:
                    self._up(n, now)
                # a combo can fire again once any of its keys is released
                self._fired = {b for b in self._fired if not set(names) & set(b.split("+"))}
                self._record_up()
            else:
                for n in names:
                    self._down(n, now)
                recording = self._recording is not None and not self._recording["done"]
                if recording:
                    self._record_down(names)
        self._check_hold()
        if value == 0 or recording or not self.keybinds_on():
            return
        for action, params, combo in self._bindings():
            tag = "+".join(sorted(combo))
            with self._lock:
                fire = bool(combo) and combo <= self._held and bool(set(names) & combo) and tag not in self._fired
                if fire:
                    self._fired.add(tag)
            if fire:
                try:
                    self.on_bind(action, params)
                except Exception:
                    log.exception("keybind handler failed")
