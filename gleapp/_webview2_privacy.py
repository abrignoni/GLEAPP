"""Turn off what the Windows window would otherwise send to Microsoft.

On Windows the desktop window is drawn by Microsoft's WebView2 Runtime, and two of its
features are on unless the application turns them off (Microsoft's "Data and privacy in
WebView2", https://learn.microsoft.com/microsoft-edge/webview2/concepts/data-privacy):

SmartScreen
    Checks the address of a page, and the hash and name of a download, with Microsoft's
    service. ``CoreWebView2Settings.IsReputationCheckingRequired`` is its switch, true by
    default, and it has to be false before the first navigation.

Crash reports
    A minidump of a WebView2 process that crashes is sent to Microsoft unless the
    environment was created with
    ``CoreWebView2EnvironmentOptions.IsCustomCrashReportingEnabled`` set, which keeps
    the dump on the machine.

pywebview sets neither. The first is a setting on the control and is set from
pywebview's own ready handler, ahead of the navigation that handler starts. The second
is an option of the environment, which pywebview lets the control create for itself
(``EnsureCoreWebView2Async(None)``), so the control is replaced with one that creates
the environment with the option and hands it over, with the folder, arguments and
private mode pywebview asked for.

What this does not reach: the diagnostic data the runtime collects as a Windows
component, which Microsoft's page says the application has no control over.

``state`` records what happened, and the desktop smoke test fails on Windows unless
both took effect. Nothing here was measured on the wire.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

# smartscreen: IsReputationCheckingRequired as read back after it was set (False is
# off). crash_reports: "local" when the window runs on an environment created with
# IsCustomCrashReportingEnabled, "default" when that failed and the control created
# its own. None for either means the window has not got that far.
state: dict = {"smartscreen": None, "crash_reports": None}


def install() -> bool:
    """Patch pywebview's WebView2 backend. True when the patch is in place."""
    if sys.platform != "win32":
        return False
    try:
        # winforms first, the order pywebview itself imports them in
        from webview.platforms import winforms
        if not getattr(winforms, "is_chromium", False):
            return False
        from webview.platforms import edgechromium
        from Microsoft.Web.WebView2.Core import (  # pylint: disable=import-error
            CoreWebView2Environment, CoreWebView2EnvironmentOptions)
        from System import Action  # pylint: disable=import-error
        from System.Threading.Tasks import Task, TaskScheduler  # pylint: disable=import-error
    except Exception:  # pylint: disable=broad-exception-caught
        log.warning("WebView2 privacy settings were not applied", exc_info=True)
        return False
    patch(edgechromium, CoreWebView2Environment, CoreWebView2EnvironmentOptions,
          Action, Task, TaskScheduler)
    return True


def patch(edge, environment_type, options_type, action, task, scheduler) -> None:
    """Replace ``edge.WebView2`` and wrap ``edge.EdgeChrome.on_webview_ready``.

    The .NET types are parameters so the logic can be tested without Windows.
    """
    base = edge.WebView2
    if getattr(base, "gleapp_private", False):
        return
    pywebview_ready = edge.EdgeChrome.on_webview_ready

    def on_webview_ready(self, sender, args):
        # pywebview's handler navigates, so the setting goes in before it runs
        try:
            if args.IsSuccess:
                settings = sender.CoreWebView2.Settings
                settings.IsReputationCheckingRequired = False
                state["smartscreen"] = bool(settings.IsReputationCheckingRequired)
        except Exception:  # pylint: disable=broad-exception-caught
            log.warning("SmartScreen could not be turned off", exc_info=True)
        return pywebview_ready(self, sender, args)

    class PrivateWebView2(base):  # pylint: disable=too-few-public-methods
        """The WebView2 control, started on an environment that keeps crash dumps here."""

        gleapp_private = True

        def EnsureCoreWebView2Async(self, environment=None, controller=None):  # pylint: disable=invalid-name
            if environment is not None:
                if controller is None:
                    return super().EnsureCoreWebView2Async(environment)
                return super().EnsureCoreWebView2Async(environment, controller)

            def implicit():
                state["crash_reports"] = "default"
                return super(PrivateWebView2, self).EnsureCoreWebView2Async(None)

            def created(done):
                try:
                    if done.IsFaulted:
                        raise RuntimeError(str(done.Exception))
                    env = done.Result
                    wanted = env.CreateCoreWebView2ControllerOptions()
                    # pylint: disable=protected-access
                    wanted.IsInPrivateModeEnabled = bool(edge._state["private_mode"])
                    super(PrivateWebView2, self).EnsureCoreWebView2Async(env, wanted)
                    state["crash_reports"] = "local"
                except Exception:  # pylint: disable=broad-exception-caught
                    log.warning("WebView2 crash reports were left at their default",
                                exc_info=True)
                    implicit()

            try:
                props = self.CreationProperties
                options = options_type()
                if props.AdditionalBrowserArguments:
                    options.AdditionalBrowserArguments = props.AdditionalBrowserArguments
                options.IsCustomCrashReportingEnabled = True
                pending = environment_type.CreateAsync(
                    props.BrowserExecutableFolder, props.UserDataFolder, options)
                return pending.ContinueWith(
                    action[task[environment_type]](created),
                    scheduler.FromCurrentSynchronizationContext())
            except Exception:  # pylint: disable=broad-exception-caught
                log.warning("WebView2 crash reports were left at their default",
                            exc_info=True)
                return implicit()

    edge.EdgeChrome.on_webview_ready = on_webview_ready
    edge.WebView2 = PrivateWebView2
