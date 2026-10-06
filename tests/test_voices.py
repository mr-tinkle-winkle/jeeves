"""Voice catalog, per-agent voice style and how it reaches the engines."""
import subprocess


def test_lots_of_voices_and_presets_share_downloads():
    from jeeves.models import catalog
    from jeeves.models.download import model_dir
    voices = catalog.of_kind("voice")
    assert len(voices) > 100
    assert len({m.engine for m in voices}) == 3
    spike = catalog.get("piper-en_GB-semaine-medium-spike")
    base = catalog.get("piper-en_GB-semaine-medium")
    assert spike.speaker == "spike" and model_dir(spike) == model_dir(base)
    assert catalog.get("piper-en_US-libritts_r-medium").speakers == 904
    assert catalog.get("kokoro-bm_lewis") and catalog.get("kokoro-ff_siwis")


def test_style_is_clamped():
    from jeeves.models.voicefx import style_of
    st = style_of({"voice_style": {"speed": 9, "pitch": -40, "effect": "nonsense", "blend_amount": 3}})
    assert st["speed"] == 2.0 and st["pitch"] == -12 and st["effect"] == "none" and st["blend_amount"] == 1.0
    assert style_of({})["speed"] == 1.0


def test_piper_gets_speaker_speed_and_expressiveness(monkeypatch, tmp_path):
    import json
    from jeeves.models import backends, catalog
    voice = catalog.get("piper-en_GB-vctk-medium")
    d = tmp_path / "vctk"
    d.mkdir()
    (d / "voice.onnx.json").write_text(json.dumps({"audio": {"sample_rate": 22050},
                                                   "speaker_id_map": {"p225": 0, "p245": 17}}))
    monkeypatch.setattr(backends, "model_dir", lambda v: d)
    monkeypatch.setattr(backends, "_find", lambda *n: "/bin/piper")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, b"\0\0" * 100, b"")
    monkeypatch.setattr(backends.subprocess, "run", fake_run)
    pcm, rate = backends.PiperTTS().synth("hi", voice, {"speaker": "P245", "speed": 1.25, "expressiveness": 0.9})
    cmd = seen["cmd"]
    assert cmd[cmd.index("--speaker") + 1] == "17"
    assert cmd[cmd.index("--length_scale") + 1] == "0.800" and cmd[cmd.index("--noise_scale") + 1] == "0.900"
    preset = catalog.get("piper-en_GB-vctk-medium-p245")
    backends.PiperTTS().synth("hi", preset, {})
    assert seen["cmd"][seen["cmd"].index("--speaker") + 1] == "17"   # a preset brings its own speaker


def test_effects_go_through_sox(monkeypatch):
    from jeeves.models import voicefx
    monkeypatch.setattr(voicefx, "available", lambda: True)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, b"\1\0" * 10, b"")
    monkeypatch.setattr(voicefx.subprocess, "run", fake_run)
    assert voicefx.apply(b"\0\0" * 10, 22050, {"pitch": -3, "effect": "radio"}) == b"\1\0" * 10
    cmd = seen["cmd"]
    assert cmd[0] == "sox" and "pitch" in cmd and "-300" in cmd and "lowpass" in cmd and "norm" in cmd
    seen.clear()
    assert voicefx.apply(b"x", 22050, {"pitch": 0, "effect": "none"}) == b"x" and not seen   # nothing to do


def test_choosing_a_voice_picks_its_engine(engine, monkeypatch):
    from jeeves.models import download, manager
    monkeypatch.setattr(manager, "is_installed", lambda e: True)
    monkeypatch.setattr(download, "is_installed", lambda e: True)
    engine.settings.set("models.tts.model", "piper")
    agent = dict(engine.agents()["jeeves"], models={"tts_voice": "kokoro-bm_george"})
    monkeypatch.setattr(engine.models, "_loaded", lambda inst, kind: inst)
    tts, voice = engine.models.tts(agent)
    assert type(tts).__name__ == "KokoroTTS" and voice.id == "kokoro-bm_george"
