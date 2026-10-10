"""What the Windows window is told before it loads anything: SmartScreen off, and an
environment that keeps crash reports on the machine. The .NET types are stand-ins here;
the real ones are exercised by the desktop smoke test on Windows, which fails unless
both took effect."""

import sys
import types

import pytest

from gleapp import _webview2_privacy as privacy


class _Generic:
    """Stands in for a .NET generic: ``Action[Task[T]](fn)`` is ``fn``."""

    def __class_getitem__(cls, item):  # pylint: disable=unused-argument
        return lambda fn: fn


class _Options:
    def __init__(self):
        self.AdditionalBrowserArguments = None
        self.IsCustomCrashReportingEnabled = False


class _Controller:
    IsInPrivateModeEnabled = None


class _Environment:
    fail = False
    created = []

    def __init__(self, folder, data, options):
        self.folder, self.data, self.options = folder, data, options

    def CreateCoreWebView2ControllerOptions(self):
        return _Controller()

    @classmethod
    def CreateAsync(cls, folder, data, options):
        env = cls(folder, data, options)
        cls.created.append(env)
        done = types.SimpleNamespace(IsFaulted=cls.fail, Exception="no runtime", Result=env)
        return types.SimpleNamespace(ContinueWith=lambda callback, scheduler: callback(done))


class _Scheduler:
    @staticmethod
    def FromCurrentSynchronizationContext():
        return "ui"


def _edge(private_mode=True):
    """A module shaped like webview.platforms.edgechromium."""

    class WebView2:
        def __init__(self):
            self.ensured = []
            self.CreationProperties = types.SimpleNamespace(
                AdditionalBrowserArguments="--disable-features=ElasticOverscroll",
                BrowserExecutableFolder=None, UserDataFolder="C:/data")

        def EnsureCoreWebView2Async(self, environment=None, controller=None):
            self.ensured.append((environment, controller))

    class EdgeChrome:
        def __init__(self):
            self.seen_at_navigation = "not navigated"

        def on_webview_ready(self, sender, args):  # pylint: disable=unused-argument
            # pywebview navigates here; record what SmartScreen was at that moment
            self.seen_at_navigation = sender.CoreWebView2.Settings.IsReputationCheckingRequired

    return types.SimpleNamespace(WebView2=WebView2, EdgeChrome=EdgeChrome,
                                 _state={"private_mode": private_mode})


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setitem(privacy.state, "smartscreen", None)
    monkeypatch.setitem(privacy.state, "crash_reports", None)
    monkeypatch.setattr(_Environment, "fail", False)
    monkeypatch.setattr(_Environment, "created", [])


def _patched(**kwargs):
    edge = _edge(**kwargs)
    privacy.patch(edge, _Environment, _Options, _Generic, _Generic, _Scheduler)
    return edge


def _sender(required=True):
    settings = types.SimpleNamespace(IsReputationCheckingRequired=required)
    return types.SimpleNamespace(CoreWebView2=types.SimpleNamespace(Settings=settings))


def test_smartscreen_is_off_before_pywebview_navigates():
    edge = _patched()
    chrome, sender = edge.EdgeChrome(), _sender()
    chrome.on_webview_ready(sender, types.SimpleNamespace(IsSuccess=True))
    assert chrome.seen_at_navigation is False
    assert privacy.state["smartscreen"] is False


def test_a_failed_initialization_is_left_to_pywebview():
    edge = _patched()
    chrome = edge.EdgeChrome()
    sender = types.SimpleNamespace(CoreWebView2=None)
    with pytest.raises(AttributeError):          # the stand-in handler reads the control
        chrome.on_webview_ready(sender, types.SimpleNamespace(IsSuccess=False))
    assert privacy.state["smartscreen"] is None


def test_the_environment_keeps_crash_reports_and_what_pywebview_asked_for():
    edge = _patched(private_mode=True)
    control = edge.WebView2()
    control.EnsureCoreWebView2Async(None)

    assert len(_Environment.created) == 1
    env = _Environment.created[0]
    assert env.options.IsCustomCrashReportingEnabled is True
    assert env.options.AdditionalBrowserArguments == "--disable-features=ElasticOverscroll"
    assert (env.folder, env.data) == (None, "C:/data")
    assert len(control.ensured) == 1
    used, controller = control.ensured[0]
    assert used is env
    assert controller.IsInPrivateModeEnabled is True
    assert privacy.state["crash_reports"] == "local"


def test_private_mode_off_is_carried_too():
    edge = _patched(private_mode=False)
    control = edge.WebView2()
    control.EnsureCoreWebView2Async(None)
    assert control.ensured[0][1].IsInPrivateModeEnabled is False


def test_an_environment_that_cannot_be_made_still_opens_the_window_and_says_so():
    _Environment.fail = True
    edge = _patched()
    control = edge.WebView2()
    control.EnsureCoreWebView2Async(None)
    assert control.ensured == [(None, None)]
    assert privacy.state["crash_reports"] == "default"


def test_an_environment_handed_in_is_used_as_it_is():
    edge = _patched()
    control = edge.WebView2()
    control.EnsureCoreWebView2Async("theirs")
    assert control.ensured == [("theirs", None)]
    assert _Environment.created == []


def test_patching_twice_wraps_once():
    edge = _patched()
    first = edge.WebView2
    privacy.patch(edge, _Environment, _Options, _Generic, _Generic, _Scheduler)
    assert edge.WebView2 is first
    chrome, sender = edge.EdgeChrome(), _sender()
    chrome.on_webview_ready(sender, types.SimpleNamespace(IsSuccess=True))
    assert chrome.seen_at_navigation is False


@pytest.mark.skipif(sys.platform == "win32", reason="the real backend is patched there")
def test_install_does_nothing_off_windows():
    before = set(sys.modules)
    assert privacy.install() is False
    assert not any(name.startswith("webview") for name in set(sys.modules) - before)
