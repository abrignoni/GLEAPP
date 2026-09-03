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


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="Windows and macOS have packaging wired up")
def test_installer_says_plainly_which_platforms_are_wired():
    r = _run("installer")
    assert r.returncode != 0
    assert "Windows" in r.stdout + r.stderr and "macOS" in r.stdout + r.stderr


def _driver_in(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "DIST", tmp_path / "dist")
    monkeypatch.setattr(mod, "BUILD", tmp_path / "build")
    return mod


def test_onefile_refuses_to_delete_a_folder_build(tmp_path, monkeypatch):
    """Off Windows both layouts are dist/GLEAPP; PyInstaller would remove the folder silently."""
    mod = _driver_in(tmp_path, monkeypatch)
    (mod.DIST / mod.exe_name()).mkdir(parents=True)   # a folder sitting where the one-file output goes
    with pytest.raises(SystemExit) as e:
        mod.build_exe(onefile=True, clean=False)
    assert "would delete it" in str(e.value)


def test_folder_build_refuses_to_clobber_a_onefile_build(tmp_path, monkeypatch):
    mod = _driver_in(tmp_path, monkeypatch)
    mod.DIST.mkdir()
    (mod.DIST / mod.APP).write_text("a one-file build")
    with pytest.raises(SystemExit) as e:
        mod.build_exe(onefile=False, clean=False)
    assert "where the folder build must go" in str(e.value)


def test_clean_discards_the_collision_and_proceeds(tmp_path, monkeypatch):
    """--clean is the sanctioned way past the guard; stop the build at pip so this stays fast."""
    mod = _driver_in(tmp_path, monkeypatch)
    (mod.DIST / mod.exe_name()).mkdir(parents=True)
    calls = []
    monkeypatch.setattr(mod, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(mod, "assert_artifact", lambda path, what: None)
    mod.build_exe(onefile=True, clean=True)
    assert not (mod.DIST / mod.exe_name()).exists()
    assert calls and "pip" in calls[0]
