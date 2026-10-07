"""Steam games and setups."""
from types import SimpleNamespace as W

from jeeves.functions import apps


def test_installed_steam_games_by_name(tmp_path, monkeypatch):
    lib = tmp_path / "games"
    (lib / "steamapps").mkdir(parents=True)
    root = tmp_path / "steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "libraryfolders.vdf").write_text(f'"libraryfolders" {{ "0" {{ "path" "{lib}" }} }}')
    for appid, name in (("1245620", "ELDEN RING"), ("367520", "Hollow Knight"), ("1493710", "Proton Experimental"),
                        ("271590", "Grand Theft Auto V")):
        (lib / "steamapps" / f"appmanifest_{appid}.acf").write_text(
            f'"AppState" {{ "appid" "{appid}" "name" "{name}" }}')
    monkeypatch.setattr(apps, "STEAM_ROOTS", [str(root)])
    games = apps.steam_games()
    assert [g["name"] for g in games] == ["ELDEN RING", "Grand Theft Auto V", "Hollow Knight"]
    assert apps.match_game("elden ring", games)["appid"] == "1245620"
    assert apps.match_game("hollow night", games)["appid"] == "367520"
    assert apps.match_game("gta", games)["appid"] == "271590"
    assert apps.match_game("minecraft", games) is None


class FakeDesktop:
    DesktopUnavailable = RuntimeError

    def __init__(self, wins):
        self.wins = wins
        self.placed, self.activated = [], []

    def windows(self):
        return list(self.wins)

    def window_info(self, w):
        return {"keepAbove": w.app == "obs", "minimized": False}

    def place(self, w, x, y, width, height, workspace="", state=None):
        self.placed.append((w.app, x, y, width, height, workspace, dict(state or {})))

    def activate(self, w):
        self.activated.append(w.app)


def test_a_setup_saves_every_window_and_puts_it_back(monkeypatch, tmp_path):
    monkeypatch.setattr(apps, "desktop_exec", lambda app: [f"/bin/{app}"] if app == "obs" else None)
    monkeypatch.setattr(apps, "_cmdline", lambda pid: ["vesktop", "--start-minimized"])
    dk = FakeDesktop([W(id="1", app="vesktop", title="Discord", x=1920, y=0, w=1280, h=1000, workspace="2",
                        focused=False, pid=11),
                      W(id="2", app="obs", title="OBS 31", x=0, y=0, w=1920, h=1080, workspace="1", focused=True,
                        pid=12)])
    saved = apps.snapshot(dk)
    assert saved[1]["state"]["keepAbove"] and saved[1]["launch"] == ["/bin/obs"]
    assert saved[0]["launch"] == ["vesktop", "--start-minimized"]
    # later: Discord moved, OBS closed (it gets started and waited for)
    dk.wins = [W(id="9", app="vesktop", title="Discord", x=5, y=5, w=600, h=400, workspace="1", focused=True, pid=11)]
    started = []

    def popen(argv, **kw):
        started.append(argv)
        dk.wins.append(W(id="10", app="obs", title="OBS 31", x=0, y=0, w=800, h=600, workspace="1", focused=False,
                         pid=13))
    monkeypatch.setattr(apps.subprocess, "Popen", popen)
    monkeypatch.setattr(apps, "which", lambda name: name)
    monkeypatch.setattr(apps.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(apps.time, "sleep", lambda s: None)
    ctx = W(think=lambda *a, **k: None, check_cancelled=lambda: None)
    placed, failed = apps.restore(ctx, dk, saved)
    assert started == [["/bin/obs"]] and placed == 2 and failed == []
    assert ("vesktop", 1920, 0, 1280, 1000, "2", {}) in [(a, x, y, w, h, ws, {k: v for k, v in st.items() if v})
                                                       for a, x, y, w, h, ws, st in dk.placed]
    assert dk.activated == ["obs"]                     # the window you were using is in front again


def test_setups_function_saves_lists_and_restores(engine, monkeypatch):
    from jeeves.daemon import desktop as real
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    dk = FakeDesktop([W(id="1", app="kitty", title="nvim", x=0, y=0, w=900, h=700, workspace="1", focused=True,
                        pid=1)])
    for name in ("windows", "window_info", "place", "activate"):
        monkeypatch.setattr(real, name, getattr(dk, name))
    monkeypatch.setattr(apps, "desktop_exec", lambda app: ["kitty"])
    said = []
    engine.speak = lambda ctx, t: said.append(t)

    def call(**args):
        ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("x", "jeeves", "text"))
        return ctx.call("setups", **args)
    call(action="save", setup="Coding")
    call(action="list")
    call(action="restore", setup="coding")
    assert said[0].startswith("Saved the coding setup: 1 windows") and said[1] == "Your setups: coding."
    assert said[2] == "You're set up for coding." and dk.placed[0][:5] == ("kitty", 0, 0, 900, 700)
