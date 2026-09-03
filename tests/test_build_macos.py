"""The macOS packaging phase of packaging/build.py, checked without a build."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "packaging" / "build.py"
darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS packaging")


def _driver_in(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("gleapp_build_driver_mac", DRIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "DIST", tmp_path / "dist")
    monkeypatch.setattr(mod, "BUILD", tmp_path / "build")
    return mod


@darwin_only
def test_dmg_needs_the_bundle_from_phase_one(tmp_path, monkeypatch):
    mod = _driver_in(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as e:
        mod.build_installer(None)
    assert "GLEAPP.app" in str(e.value)


@darwin_only
def test_sign_tool_is_refused_on_macos_with_the_right_pointer(tmp_path, monkeypatch):
    mod = _driver_in(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as e:
        mod.build_installer("anything")
    assert "codesign" in str(e.value)
