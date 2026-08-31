"""Native desktop shell for GLEAPP (pywebview + in-process Flask).

Runs entirely offline: a Flask server bound to 127.0.0.1 on a free port, shown
in an OS webview window (WebView2 on Windows).  This is the module PyInstaller
bundles into ``GLEAPP.exe``.

    python -m gleapp.desktop [CASE_DIR]
    gleapp-desktop            (console script)
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from pathlib import Path

from werkzeug.serving import make_server

from . import __version__
from .web.app import create_app

HOST = "127.0.0.1"


def _free_port() -> int:
    s = socket.socket()
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server(threading.Thread):
    def __init__(self, app, port: int):
        super().__init__(daemon=True)
        self.srv = make_server(HOST, port, app, threaded=True)
        self.port = port

    def run(self) -> None:
        self.srv.serve_forever()

    def stop(self) -> None:
        self.srv.shutdown()


def _wait_until_up(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main(argv: list[str] | None = None) -> int:
    import webview  # imported here so `--version` etc. don't need a GUI lib

    argv = sys.argv[1:] if argv is None else argv
    case_dir = argv[0] if argv and not argv[0].startswith("-") else None

    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    app = create_app(case_dir, native=True)
    port = _free_port()
    server = _Server(app, port)
    server.start()
    if not _wait_until_up(port):
        print("error: local server failed to start", file=sys.stderr)
        return 1

    window = webview.create_window(
        f"GLEAPP {__version__} — Media Forensics",
        f"http://{HOST}:{port}/",
        width=1440, height=900, min_size=(900, 600),
        text_select=True, confirm_close=False,
    )

    def _on_closed() -> None:
        # final snapshot of the open case, then shut the local server down
        try:
            close = app.config.get("STATE", {}).get("close_current")
            if close:
                close()
        except Exception:  # noqa: BLE001
            pass
        server.stop()

    window.events.closed += _on_closed
    # gui=None lets pywebview pick: edgechromium on Windows, gtk/qt on Linux.
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
