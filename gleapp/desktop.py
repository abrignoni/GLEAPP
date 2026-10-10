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
    static title still loads and the interface does not. With WebView2 it is also
    True only if SmartScreen was read back off and the window runs on an environment
    that keeps crash reports on the machine (``privacy`` is ``_webview2_privacy.state``).
    """

    def __init__(self, webview_module, *, platform: str = sys.platform,
                 timeout: float = 60.0, privacy: dict | None = None) -> None:
        self._webview = webview_module
        self._platform = platform
        self._timeout = timeout
        self._privacy = privacy or {}
        self.ok = False

    def run(self, window) -> None:
        loaded = window.events.loaded.wait(self._timeout)
        title = window.evaluate_js("document.title") if loaded else None
        renderer = getattr(self._webview, "renderer", None)
        smartscreen = self._privacy.get("smartscreen")
        crash_reports = self._privacy.get("crash_reports")
        self.ok = bool(loaded) and "GLEAPP" in str(title) and (
            self._platform != "win32" or (
                renderer == "edgechromium" and smartscreen is False
                and crash_reports == "local"))
        print(f"desktop smoke: loaded={loaded} title={title!r} renderer={renderer!r} "
              f"smartscreen={smartscreen!r} crash_reports={crash_reports!r} "
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


def _web_command() -> str:
    """The command that opens the same interface in a browser, as this copy runs.

    From a source checkout that is ``python gleapp.py web``. A frozen build has no
    gleapp.py, so it names its own executable, which accepts ``web`` too.
    """
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return f'"{exe}" web' if " " in exe else f"{exe} web"
    return "python gleapp.py web"


def _has_terminal() -> bool:
    """True when someone started GLEAPP from a terminal and can read and stop it there.

    The browser fallback prints its address and runs until Ctrl-C, so it is only
    offered where both of those reach a person. A windowed build started from a file
    manager has no stream to print to, and a server nobody can stop would outlive the
    browser tab.
    """
    stream = sys.stderr
    try:
        return stream is not None and stream.isatty()
    except (AttributeError, ValueError):
        return False


def _close_case(app) -> None:
    """Take the final snapshot of the open case, if one is open."""
    try:
        close = app.config.get("STATE", {}).get("close_current")
        if close:
            close()
    except Exception:  # noqa: BLE001
        pass


def _wait_for_interrupt(server: "_Server") -> None:
    """Block until Ctrl-C or until the server stops."""
    while server.is_alive():
        server.join(0.5)


def _serve_in_browser(case_dir: str | None) -> int:
    """Serve the interface to the default browser, as ``gleapp web`` does.

    Used when the desktop window cannot start. The app is created without the native
    flag, so the file pickers fall back to typed paths as they do in ``gleapp web``.
    """
    import webbrowser

    app = create_app(case_dir)
    port = _free_port()
    server = _Server(app, port)
    server.start()
    if not _wait_until_up(port):
        print("error: local server failed to start", file=sys.stderr)
        return 1
    url = f"http://{HOST}:{port}/"
    print(f"GLEAPP review UI -> {url}  (Ctrl-C to stop)\n"
          f"To open it in a browser directly next time, run:\n    {_web_command()}",
          file=sys.stderr, flush=True)
    webbrowser.open(url)
    try:
        _wait_for_interrupt(server)
    except KeyboardInterrupt:
        pass
    finally:
        _close_case(app)
        server.stop()
    return 0


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
        _close_case(app)
        server.stop()

    # Windows: SmartScreen off and crash reports kept here, before the window exists.
    from . import _webview2_privacy
    _webview2_privacy.install()

    smoke = (_Smoke(webview, privacy=_webview2_privacy.state)
             if os.environ.get(SMOKE_ENV) else None)

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
        # bindings are installed, which pip does not do by default. The Linux release
        # binary bundles neither, so there the browser is the only interface.
        server.stop()
        print(f"error: the desktop window could not start: {exc}", file=sys.stderr)
        if smoke is None and _has_terminal():
            print("Opening the same interface in your browser instead.", file=sys.stderr)
            return _serve_in_browser(case_dir)
        print(
            "See the README's Desktop app section for what the window needs on this\n"
            "platform. To open the same interface in a browser instead:\n"
            f"    {_web_command()}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
