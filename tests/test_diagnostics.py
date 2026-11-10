"""The Logs page's report: everything about how Jeeves runs here, nothing private."""
import time


def test_the_report_has_everything_and_no_secrets(engine, monkeypatch):
    from jeeves import config
    from jeeves.daemon import diagnostics
    from jeeves.models.backends import STATS
    engine.settings.set("jellyfin.url", "http://x:8096")
    config.save_secret("jellyfin_token", "SECRET-TOKEN")
    engine.settings.set("models.local_response.max_tokens", 4096)
    STATS["llm"].append({"at": time.time(), "model": "qwen3-1.7b", "took_s": 40.0, "think": True, "prompt_chars": 900,
                         "answer_chars": 40, "thought_chars": 9000, "prompt_tokens": 300, "made_tokens": 2500,
                         "read_tok_s": 90.0, "write_tok_s": 6.2, "stream": False})
    STATS["stt"].append({"at": time.time(), "model": "whisper-large-v3-turbo", "audio_s": 3.0, "took_s": 6.0,
                         "speed": 0.5, "ok": True, "busy_at_once": 2})
    engine.handle_text("Jeeves, what's 2+2?", wait=True)
    text = diagnostics.write(engine)
    for part in ("===== Computer =====", "===== GPU =====", "===== Models =====", "6.2 tok/s", "0.5x realtime",
                 "2 at once", "what's 2+2", "-> math", "agent jeeves", '"max_tokens": 4096', "Speed test"):
        assert part in text, part
    assert "SECRET-TOKEN" not in text
    from jeeves import paths
    assert (paths.state_dir() / "diagnostics.txt").read_text() == text


def test_model_server_output_is_always_read(tmp_path):
    from jeeves.models.backends import ManagedServer
    import subprocess
    srv = ManagedServer("chatty")
    script = tmp_path / "chatty.py"
    script.write_text("import sys,time\nfor i in range(20000): print('x'*80, i, file=sys.stderr)\ntime.sleep(30)\n")
    srv.proc = subprocess.Popen(["python3", str(script)], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    import threading
    threading.Thread(target=srv._drain, daemon=True).start()
    deadline = time.time() + 10
    while time.time() < deadline and not any("19999" in ln for ln in srv.tail):
        time.sleep(0.05)
    assert any("19999" in ln for ln in srv.tail) and len(srv.head) == 120     # (1.6 MB: never stuck on a full pipe)
    srv.proc.kill()


def test_what_stands_out_on_a_cpu_only_amd_machine(engine):
    from jeeves.daemon import diagnostics
    from jeeves.models.backends import STATS
    STATS["llm"].clear()
    STATS["stt"].clear()
    for _ in range(3):
        STATS["llm"].append({"at": 0, "model": "q", "took_s": 29, "think": False, "prompt_chars": 1, "answer_chars": 1,
                             "thought_chars": 0, "prompt_tokens": 1, "made_tokens": 111, "read_tok_s": 3,
                             "write_tok_s": 4.0, "stream": False})
        STATS["stt"].append({"at": 0, "model": "w", "audio_s": 5, "took_s": 10, "speed": 0.5, "ok": True,
                             "busy_at_once": 2})
    hw = {"gpus": [{"vendor": "amd", "vram_mb": 16304}], "llama_gpu": [], "gpu_usable": False}
    text = "\n".join(diagnostics.findings(engine, hw))
    assert 'services.jeeves.acceleration = "vulkan"' in text
    assert "4.0 tokens/s" in text and "0.5x realtime" in text and "several at once" in text


def test_a_gpu_build_that_finds_no_gpu_is_called_out(engine):
    from jeeves.daemon import diagnostics

    class Srv:
        head = ["warning: no usable GPU found, --gpu-layers option will be ignored"]

    class Inst:
        server = Srv()
    engine.models.instances["qwen3-1.7b"] = Inst()
    hw = {"gpus": [{"vendor": "amd", "vram_mb": 16304}], "llama_gpu": ["vulkan"], "gpu_usable": True}
    text = "\n".join(diagnostics.findings(engine, hw))
    assert "found no GPU when they started" in text and "follow the same nixpkgs" in text
    engine.models.instances.pop("qwen3-1.7b")
