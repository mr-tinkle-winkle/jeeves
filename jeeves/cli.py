"""The ``jeeves`` command.

    jeeves                                   open the settings GUI
    jeeves daemon                            run the daemon (systemd runs this)
    jeeves --manual_request=text             open the Text Request box
    jeeves --manual_request=text "Jeeves, set a timer for 5 minutes"
    jeeves --manual_request=voice --agent=jeeves
    jeeves --review                          Manual Response Review popup
    jeeves --abort                           stop all agents, release all inputs
    jeeves --toggle                          turn Jeeves off (stops the daemon) / back on (starts it)
    jeeves on | jeeves off
    jeeves dry-run "Jeeves, open OBS"        what WOULD happen
    jeeves status | history | models | functions | dictionary
    jeeves import-functions manifest.json    (shows the approval popup)
    jeeves export-functions [NAME ...] -o out.json
    jeeves doctor [--no-move]                check screen reading and Control Mode
    jeeves set models.stt.model whisper-base-en
    jeeves get models
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import ipc


def _print(obj: Any) -> None:
    if isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, indent=2, default=str))


def _value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _describe(entry: dict[str, Any]) -> str:
    lines = [f"Agent:     {entry.get('agent')}", f"Request:   {entry.get('text')}",
             f"Function:  {entry.get('function')}  {json.dumps(entry.get('args'))}",
             f"Confidence:{entry.get('confidence')}"]
    for t in entry.get("trace", []):
        kind = t.get("kind")
        if kind in ("stage",):
            continue
        rest = {k: v for k, v in t.items() if k not in ("t", "kind")}
        lines.append(f"  {t.get('t', 0):6.2f}s  {kind:<14} {json.dumps(rest, default=str)[:300]}")
    if entry.get("response"):
        lines.append(f"Response:  {entry['response']}")
    if entry.get("error"):
        lines.append(f"Error:     {entry['error']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="jeeves", description="Local-first voice assistant")
    p.add_argument("--manual_request", "--manual-request", choices=["text", "voice"])
    p.add_argument("--agent")
    p.add_argument("--abort", action="store_true")
    p.add_argument("--review", action="store_true")
    p.add_argument("--toggle", action="store_true", help="turn Jeeves (every AI) on or off")
    p.add_argument("command", nargs="?")
    p.add_argument("args", nargs="*")
    p.add_argument("-o", "--output")
    p.add_argument("--json", action="store_true", help="raw JSON output")
    p.add_argument("--popups", action="store_true", help="overlay: run the popups process")
    p.add_argument("--no-move", action="store_true", help="doctor: don't test moving the mouse")
    a = p.parse_args(argv)

    try:
        if a.abort:
            _print(ipc.call("abort"))
            return 0
        if a.toggle:
            from . import service
            print("Jeeves is " + ("on" if service.toggle() else "off"))
            return 0
        if a.review:
            ipc.call("ui.review")
            return 0
        if a.manual_request == "voice":
            _print(ipc.call("request.voice", agent=a.agent))
            return 0
        if a.manual_request == "text":
            text = " ".join(([a.command] if a.command else []) + a.args).strip()
            if not text:
                ipc.call("ui.text_request")
            else:
                _print(ipc.call("request.text", text=text, agent=a.agent))
            return 0

        cmd = a.command
        if cmd in (None, "gui"):
            from .gui.app import main as gui_main
            return gui_main()
        if cmd == "daemon":
            from .daemon.server import main as daemon_main
            daemon_main()
            return 0
        if cmd == "overlay":
            from .overlay import main as overlay_main
            return overlay_main(popups=a.popups)
        if cmd == "screenshot-portal":
            if not a.args:
                p.error("screenshot-portal needs an output path")
            from .screenshot_portal import main as portal_main
            return portal_main(a.args[0])
        if cmd == "doctor":
            from .daemon.doctor import format_report
            checks = ipc.call("doctor", move_test=not a.no_move, timeout=120)
            _print(checks if a.json else format_report(checks))
            return 0 if all(c["ok"] for c in checks) else 1
        if cmd in ("say", "request"):
            _print(ipc.call("request.text", text=" ".join(a.args), agent=a.agent))
        elif cmd in ("dry-run", "dry_run"):
            entry = ipc.call("request.dry_run", text=" ".join(a.args), agent=a.agent, timeout=120)
            _print(entry if a.json else _describe(entry))
        elif cmd in ("on", "off"):
            from . import service
            service.power_on() if cmd == "on" else service.power_off()
            print(f"Jeeves is {cmd}")
        elif cmd == "status":
            _print(ipc.call("status"))
        elif cmd == "history":
            items = ipc.call("history.list", limit=int(a.args[0]) if a.args else 10)
            _print(items if a.json else "\n\n".join(_describe(e) for e in reversed(items)))
        elif cmd == "models":
            st = ipc.call("models.status")
            if a.args and a.args[0] == "download":
                for mid in a.args[1:]:
                    ipc.call("models.download", id=mid)
                print("downloading in the daemon; watch progress in the GUI (Models)")
            elif a.json:
                _print(st)
            else:
                for m in st["catalog"]:
                    print(f"{'*' if m['installed'] else ' '} {m['kind']:<6} {m['id']:<34} {m['name']}")
                print("\nIn use:", json.dumps(st["kinds"], indent=1))
        elif cmd == "functions":
            for f in ipc.call("functions.list"):
                print(f"{f['kind']:<7} {f['name']:<24} {f['source']:<12} {f['description'][:70]}")
        elif cmd == "dictionary":
            print(ipc.call("functions.dictionary", agent=a.agent))
        elif cmd == "import-functions":
            if not a.args:
                p.error("import-functions needs a manifest file")
            with open(a.args[0]) as f:
                manifest = json.load(f)
            res = ipc.call("functions.import", manifest=manifest)
            print(f"Sent {len(res['items'])} function(s) from '{res['app']}' for approval -- "
                  "accept or deny them in the popup.")
        elif cmd == "export-functions":
            manifest = ipc.call("functions.export", names=a.args or None)
            text = json.dumps(manifest, indent=2)
            if a.output:
                with open(a.output, "w") as f:
                    f.write(text)
            else:
                print(text)
        elif cmd == "get":
            _print(ipc.call("settings.get", path=a.args[0] if a.args else "")["value"])
        elif cmd == "set":
            if len(a.args) != 2:
                p.error("set needs PATH VALUE")
            ipc.call("settings.set", changes={a.args[0]: _value(a.args[1])})
        elif cmd == "reset":
            ipc.call("settings.reset", path=a.args[0])
        else:
            p.error(f"unknown command '{cmd}'")
    except ipc.DaemonUnavailable as exc:
        from . import config
        if not config.Settings().get("general.enabled", True):
            print("jeeves: Jeeves is off -- turn it on with `jeeves on` (or the GUI)", file=sys.stderr)
        else:
            print(f"jeeves: {exc}", file=sys.stderr)
        return 2
    except ipc.DaemonError as exc:
        print(f"jeeves: {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, PermissionError) as exc:      # starting/stopping the daemon
        print(f"jeeves: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
