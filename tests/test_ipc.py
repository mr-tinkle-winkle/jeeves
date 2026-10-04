import asyncio
import threading
import time

from jeeves import ipc


def start_server():
    from jeeves.daemon.server import Server
    srv = Server(start_io=False)
    loop = asyncio.new_event_loop()
    stop = threading.Event()

    async def run():
        from jeeves import paths
        paths.ensure_all()
        server = await asyncio.start_unix_server(srv.handle, path=str(paths.socket_path()))
        srv.loop = asyncio.get_running_loop()
        async with server:
            while not stop.is_set():
                await asyncio.sleep(0.05)

    t = threading.Thread(target=lambda: loop.run_until_complete(run()), daemon=True)
    t.start()
    for _ in range(100):
        try:
            ipc.call("ping")
            break
        except ipc.DaemonUnavailable:
            time.sleep(0.05)
    return srv, stop


def test_requests_events_and_locks(system_settings):
    system_settings({"summary": {"minutes": 15}})
    srv, stop = start_server()
    try:
        assert ipc.call("ping") == "pong"
        got = []
        sub = ipc.Subscriber(["settings", "history"], lambda topic, data: got.append(topic)).start()
        time.sleep(0.3)
        ipc.call("settings.set", changes={"memory.recent_count": 5})
        assert ipc.call("settings.get", path="memory.recent_count")["value"] == 5
        res = ipc.call("settings.get")
        assert "summary.minutes" in res["locked"]
        try:
            ipc.call("settings.set", changes={"summary.minutes": 20})
            raise AssertionError("locked key changed")
        except ipc.DaemonError as exc:
            assert "NixOS" in str(exc)
        entry = ipc.call("request.dry_run", text="Jeeves, set a timer for 3 minutes")
        assert entry["function"] == "timers"
        ipc.call("request.text", text="Jeeves, set a timer for 3 minutes")
        for _ in range(100):
            if "history" in got:
                break
            time.sleep(0.05)
        assert "settings" in got and "history" in got
        try:
            ipc.call("no.such.method")
            raise AssertionError
        except ipc.DaemonError as exc:
            assert "unknown method" in str(exc)
        sub.stop()
    finally:
        stop.set()
        srv.engine.timers.stop()


def test_cli_reports_missing_daemon(capsys):
    from jeeves.cli import main
    assert main(["status"]) == 2
    assert "isn't running" in capsys.readouterr().err


def test_cli_toggle(capsys):
    srv, stop = start_server()
    try:
        from jeeves.cli import main
        assert main(["--toggle"]) == 0
        assert "off" in capsys.readouterr().out
        assert ipc.call("power.get") is False
        assert main(["on"]) == 0
        assert "on" in capsys.readouterr().out
    finally:
        stop.set()
        srv.engine.timers.stop()
