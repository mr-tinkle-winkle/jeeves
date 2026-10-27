"""Hold-to-listen: agent names are only listened for while a key combo is held."""
import array
import time

import pytest

from jeeves.daemon.audio import FRAME_SAMPLES
from jeeves.daemon.engine import HOLD_INDICATOR
from jeeves.daemon.keyboard import Keyboard
from jeeves.daemon.listener import HOLD_GRACE, Listener, Session


def frame(level: float) -> bytes:
    v = int(level * 32767)
    return array.array("h", [v, -v] * (FRAME_SAMPLES // 2)).tobytes()


LOUD, QUIET, NAME = frame(0.2), frame(0.0), frame(0.05)     # NAME: what the fake wake model hears as "Jeeves"


class FakeRec:
    def __init__(self):
        self.fed = 0
        self.phrase_open = False
        self.speech_end = 0.0
        self.last_result = []

    def feed(self, f):
        self.fed += 1
        if f == NAME:
            self.last_result = [{"word": "jeeves", "conf": 0.95, "start": 0.0, "end": 0.03}]
            return [("jeeves", 0.95)]
        return []


class FakeSpotter:
    def __init__(self):
        self.made = []

    def recognizer(self, names):
        self.made.append(FakeRec())
        return self.made[-1]


class Keys:
    """Stands in for the keyboard: when the hold-to-listen keys were down."""
    status = "watching 1 keyboard(s)"

    def __init__(self):
        self.spans = []

    def press(self, t=None):
        self.spans.append([time.time() if t is None else t, float("inf")])

    def release(self, t=None):
        self.spans[-1][1] = time.time() if t is None else t

    def held_during(self, keys, t0, t1):
        return any(max(a, t0) <= min(b, t1) for a, b in self.spans)

    def holding(self, keys):
        return bool(self.spans) and self.spans[-1][1] == float("inf")


class FakeSTT:
    def __init__(self, text):
        self.text = text

    def transcribe(self, pcm, prompt="", language="en"):
        return self.text


def wait_for(cond, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not cond():
        time.sleep(0.01)
    return cond()


@pytest.fixture
def holding(engine):
    """Hold-to-listen on, a fake wake model and keyboard; returns (keys, spotter, names heard)."""
    engine.settings.set("wake_word.hold_to_listen", True)
    engine.settings.set("wake_word.stt_backup", False)
    keys, spot, woke = Keys(), FakeSpotter(), []
    engine.keyboard = keys
    engine.models.wake_spotter = lambda: spot
    engine.on_wake = lambda source, name, conf, after, **kw: woke.append(name)
    return keys, spot, woke


# ---------------------------------------------------------------------------- the wake model
def test_names_are_only_heard_while_the_keys_are_held(engine, holding):
    keys, spot, woke = holding
    lst = Listener(engine, "microphone")
    for f in [QUIET] * 5 + [NAME] + [QUIET] * 20:          # (over half a second before pressing them)
        lst.process(f)
    assert woke == [] and spot.made == []                  # not held: the wake model doesn't even run
    keys.press()
    for f in [QUIET] * 3 + [NAME] + [QUIET] * 3:
        lst.process(f)
    assert woke == ["jeeves"]


def test_a_name_begun_just_before_pressing_the_keys_is_heard(engine, holding):
    keys, spot, woke = holding
    lst = Listener(engine, "microphone")
    for f in [QUIET] * 30 + [NAME] + [QUIET] * 3:          # "Jee-" as the keys go down
        lst.process(f)
    keys.press()
    lst.process(QUIET)
    assert woke == ["jeeves"]
    assert lst.spot_base == lst.frame_no - 1 - 16          # its word times still line up with the audio
    assert spot.made[-1].fed == 17                          # half a second from before, and this frame


def test_names_are_heard_a_moment_after_letting_go_then_not(engine, holding):
    keys, spot, woke = holding
    lst = Listener(engine, "microphone")
    now = time.time()
    keys.press(now - 5)
    keys.release(now - HOLD_GRACE / 2)                    # just let go: the name may still be decoding
    lst.process(NAME)
    assert woke == ["jeeves"]
    keys.spans[-1][1] = now - HOLD_GRACE - 1               # let go a while ago
    lst.process(QUIET)
    lst.process(NAME)
    assert woke == ["jeeves"] and lst.wake_rec is None


def test_an_open_listen_keeps_the_wake_model_but_a_name_doesnt_count(engine, holding):
    keys, spot, woke = holding
    lst = Listener(engine, "microphone")
    engine.sessions["microphone"] = Session("microphone", "jeeves", "request")
    lst.process(NAME)
    assert woke == [] and lst.wake_rec is not None         # (its decoding helps tell when you've stopped)


def test_without_hold_to_listen_names_are_always_heard(engine, holding):
    keys, spot, woke = holding
    engine.settings.set("wake_word.hold_to_listen", False)
    lst = Listener(engine, "microphone")
    lst.process(NAME)
    assert woke == ["jeeves"]


# ---------------------------------------------------------------------------- speech recognition paths
def _utterance(lst, frames):
    for i, f in enumerate(frames):
        lst._backup(f, f is LOUD and i >= 2, time.time() + 0.03 * i, 0.6, False, None)


def test_missed_call_checks_only_run_for_what_was_said_holding_the_keys(engine, holding, monkeypatch):
    keys, spot, woke = holding
    seen = []
    monkeypatch.setattr(engine, "run_async", lambda fn, *a: seen.append(fn.__name__))
    lst = Listener(engine, "microphone")
    lst._config(time.time())
    _utterance(lst, [LOUD] * 30 + [QUIET] * 40)
    assert seen == []                                      # not even transcribed
    keys.press()
    _utterance(lst, [LOUD] * 30 + [QUIET] * 40)
    assert seen == ["on_backup_utterance"]


def test_a_name_in_the_transcript_only_calls_while_holding(engine, holding, monkeypatch):
    from jeeves.daemon import listener as listener_mod
    keys, spot, woke = holding
    engine.settings.set("wake_word.engine", "stt-match")
    clock = {"t": 1000.0}
    monkeypatch.setattr(listener_mod.time, "time", lambda: clock["t"])
    calls = []
    monkeypatch.setattr(engine, "run_async", lambda fn, *a: calls.append(a))
    lst = Listener(engine, "microphone")

    def say():
        for f in [QUIET] * 40 + [LOUD] * 25 + [QUIET] * 60:
            clock["t"] += 0.03
            lst.process(f)
    say()
    keys.press()
    say()
    assert [c[2] for c in calls] == [False, True]          # on_utterance(source, pcm, may_call)

    handled = []
    engine.handle_text = lambda text, agent_id=None, **k: handled.append((text, agent_id))
    engine.models.stt = lambda agent=None: FakeSTT("Jeeves, what time is it")
    engine.on_utterance("microphone", LOUD * 30, may_call=False)
    assert handled == []
    engine.on_utterance("microphone", LOUD * 30, may_call=True)
    assert handled == [("what time is it", "jeeves")]


# ---------------------------------------------------------------------------- the engine
def test_hold_to_listen_works_with_always_listen_off(engine):
    engine.settings.set("wake_word.enabled", False)
    assert not engine.listens_for_names() and not engine._needs_source("microphone")
    engine.settings.set("wake_word.hold_to_listen", True)
    assert engine.listens_for_names() and engine._needs_source("microphone")   # open, for a name said as you press
    assert engine.hold_keys() == ["KEY_RIGHTCTRL"]
    engine.settings.set("wake_word.hold_keys", "key_leftmeta")
    assert engine.hold_keys() == ["KEY_LEFTMETA"]


def test_holding_the_keys_shows_agents_are_listening_for_names(engine, monkeypatch):
    from jeeves.daemon import engine as engine_mod
    monkeypatch.setattr(engine_mod, "HOLD_SHOW_AFTER", 0.01)
    engine.settings.set("wake_word.hold_to_listen", True)
    keys = Keys()
    engine.keyboard = keys
    keys.press()
    engine._on_hold(True)
    assert wait_for(lambda: HOLD_INDICATOR in engine.indicators)
    st = engine.indicators[HOLD_INDICATOR]
    assert st["stage"] == "listening" and "name" in st["detail"]
    keys.release()
    engine._on_hold(False)
    assert HOLD_INDICATOR not in engine.indicators
    keys.press()                                           # a quick tap (part of a shortcut): nothing shown
    engine._on_hold(True)
    keys.release()
    engine._on_hold(False)
    time.sleep(0.05)
    assert HOLD_INDICATOR not in engine.indicators
    keys.press()                                           # a name heard while holding: its listen takes over
    engine._on_hold(True)
    assert wait_for(lambda: HOLD_INDICATOR in engine.indicators)
    engine.on_wake("microphone", "jeeves", 0.99, [])
    assert HOLD_INDICATOR not in engine.indicators and engine.sessions["microphone"].agent_id == "jeeves"


def test_hold_to_listen_says_when_it_cant_see_the_keys(engine):
    notices = []
    engine.publish = lambda topic, data: notices.append(data["text"]) if topic == "notice" else None
    engine.start_io = True                                 # (only a real daemon checks)
    keys = Keys()
    keys.status = "no readable keyboard (is the user in the 'input' group?)"
    engine.keyboard = keys
    engine._check_hold_to_listen()
    assert notices == []                                   # off: nothing to say
    engine.settings.set("wake_word.hold_to_listen", True)
    engine._check_hold_to_listen()
    assert notices == []                                   # just turned on: the keyboard may still be starting
    engine._check_hold_to_listen()
    assert len(notices) == 1 and "'input' group" in notices[0]
    engine._check_hold_to_listen()
    assert len(notices) == 1                               # said once
    keys.status = "watching 1 keyboard(s)"
    engine._check_hold_to_listen()
    engine.settings.set("wake_word.hold_keys", [])
    engine._check_hold_to_listen()
    engine._check_hold_to_listen()
    assert len(notices) == 2 and "no keys" in notices[-1]


# ---------------------------------------------------------------------------- the keyboard
@pytest.fixture
def ecodes():
    return pytest.importorskip("evdev").ecodes


def test_keys_with_two_names_work(engine, ecodes):
    fired = []
    engine.settings.set("general.abort_key", "KEY_MUTE")
    kb = Keyboard(engine.settings, lambda action, params: fired.append(action))
    kb._handle(ecodes.KEY_LEFTSHIFT, 1)
    kb._handle(ecodes.KEY_MUTE, 1)                         # KEY_MUTE is also KEY_MIN_INTERESTING
    assert fired == ["abort"]
    assert kb.held() == ["KEY_LEFTSHIFT", "KEY_MUTE"]     # each key once, by its plain name
    kb._handle(ecodes.KEY_MUTE, 0)
    kb._handle(ecodes.KEY_LEFTSHIFT, 0)
    kb._handle(ecodes.BTN_LEFT, 1)                         # (also BTN_MOUSE)
    assert kb.held() == ["BTN_LEFT"]


def test_held_during_goes_by_when_the_keys_were_down(engine, ecodes):
    kb = Keyboard(engine.settings, lambda a, p: None)
    ctrl = ["KEY_RIGHTCTRL"]
    kb._handle(ecodes.KEY_RIGHTCTRL, 1, at=100.0)
    kb._handle(ecodes.KEY_RIGHTCTRL, 2, at=100.5)          # autorepeat changes nothing
    kb._handle(ecodes.KEY_RIGHTCTRL, 0, at=102.0)
    assert not kb.held_during(ctrl, 98.0, 99.9)
    assert kb.held_during(ctrl, 99.0, 100.1) and kb.held_during(ctrl, 101.0, 101.5)
    assert not kb.held_during(ctrl, 102.5, 103.0)
    both = ["KEY_LEFTMETA", "KEY_LEFTALT"]
    kb._handle(ecodes.KEY_LEFTMETA, 1, at=200.0)
    kb._handle(ecodes.KEY_LEFTMETA, 0, at=201.0)
    kb._handle(ecodes.KEY_LEFTALT, 1, at=201.5)
    kb._handle(ecodes.KEY_LEFTALT, 0, at=202.0)
    assert not kb.held_during(both, 199.0, 203.0)          # one after the other isn't the combo
    kb._handle(ecodes.KEY_LEFTMETA, 1, at=210.0)
    kb._handle(ecodes.KEY_LEFTALT, 1, at=210.5)
    kb._handle(ecodes.KEY_LEFTMETA, 0, at=211.0)
    assert kb.held_during(both, 209.0, 210.7) and not kb.held_during(both, 211.2, 212.0)


def test_recording_a_combo_doesnt_set_off_keybinds(engine, ecodes):
    fired = []
    kb = Keyboard(engine.settings, lambda action, params: fired.append(action))
    kb.record()
    kb._handle(ecodes.KEY_LEFTMETA, 1)
    kb._handle(ecodes.KEY_J, 1)                            # Meta+J is the Text Request keybind
    st = kb.recorded()
    assert st["recording"] and st["keys"] == ["KEY_LEFTMETA", "KEY_J"]
    kb._handle(ecodes.KEY_J, 0)
    kb._handle(ecodes.KEY_LEFTMETA, 0)
    st = kb.recorded()
    assert not st["recording"] and st["keys"] == ["KEY_LEFTMETA", "KEY_J"] and fired == []
    kb._handle(ecodes.KEY_LEFTMETA, 1)
    kb._handle(ecodes.KEY_J, 1)
    assert fired == ["text_request"]                       # recorded: keybinds work again


def test_hold_keys_are_watched_even_with_keybinds_off(engine, ecodes):
    holds = []
    engine.settings.set("general.watch_keyboard_for_keybinds", False)
    kb = Keyboard(engine.settings, lambda a, p: None, holds.append)
    assert not kb.wanted()
    engine.settings.set("wake_word.hold_to_listen", True)
    assert kb.wanted()
    kb._handle(ecodes.KEY_RIGHTCTRL, 1)
    assert kb.holding(["KEY_RIGHTCTRL"])
    kb._handle(ecodes.KEY_RIGHTCTRL, 0)
    assert holds == [True, False]
    assert kb._buttons() == frozenset()
    engine.settings.set("wake_word.hold_keys", ["KEY_LEFTMETA", "BTN_SIDE"])
    assert kb._buttons() == {"BTN_SIDE"}                   # mice are only read for a button something uses


def test_keys_let_go_while_nobody_was_reading_dont_stay_held(engine, ecodes):
    class Dev:
        def __init__(self, keys):
            self.keys = keys

        def active_keys(self):
            return self.keys
    kb = Keyboard(engine.settings, lambda a, p: None)
    kb._handle(ecodes.KEY_RIGHTCTRL, 1)
    kb._resync([Dev([ecodes.KEY_A])])
    assert kb.held() == ["KEY_A"]
    assert not kb.held_during(["KEY_RIGHTCTRL"], time.time() + 1, time.time() + 2)


# ---------------------------------------------------------------------------- the GUI
def test_listen_for_names_choice_and_recording_the_keys(engine):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from jeeves.gui.pages_misc import GeneralPage
    from jeeves.gui.ui_kit import CustomButton
    app = QApplication.instance() or QApplication([])
    sent = []

    class D:
        def call(self, method, cb=None, err=None, **kw):
            if method == "keyboard.record":
                cb({"recording": True, "keys": [], "held": [], "status": "watching 1 keyboard(s)"})
            elif method == "keyboard.recorded":
                cb({"recording": False, "keys": ["KEY_LEFTMETA", "BTN_SIDE"], "held": [],
                    "status": "watching 1 keyboard(s)"})
            elif method == "keyboard.state":
                cb({"status": "watching 1 keyboard(s)", "held": [], "hold_keys": None})
    page = GeneralPage(D(), sent.append)
    s = engine.settings.effective()
    page.refresh(s, set())
    assert page.listen_mode.currentData() == "always" and page.hold_row.isHidden()
    s["wake_word"]["hold_to_listen"] = True
    page.refresh(s, set())
    assert page.listen_mode.currentData() == "hold" and not page.hold_row.isHidden()
    assert "sees your keys" in page.hold_status.text()
    assert sent == []                                      # showing settings never sends them back
    i = page.listen_mode.findData("off")
    page.listen_mode.setCurrentIndex(i)
    page.listen_mode.activated.emit(i)
    assert sent[-1] == {"wake_word.hold_to_listen": False, "wake_word.enabled": False}
    rec = page.hold_row.findChildren(CustomButton)[0]
    rec.click()
    deadline = time.time() + 3
    while time.time() < deadline and {"wake_word.hold_keys": ["KEY_LEFTMETA", "BTN_SIDE"]} not in sent:
        app.processEvents()
        time.sleep(0.01)
    assert sent[-1] == {"wake_word.hold_keys": ["KEY_LEFTMETA", "BTN_SIDE"]} and rec.isEnabled()
    page.refresh(s, {"wake_word.enabled"})                  # either setting declared in NixOS locks the choice
    assert not page.listen_mode.isEnabled()


def test_the_reading_loop_watches_keyboards_and_only_the_mice_it_needs(engine, ecodes, monkeypatch):
    import os
    from jeeves.daemon import keyboard as kbmod

    class Ev:
        def __init__(self, type_, code, value):
            self.type, self.code, self.value, self.t = type_, code, value, time.time()

        def timestamp(self):
            return self.t

    class Dev:
        def __init__(self, path, name, keys):
            self.path, self.name, self.keys = path, name, keys
            self.r, self.w = os.pipe()
            self.queue, self.down = [], []

        def capabilities(self):
            return {ecodes.EV_KEY: self.keys}

        def fileno(self):
            return self.r

        def send(self, *events):
            self.queue.extend(events)
            os.write(self.w, b"x")

        def read(self):
            os.read(self.r, 4096)
            out, self.queue = self.queue, []
            return out

        def active_keys(self):
            return list(self.down)

        def close(self):
            pass

    kbd = Dev("/dev/input/event0", "AT keyboard", [ecodes.KEY_A, ecodes.KEY_SPACE, ecodes.KEY_RIGHTCTRL])
    mouse = Dev("/dev/input/event1", "Gaming mouse", [ecodes.BTN_LEFT, ecodes.BTN_SIDE])
    own = Dev("/dev/input/event2", "jeeves-keyboard", [ecodes.KEY_A, ecodes.KEY_SPACE])
    devs = {d.path: d for d in (kbd, mouse, own)}
    monkeypatch.setattr(kbmod.evdev, "list_devices", lambda: list(devs))
    monkeypatch.setattr(kbmod.evdev, "InputDevice", lambda path: devs[path])
    monkeypatch.setattr(kbmod, "SETTINGS_EVERY", 0.05)
    holds = []
    engine.settings.set("wake_word.hold_to_listen", True)
    kb = Keyboard(engine.settings, lambda a, p: None, holds.append)
    kb.start()
    try:
        # the keyboard, not Jeeves' own device, and not the mouse: nothing uses its buttons
        assert wait_for(lambda: kb.status == "watching 1 keyboard(s)")
        kbd.send(Ev(ecodes.EV_KEY, ecodes.KEY_RIGHTCTRL, 1))
        assert wait_for(lambda: holds == [True])
        kbd.send(Ev(ecodes.EV_KEY, ecodes.KEY_RIGHTCTRL, 0))
        assert wait_for(lambda: holds == [True, False])
        engine.settings.set("wake_word.hold_keys", ["BTN_SIDE"])
        assert wait_for(lambda: kb.status == "watching 1 keyboard(s) and 1 mouse/mice")
        mouse.send(Ev(ecodes.EV_KEY, ecodes.BTN_SIDE, 1))
        assert wait_for(lambda: holds[-1] is True)
        mouse.send(Ev(ecodes.EV_SYN, ecodes.SYN_DROPPED, 0))   # events lost (the release among them)
        assert wait_for(lambda: holds[-1] is False)
        engine.settings.set("wake_word.hold_to_listen", False)
        engine.settings.set("general.watch_keyboard_for_keybinds", False)
        assert wait_for(lambda: kb.status == "disabled in settings")
    finally:
        kb.stop()
