"""Frozen-app entry point. Kept outside the package so PyInstaller runs it as a
plain script while ``gleapp`` stays importable as a package."""

import multiprocessing
import sys

# Options gleapp's command line takes ahead of a subcommand, each followed by a value.
_LEADING_OPTIONS = ("-c", "--case", "--examiner")


def wants_web(args: list[str]) -> bool:
    """True when the command line asks for the browser interface.

    ``GLEAPP web`` and ``GLEAPP -c <case> web`` go to ``gleapp web``, the same
    interface served to a browser, which needs no GUI toolkit. Everything else still
    opens the desktop window.
    """
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _LEADING_OPTIONS:
            i += 2
        elif arg.startswith(tuple(f"{o}=" for o in _LEADING_OPTIONS if o.startswith("--"))):
            i += 1
        else:
            break
    return i < len(args) and args[i] == "web"


if __name__ == "__main__":
    multiprocessing.freeze_support()
    # isolated workers re-invoke GLEAPP.exe (crash-safe decoding)
    if len(sys.argv) > 1 and sys.argv[1] == "--vidworker":
        from gleapp._vidworker import main as vw_main
        sys.exit(vw_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--texworker":
        from gleapp._texworker import main as tw_main
        sys.exit(tw_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--edbworker":
        from gleapp._edbworker import main as edb_main
        sys.exit(edb_main(sys.argv[2:]))
    # The build excludes c2pa, so this answers "not installed" for every file;
    # dispatched all the same, so a frozen build never opens a window for it.
    if len(sys.argv) > 1 and sys.argv[1] == "--c2paworker":
        from gleapp._c2paworker import main as c2pa_main
        sys.exit(c2pa_main(sys.argv[2:]))
    # --version answers without a window, so a frozen build can be smoke-tested
    # headless and it matches `python gleapp.py --version`. desktop.main() does not
    # handle it.
    if "--version" in sys.argv[1:]:
        from gleapp import __version__
        print(f"GLEAPP {__version__}")
        sys.exit(0)
    # Before anything loads pythonnet: a portable build extracted from a downloaded zip
    # carries the internet mark on every DLL, and .NET Framework refuses to load them.
    # Workers never load .NET, so they skip the folder walk. See gleapp/_zone_marks.py.
    from gleapp._zone_marks import clear_frozen_bundle
    clear_frozen_bundle()
    # --selfcheck imports what the desktop shell imports, the native stack included,
    # and exits without opening a window. --version answers above this line and
    # --texworker only reaches Pillow, so neither of them loads cv2: the macOS build of
    # v2026.5.0 passed both and still could not start, because a harfbuzz collision in
    # the bundle made importing cv2 fail. A frozen build that cannot import these
    # cannot run, so this is what a smoke test has to call.
    # On Windows it also imports pywebview's WinForms backend, which is where pythonnet
    # loads .NET and the window's assemblies: gleapp.desktop imports webview only inside
    # main(), so without it the check passed on a v2026.5.1 portable build that could not
    # open its window. This build has no console, so an uncaught exception would open a
    # dialog and wait for a click; the check reports the failure and exits 1 instead.
    if "--selfcheck" in sys.argv[1:]:
        import importlib
        import traceback
        names = ["numpy", "PIL.Image", "cv2", "gleapp.web.app", "gleapp.desktop",
                 "gleapp.cli"]
        if sys.platform == "win32":
            names.append("webview.platforms.winforms")
        for name in names:
            try:
                importlib.import_module(name)
            except Exception:  # pylint: disable=broad-exception-caught
                traceback.print_exc()
                print(f"selfcheck failed importing {name}", file=sys.stderr)
                sys.exit(1)
            print(f"ok {name}")
        # Importing cv2 does not prove it can read the Find similar content model:
        # every OpenCV 4.x imports fine and then fails on the model's Expand or cubic
        # Resize node, so a build on 4.x would fail every content pass.
        try:
            import cv2
            from gleapp import content
            cv2.dnn.readNetFromONNX(str(content.model_path()))  # pylint: disable=no-member
        except Exception:  # pylint: disable=broad-exception-caught
            traceback.print_exc()
            print("selfcheck failed loading the content model", file=sys.stderr)
            sys.exit(1)
        print("ok content model")
        print("selfcheck passed")
        sys.exit(0)
    if wants_web(sys.argv[1:]):
        from gleapp.cli import main as cli_main
        sys.exit(cli_main(sys.argv[1:]))
    from gleapp.desktop import main
    sys.exit(main())
