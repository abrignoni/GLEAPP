"""Native desktop shell for GLEAPP (pywebview + in-process Flask).

Runs entirely offline: a Flask server bound to 127.0.0.1 on a free port, shown
in an OS webview window (WebView2 on Windows).  This is the module PyInstaller
bundles into ``GLEAPP.exe``.

    python -m gleapp.desktop [CASE_DIR]
    gleapp-desktop            (console script)
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

from werkzeug.serving import make_server

from . import __version__
from .web.app import create_app

HOST = "127.0.0.1"

# Set to any non-empty value to open the window, wait for the launcher to load,
# read its title through the webview, close it, and exit. See _Smoke.
SMOKE_ENV = "GLEAPP_DESKTOP_SMOKE"


class _Smoke:
    """The desktop entry point run end to end without a person at the window.

    CI runs ``python gleappGUI.py`` with ``GLEAPP_DESKTOP_SMOKE=1`` on a real desktop
    session, which exercises what a user gets after ``pip install -r requirements.txt``:
    the pywebview import, the platform backend (pythonnet loading .NET and the WebView2
    control on Windows), and Flask serving the launcher into the window. ``run`` is
    handed to ``webview.start`` as its thread function.

    ``ok`` is True only if the page loaded within the timeout and carried GLEAPP's
    title, and, on Windows, if the renderer is WebView2 (``edgechromium``): without
    the WebView2 runtime pywebview falls back to the legacy MSHTML control, where the
    static title still loads and the interface does not.
    """

    def __init__(self, webview_module, *, platform: str = sys.platform,
                 timeout: float = 60.0) -> None:
        self._webview = webview_module
        self._platform = platform
        self._timeout = timeout
        self.ok = False

    def run(self, window) -> None:
        loaded = window.events.loaded.wait(self._timeout)
        title = window.evaluate_js("document.title") if loaded else None
        renderer = getattr(self._webview, "renderer", None)
        self.ok = bool(loaded) and "GLEAPP" in str(title) and (
            self._platform != "win32" or renderer == "edgechromium")
        print(f"desktop smoke: loaded={loaded} title={title!r} renderer={renderer!r} "
              f"ok={self.ok}", flush=True)
        window.destroy()


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
    # Imported here rather than at module level so importing gleapp.desktop never
    # needs a GUI lib. --version is answered in packaging/entrypoint.py before this runs.
    try:
        import webview
    except ModuleNotFoundError as exc:
        # The module is imported as ``webview`` but the package is ``pywebview``, and
        # PyPI's package named ``webview`` is a different project, so a bare "No module
        # named 'webview'" sends people to the wrong install. Only pywebview itself
        # being absent gets this message; a dependency missing from an installed
        # pywebview carries its own name and reports itself.
        if exc.name != "webview":
            raise
        print(
            "error: the desktop window needs pywebview, which is not installed in this\n"
            "Python environment. From the GLEAPP folder, run:\n"
            "    pip install -r requirements.txt\n"
            'pywebview is on that list. The PyPI package named "webview" is a different\n'
            "project and is not the one GLEAPP uses. To open the same interface in a\n"
            "browser instead:\n"
            "    python gleapp.py web",
            file=sys.stderr,
        )
        return 1

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

    def _on_closed() -> None:
        # final snapshot of the open case, then shut the local server down
        try:
            close = app.config.get("STATE", {}).get("close_current")
            if close:
                close()
        except Exception:  # noqa: BLE001
            pass
        server.stop()

    smoke = _Smoke(webview) if os.environ.get(SMOKE_ENV) else None

    try:
        window = webview.create_window(
            f"GLEAPP {__version__} — Media Forensics",
            f"http://{HOST}:{port}/",
            width=1440, height=900, min_size=(900, 600),
            text_select=True, confirm_close=False,
        )
        window.events.closed += _on_closed
        # gui=None lets pywebview pick: edgechromium on Windows, cocoa on macOS,
        # gtk or qt on Linux.
        if smoke is None:
            webview.start()
            return 0
        webview.start(smoke.run, (window,))
        return 0 if smoke.ok else 1
    except webview.WebViewException as exc:
        # pywebview's own words name the cause: on Linux, that neither GTK nor Qt
        # bindings are installed, which pip does not do by default.
        print(
            f"error: the desktop window could not start: {exc}\n"
            "See the README's Desktop app section for what the window needs on this\n"
            "platform. To open the same interface in a browser instead:\n"
            "    python gleapp.py web",
            file=sys.stderr,
        )
        server.stop()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
