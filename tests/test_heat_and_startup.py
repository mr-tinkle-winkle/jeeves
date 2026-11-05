"""What kept the CPU hot and Jeeves slow to start: whisper on 28 CPU threads beside an idle GPU,
core dumps of crashed model servers, a server crashing over and over, and `jeeves on` waiting on
systemd."""
import subprocess
import sys

import pytest


def test_whisper_uses_the_gpu_when_there_is_one_and_at_most_8_threads(monkeypatch):
    from jeeves.models import backends
    from jeeves.models.catalog import CATALOG
    started = []
    monkeypatch.setattr(backends, "_find", lambda *n: "/bin/whisper-server")
    monkeypatch.setattr(backends.ManagedServer, "start", lambda self, cmd, health, **k: started.append(cmd))
    entry = next(e for e in CATALOG if e.engine == "whisper.cpp")
    backends.WhisperCppSTT(entry, threads=28, gpu=True).load()
    backends.WhisperCppSTT(entry, threads=28, gpu=False).load()
    gpu, cpu = started
    assert "-ng" not in gpu and "-ng" in cpu
    assert gpu[gpu.index("-t") + 1] == "8"


def test_the_manager_lets_whisper_use_a_gpu_it_has_vram_for(engine, monkeypatch):
    m = engine.models
    monkeypatch.setattr(m, "budget", lambda: {"whisper_gpu": [], "vram_mb": 24000, "gpu_share": 1.0})
    seen = {}
    from jeeves.models import manager
    class S:
        def loaded(self):
            return True
    monkeypatch.setattr(manager, "make_stt", lambda entry, threads, gpu: seen.setdefault("gpu", gpu) and S())
    monkeypatch.setattr(m, "_check", lambda kind, mid: object())
    monkeypatch.setattr(m, "_instance", lambda entry, make: make())
    m.stt()
    assert seen["gpu"] is True


def test_model_servers_never_leave_core_dumps():
    from jeeves.models import backends
    out = subprocess.run([sys.executable, "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_CORE))"],
                         capture_output=True, text=True, preexec_fn=backends._child_setup)
    assert out.stdout.strip() == "(0, 0)"


def test_speech_recognition_that_keeps_crashing_rests(engine, monkeypatch):
    from jeeves.models.backends import BackendError
    from jeeves.models.manager import ModelUnavailable
    calls = []

    class Broken:
        def transcribe(self, *a, **k):
            calls.append(1)
            raise BackendError("Remote end closed connection without response")

        def unload(self):
            pass
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: Broken())
    notices = []
    engine.publish = lambda t, d: notices.append((t, d))
    for _ in range(3):
        with pytest.raises(ModelUnavailable):
            engine.transcribe(b"\0" * 3200)
    n = len(calls)
    with pytest.raises(ModelUnavailable, match="resting"):
        engine.transcribe(b"\0" * 3200)
    assert len(calls) == n and any(t == "notice" for t, _d in notices)        # not even tried while resting


def test_jeeves_on_says_why_it_did_not_start(monkeypatch):
    from jeeves import service
    monkeypatch.setattr(service, "running", lambda: False)
    monkeypatch.setattr(service, "has_unit", lambda: True)
    ran = []

    def fake_run(cmd, **kw):
        ran.append(cmd)

        class R:
            returncode = 0
            stderr = ""
            stdout = "ActiveState=failed SubState=failed Result=exit-code" if "show" in cmd else "boom: no GPU"
        return R()
    monkeypatch.setattr(service.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as err:
        service.start(timeout=0.5)
    assert ["systemctl", "--user", "start", "--no-block", "jeeves.service"] in ran
    assert "ActiveState=failed" in str(err.value) and "boom: no GPU" in str(err.value)


def test_ping_is_answered_even_when_every_worker_is_busy():
    import asyncio

    from jeeves.daemon.server import Server
    srv = Server.__new__(Server)
    srv.subscribers, srv.methods = {}, {}

    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"id": 1, "method": "ping"}\n')
        reader.feed_eof()
        out = []

        class W:
            def write(self, b):
                out.append(b)

            async def drain(self):
                pass

            def close(self):
                pass
        await srv.handle(reader, W())
        return out
    assert b'"pong"' in asyncio.run(go())[0]
