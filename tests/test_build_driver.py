"""packaging/build.py must load, explain itself, read the version, and refuse the unsafe combinations.

These run on every CI platform. The builds themselves are exercised by the
test_builds workflow, not here, since a PyInstaller run is minutes rather than seconds.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "packaging" / "build.py"


def _load():
    spec = importlib.util.spec_from_file_location("gleapp_build_driver", DRIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(*args):
    return subprocess.run([sys.executable, str(DRIVER), *args],
                          capture_output=True, text=True, check=False)


def test_help_lists_every_phase():
    r = _run("--help")
    assert r.returncode == 0, r.stderr
    for word in ("exe", "installer", "all", "verify"):
        assert word in r.stdout


def test_version_matches_the_package_without_importing_it():
    from gleapp import __version__

    assert _load().read_version() == __version__


def test_all_refuses_onefile_because_the_installer_packages_a_folder():
    r = _run("all", "--onefile")
    assert r.returncode != 0
    assert "one-folder" in r.stdout + r.stderr


def test_all_refuses_a_sign_tool_because_the_inner_exe_would_be_unsigned():
    r = _run("all", "--sign-tool", "anything")
    assert r.returncode != 0
    assert "unsigned" in r.stdout + r.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="Windows is the platform that has an installer")
def test_installer_says_plainly_it_is_windows_only():
    r = _run("installer")
    assert r.returncode != 0
    assert "Windows" in r.stdout + r.stderr
