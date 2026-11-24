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


def _dmg_settings():
    names = {"defines": {"app": "/x/dist/GLEAPP.app", "icon": "i.icns", "background": "b.png"}}
    code = (ROOT / "packaging" / "dmg_settings.py").read_text(encoding="utf-8")
    exec(compile(code, "dmg_settings.py", "exec"), names, names)  # pylint: disable=exec-used
    return names


def test_dmg_layout_puts_the_app_and_applications_either_side_of_the_arrow():
    s = _dmg_settings()
    assert s["files"] == ["/x/dist/GLEAPP.app"]
    assert s["symlinks"] == {"Applications": "/Applications"}
    assert set(s["icon_locations"]) == {"GLEAPP.app", "Applications"}
    assert s["background"] == "b.png"


def test_dmg_window_is_as_wide_as_its_background():
    from PIL import Image  # pylint: disable=import-outside-toplevel
    with Image.open(ROOT / "packaging" / "dmg_background.png") as img:
        width, height = img.size
    (_, _), (win_w, win_h) = _dmg_settings()["window_rect"]
    assert win_w == width
    assert win_h >= height


def test_dmg_icons_sit_either_side_of_the_arrow():
    """The arrow moved 44 points with the background of 2026-10-01, and a background that
    moves it again leaves the icons off centre unless the settings move with it. The arrow
    is the only dark mark in its band."""
    from PIL import Image  # pylint: disable=import-outside-toplevel
    with Image.open(ROOT / "packaging" / "dmg_background.png") as img:
        rgb = img.convert("RGB")
        dark = [x for y in range(250, 320) for x in range(rgb.width)
                if sum(rgb.getpixel((x, y))) < 600]
    arrow_centre = (min(dark) + max(dark)) / 2
    locations = _dmg_settings()["icon_locations"]
    (app_x, app_y), (apps_x, apps_y) = locations["GLEAPP.app"], locations["Applications"]
    assert app_y == apps_y
    assert app_x < min(dark)
    assert apps_x > max(dark)
    assert abs((app_x + apps_x) / 2 - arrow_centre) <= 2


def test_dmg_retina_background_is_exactly_twice_the_size():
    """dmgbuild finds dmg_background@2x.png by name and joins it to the background with
    tiffutil -cathidpicheck, which refuses a pair that is not exactly 1x and 2x, and the
    disk image then fails to build. Without it a Retina screen shows the 1x blurred."""
    from PIL import Image  # pylint: disable=import-outside-toplevel
    with Image.open(ROOT / "packaging" / "dmg_background.png") as img:
        width, height = img.size
    with Image.open(ROOT / "packaging" / "dmg_background@2x.png") as img:
        assert img.size == (2 * width, 2 * height)


@darwin_only
def test_dmg_carries_the_background_and_the_applications_link(tmp_path, monkeypatch):
    pytest.importorskip("dmgbuild")
    import subprocess  # pylint: disable=import-outside-toplevel
    mod = _driver_in(tmp_path, monkeypatch)
    app = tmp_path / "dist" / "GLEAPP.app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    (app / "GLEAPP").write_text("#!/bin/sh\n", encoding="utf-8")
    dmg = mod.build_installer(None)
    mnt = tmp_path / "mnt"
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen",
                    "-mountpoint", str(mnt), str(dmg)], check=True, capture_output=True)
    try:
        # dmgbuild joins dmg_background.png and its @2x into one TIFF holding both sizes
        from PIL import Image  # pylint: disable=import-outside-toplevel
        with Image.open(mnt / ".background.tiff") as img:
            assert img.n_frames == 2
        assert (mnt / ".DS_Store").is_file()
        assert (mnt / "Applications").is_symlink()
        assert (mnt / "GLEAPP.app").is_dir()
    finally:
        subprocess.run(["hdiutil", "detach", str(mnt)], check=False, capture_output=True)


@darwin_only
def test_verify_accepts_a_signed_app_bundle(tmp_path, monkeypatch, capsys):
    import shutil  # pylint: disable=import-outside-toplevel
    import subprocess  # pylint: disable=import-outside-toplevel
    mod = _driver_in(tmp_path, monkeypatch)
    app = tmp_path / "GLEAPP.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    shutil.copy("/usr/bin/true", app / "Contents" / "MacOS" / "GLEAPP")
    (app / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
        "<key>CFBundleExecutable</key><string>GLEAPP</string>"
        "<key>CFBundleIdentifier</key><string>org.leapp.gleapp.test</string>"
        "</dict></plist>\n", encoding="utf-8")
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)],
                   check=True, capture_output=True)
    assert mod.verify([str(app)], None) == 0
    assert "OK" in capsys.readouterr().out


@darwin_only
def test_verify_still_fails_a_missing_bundle(tmp_path, monkeypatch):
    mod = _driver_in(tmp_path, monkeypatch)
    assert mod.verify([str(tmp_path / "missing.app")], None) == 1
