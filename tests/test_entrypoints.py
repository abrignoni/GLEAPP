"""The LEAPP-style entry points at the repo root must keep working from a source checkout."""

import importlib.util
import subprocess
import sys
from pathlib import Path

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

    Importing it must not need the desktop extra, because desktop.py imports
    pywebview lazily inside main(). That is what lets this run in the plain CI
    environment without opening a window.
    """
    spec = importlib.util.spec_from_file_location("gleappGUI", ROOT / "gleappGUI.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from gleapp.desktop import main as desktop_main

    assert mod.main is desktop_main
