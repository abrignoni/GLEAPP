"""The LEAPP-style entry points at the repo root must keep working from a source checkout."""

import importlib
import importlib.util
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_root_cli_shim_runs_from_source():
    """``python gleapp.py --version`` is the LEAPP muscle memory; it must print the version."""
    from gleapp import __version__

    r = subprocess.run([sys.executable, str(ROOT / "gleapp.py"), "--version"],
                       capture_output=True, text=True, cwd=ROOT, check=False)
    assert r.returncode == 0, r.stderr
    assert __version__ in r.stdout


def test_root_gui_shim_targets_the_desktop_entry():
    """gleappGUI.py must route to the same main the gleapp-desktop console script uses.

    Importing it must not need pywebview, because desktop.py imports it lazily
    inside main(). That is what lets this run in the plain CI environment without
    opening a window.
    """
    spec = importlib.util.spec_from_file_location("gleappGUI", ROOT / "gleappGUI.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from gleapp.desktop import main as desktop_main

    assert mod.main is desktop_main


def test_requirements_carry_the_desktop_window_library():
    """``pip install -r requirements.txt`` alone must be enough for ``python gleappGUI.py``.

    A user who installed the requirements and then met ``No module named 'webview'``
    installed PyPI's ``webview``, a different project with no wheels, and got a C
    build failure for it. So pywebview is a requirement, not an extra, in both places
    dependencies are declared, and the environment CI built from requirements.txt
    must import it.
    """
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    reqs = [ln.strip() for ln in lines
            if ln.strip() and not ln.startswith("#") and not ln.startswith("whl_files/")]
    names = {re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", r).group(0).lower() for r in reqs}
    assert "pywebview" in names

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dependencies = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "pywebview" in dependencies

    assert importlib.import_module("webview").__name__ == "webview"


def test_desktop_without_pywebview_names_the_package_to_install(monkeypatch, capsys):
    """Without pywebview the desktop entry must say what to install, and exit 1.

    The module is imported as ``webview`` but the package is ``pywebview``, and the
    package PyPI calls ``webview`` is a different project, so a bare
    ``No module named 'webview'`` points people at the wrong install. A None entry
    in sys.modules makes the import raise the same ModuleNotFoundError, with the
    same ``name``, as a Python where pywebview was never installed.
    """
    monkeypatch.setitem(sys.modules, "webview", None)
    from gleapp.desktop import main as desktop_main

    assert desktop_main([]) == 1
    err = capsys.readouterr().err
    assert "pywebview" in err
    assert "pip install -r requirements.txt" in err


def test_a_dependency_missing_inside_pywebview_still_names_that_dependency(monkeypatch, tmp_path):
    """The message is only for pywebview itself being absent.

    If pywebview is installed but one of its own dependencies is not, the error
    naming that dependency has to reach the user unchanged, or they would be told
    to install a package they already have.
    """
    fake = tmp_path / "webview"
    fake.mkdir()
    (fake / "__init__.py").write_text("import gleapp_test_absent_dependency\n", encoding="utf-8")
    monkeypatch.delitem(sys.modules, "webview", raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    from gleapp.desktop import main as desktop_main

    with pytest.raises(ModuleNotFoundError) as info:
        desktop_main([])
    assert info.value.name == "gleapp_test_absent_dependency"


# A stand-in for the part of pywebview's surface gleapp.desktop touches, written as a
# package so ``import webview`` inside main() resolves to it whether or not pywebview
# is installed. start() calls the thread function inline, and a window left open when
# it would return is an error, because the real start() blocks until the last window
# closes.
_STUB_WEBVIEW = '''
class WebViewException(Exception):
    pass


class _Event:
    def __init__(self, ready):
        self._ready = ready
        self._handlers = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def wait(self, timeout=None):
        return self._ready

    def fire(self):
        for handler in self._handlers:
            handler()


class _Window:
    def __init__(self, url):
        self.url = url
        self.events = type("Events", (), {})()
        self.events.loaded = _Event(LOADED)
        self.events.closed = _Event(False)
        self.destroyed = 0

    def evaluate_js(self, script):
        return TITLE

    def destroy(self):
        self.destroyed += 1
        self.events.closed.fire()


windows = []
renderer = RENDERER


def create_window(title, url, **kwargs):
    window = _Window(url)
    windows.append(window)
    return window


def start(func=None, args=None):
    if START_RAISES:
        raise WebViewException(START_RAISES)
    if func is not None:
        func(*(args or ()))
    if any(not w.destroyed for w in windows):
        raise AssertionError("start() would block: a window is still open")
'''


def _stub_webview(tmp_path, monkeypatch, *, loaded=True, title="GLEAPP Review",
                  renderer="edgechromium", start_raises=None):
    # The stub's renderer is WebView2's name because the smoke requires that one on
    # Windows and accepts any elsewhere, and this suite runs on Windows in CI. The
    # per-platform rule itself is pinned by test_smoke_requires_webview2_on_windows_only.
    source = (_STUB_WEBVIEW
              .replace("LOADED", repr(loaded))
              .replace("TITLE", repr(title))
              .replace("RENDERER", repr(renderer))
              .replace("START_RAISES", repr(start_raises)))
    pkg = tmp_path / "webview"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(source, encoding="utf-8")
    monkeypatch.delitem(sys.modules, "webview", raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))


def test_smoke_mode_opens_the_window_loads_the_launcher_and_closes_it(tmp_path, monkeypatch, capsys):
    """GLEAPP_DESKTOP_SMOKE runs the whole entry and exits 0 once the launcher loaded."""
    _stub_webview(tmp_path, monkeypatch)
    monkeypatch.setenv("GLEAPP_DESKTOP_SMOKE", "1")
    # The stub has no WebView2 backend to patch, so stand in for what the patch
    # records once the real control is ready; Windows requires it of the smoke.
    from gleapp import _webview2_privacy
    monkeypatch.setattr(_webview2_privacy, "install", lambda: True)
    monkeypatch.setitem(_webview2_privacy.state, "smartscreen", False)
    monkeypatch.setitem(_webview2_privacy.state, "crash_reports", "local")
    from gleapp.desktop import main as desktop_main

    assert desktop_main([]) == 0
    (window,) = sys.modules["webview"].windows
    assert window.url.startswith("http://127.0.0.1:")
    assert window.destroyed == 1
    assert "ok=True" in capsys.readouterr().out


def test_smoke_mode_fails_when_the_page_is_not_gleapp(tmp_path, monkeypatch, capsys):
    """A window that opened on something other than GLEAPP is a failure, and still closes."""
    _stub_webview(tmp_path, monkeypatch, title="Internal Server Error")
    monkeypatch.setenv("GLEAPP_DESKTOP_SMOKE", "1")
    from gleapp.desktop import main as desktop_main

    assert desktop_main([]) == 1
    assert sys.modules["webview"].windows[0].destroyed == 1
    assert "ok=False" in capsys.readouterr().out


def test_smoke_mode_fails_when_the_page_never_loads(tmp_path, monkeypatch):
    """A window whose page never loads is closed and reported, rather than left open."""
    _stub_webview(tmp_path, monkeypatch, loaded=False)
    monkeypatch.setenv("GLEAPP_DESKTOP_SMOKE", "1")
    from gleapp.desktop import main as desktop_main

    assert desktop_main([]) == 1
    assert sys.modules["webview"].windows[0].destroyed == 1


class _FakeWindow:
    def __init__(self, loaded=True, title="GLEAPP Review"):
        self.events = types.SimpleNamespace(
            loaded=types.SimpleNamespace(wait=lambda timeout=None: loaded))
        self._title = title
        self.destroyed = 0

    def evaluate_js(self, _script):
        return self._title

    def destroy(self):
        self.destroyed += 1


def test_smoke_requires_webview2_on_windows_only():
    """On Windows the renderer must be WebView2; pywebview otherwise falls back to
    MSHTML with only a log warning, and the launcher's static title loads there too.
    On other platforms the renderer is whatever pywebview picked."""
    from gleapp.desktop import _Smoke

    private = {"smartscreen": False, "crash_reports": "local"}

    def verdict(renderer, platform, privacy=None):
        smoke = _Smoke(types.SimpleNamespace(renderer=renderer), platform=platform,
                       privacy=privacy)
        window = _FakeWindow()
        smoke.run(window)
        assert window.destroyed == 1
        return smoke.ok

    assert verdict("edgechromium", "win32", private) is True
    assert verdict("mshtml", "win32", private) is False
    # WebView2 with SmartScreen still on, or on an environment that sends crash
    # reports, is a window that opens and a smoke test that fails
    assert verdict("edgechromium", "win32") is False
    assert verdict("edgechromium", "win32", {**private, "smartscreen": True}) is False
    assert verdict("edgechromium", "win32", {**private, "smartscreen": None}) is False
    assert verdict("edgechromium", "win32", {**private, "crash_reports": "default"}) is False
    assert verdict("wkwebview", "darwin") is True
    assert verdict("gtkwebkit2", "linux") is True


def test_a_window_that_cannot_start_names_the_browser_alternative(tmp_path, monkeypatch, capsys):
    """pywebview raises WebViewException when no GUI toolkit is available (Linux with
    neither GTK nor Qt bindings). Its own message must reach the user, with the
    browser alternative, instead of a traceback."""
    message = ("You must have either QT or GTK with Python extensions installed "
               "in order to use pywebview.")
    _stub_webview(tmp_path, monkeypatch, start_raises=message)
    from gleapp.desktop import main as desktop_main

    assert desktop_main([]) == 1
    err = capsys.readouterr().err
    assert "could not start" in err
    assert "QT or GTK" in err
    assert "gleapp.py web" in err


def test_a_frozen_build_names_its_own_browser_command(tmp_path, monkeypatch, capsys):
    """A frozen build has no gleapp.py, so the fallback it names must be the binary's own
    ``web``, quoted when the path holds a space."""
    _stub_webview(tmp_path, monkeypatch, start_raises="no toolkit")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    from gleapp import desktop

    monkeypatch.setattr(sys, "executable", "/opt/GLEAPP/GLEAPP")
    assert desktop.main([]) == 1
    err = capsys.readouterr().err
    assert "    /opt/GLEAPP/GLEAPP web" in err
    assert "gleapp.py" not in err

    monkeypatch.setattr(sys, "executable", r"C:\Program Files\GLEAPP\GLEAPP.exe")
    assert desktop.main([]) == 1
    assert r'    "C:\Program Files\GLEAPP\GLEAPP.exe" web' in capsys.readouterr().err


def test_a_window_that_cannot_start_falls_back_to_the_browser_from_a_terminal(tmp_path, monkeypatch, capsys):
    """Started from a terminal, a window that cannot start hands the same interface to the
    browser, served without the native flag so the file pickers fall back to typed paths."""
    import json
    import urllib.request

    _stub_webview(tmp_path, monkeypatch, start_raises="no toolkit")
    from gleapp import desktop

    opened = []
    seen = {}

    def fake_wait(_server):
        url = opened[0]
        with urllib.request.urlopen(url, timeout=10) as r:
            seen["page"] = r.read().decode("utf-8", "replace")
        req = urllib.request.Request(url + "api/pick", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            seen["pick"] = json.loads(r.read())

    monkeypatch.setattr(desktop, "_has_terminal", lambda: True)
    monkeypatch.setattr(desktop, "_wait_for_interrupt", fake_wait)
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)

    assert desktop.main([]) == 0
    assert len(opened) == 1 and opened[0].startswith("http://127.0.0.1:")
    assert "GLEAPP" in seen["page"]
    assert seen["pick"]["error"] == "not running in desktop mode"
    err = capsys.readouterr().err
    assert "Opening the same interface in your browser instead." in err
    assert opened[0] in err


def test_smoke_mode_never_falls_back_to_the_browser(tmp_path, monkeypatch):
    """The desktop smoke test exists to prove the window starts, so a window that cannot
    start fails it even when a terminal is attached."""
    _stub_webview(tmp_path, monkeypatch, start_raises="no toolkit")
    monkeypatch.setenv("GLEAPP_DESKTOP_SMOKE", "1")
    from gleapp import desktop

    monkeypatch.setattr(desktop, "_has_terminal", lambda: True)
    monkeypatch.setattr(desktop, "_serve_in_browser",
                        lambda case_dir: pytest.fail("smoke mode fell back to the browser"))
    assert desktop.main([]) == 1


def _frozen_entrypoint():
    spec = importlib.util.spec_from_file_location("gleapp_frozen_entry", ROOT / "packaging" / "entrypoint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("args, expected", [
    (["web"], True),
    (["web", "--port", "9000"], True),
    (["-c", "mycase", "web"], True),
    (["--case=mycase", "--examiner", "AB", "web"], True),
    ([], False),
    (["mycase"], False),
    (["mycase", "web"], False),
    (["--version"], False),
])
def test_the_frozen_entrypoint_sends_web_to_the_command_line(args, expected):
    """``GLEAPP web`` has to reach ``gleapp web``: the Linux binary bundles no window
    toolkit, so the browser is the only interface it has."""
    assert _frozen_entrypoint().wants_web(args) is expected


@pytest.mark.parametrize("args", [["web", "--help"], ["-c", "mycase", "web", "--help"]])
def test_the_frozen_entrypoint_runs_the_web_subcommand(args):
    """Run as a script, the way the frozen build runs it, ``web`` reaches the web
    subcommand's own parser rather than being taken for a case folder."""
    env = {**__import__("os").environ, "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, str(ROOT / "packaging" / "entrypoint.py"), *args],
                       capture_output=True, text=True, cwd=ROOT, env=env, check=False, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "usage: gleapp" in r.stdout and "--no-browser" in r.stdout
