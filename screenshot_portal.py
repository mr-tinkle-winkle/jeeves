"""Take a screenshot through the XDG desktop portal (org.freedesktop.portal.Screenshot).

KWin only lets approved programs (Spectacle itself) take silent screenshots, so
on KDE this is the reliable route for everyone else; it also works on GNOME and
most wlroots desktops with xdg-desktop-portal-wlr. The first time, the desktop
may ask whether Jeeves may take screenshots.

    jeeves screenshot-portal /tmp/out.png      (exit 0 on success)

Runs as its own short-lived process because receiving the portal's Response
signal needs a Qt event loop, which the daemon doesn't have.
"""
from __future__ import annotations

import shutil
import sys
import uuid
from urllib.parse import unquote, urlparse


def main(dest: str, timeout_ms: int = 20000) -> int:
    from PySide6.QtCore import SLOT, QCoreApplication, QObject, QTimer, Slot
    from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage

    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        print("no D-Bus session bus", file=sys.stderr)
        return 2
    token = "jeeves" + uuid.uuid4().hex[:12]
    sender = bus.baseService().lstrip(":").replace(".", "_")
    request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    result = {"code": 3, "error": "the portal didn't answer"}

    class Receiver(QObject):
        @Slot(QDBusMessage)
        def response(self, msg: QDBusMessage) -> None:
            args = msg.arguments()
            code = int(args[0]) if args else 2
            results = args[1] if len(args) > 1 else {}
            uri = results.get("uri", "") if isinstance(results, dict) else ""
            if code == 0 and uri:
                try:
                    shutil.copyfile(unquote(urlparse(uri).path), dest)
                    result.update(code=0, error="")
                except OSError as exc:
                    result.update(code=4, error=f"couldn't copy the screenshot: {exc}")
            else:
                result.update(code=5, error="screenshot refused or cancelled" if code == 1 else
                              f"portal returned {code}")
            app.quit()

    receiver = Receiver()
    # subscribe BEFORE asking, so a fast answer isn't missed
    bus.connect("", request_path, "org.freedesktop.portal.Request", "Response", receiver,
                SLOT("response(QDBusMessage)"))
    portal = QDBusInterface("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                            "org.freedesktop.portal.Screenshot", bus)
    if not portal.isValid():
        print("the desktop portal isn't available", file=sys.stderr)
        return 2
    reply = portal.call("Screenshot", "", {"handle_token": token, "interactive": False})
    if reply.type() == QDBusMessage.ErrorMessage:
        print(f"portal error: {reply.errorMessage()}", file=sys.stderr)
        return 2
    QTimer.singleShot(timeout_ms, app.quit)
    app.exec()
    if result["code"] != 0:
        print(result["error"], file=sys.stderr)
    return int(result["code"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
