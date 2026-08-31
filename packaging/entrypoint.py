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
    from gleapp.desktop import main
    sys.exit(main())
