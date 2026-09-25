"""Frozen-app entry point. Kept outside the package so PyInstaller runs it as a
plain script while ``gleapp`` stays importable as a package."""

import multiprocessing
import sys

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
    # --version answers without a window, so a frozen build can be smoke-tested
    # headless and it matches `python gleapp.py --version`. desktop.main() does not
    # handle it.
    if "--version" in sys.argv[1:]:
        from gleapp import __version__
        print(f"GLEAPP {__version__}")
        sys.exit(0)
    # --selfcheck imports what the desktop shell imports, the native stack included,
    # and exits without opening a window. --version answers above this line and
    # --texworker only reaches Pillow, so neither of them loads cv2: the macOS build of
    # v2026.5.0 passed both and still could not start, because a harfbuzz collision in
    # the bundle made importing cv2 fail. A frozen build that cannot import these
    # cannot run, so this is what a smoke test has to call.
    if "--selfcheck" in sys.argv[1:]:
        import importlib
        for name in ("numpy", "PIL.Image", "cv2", "gleapp.web.app", "gleapp.desktop"):
            importlib.import_module(name)
            print(f"ok {name}")
        print("selfcheck passed")
        sys.exit(0)
    from gleapp.desktop import main
    sys.exit(main())
