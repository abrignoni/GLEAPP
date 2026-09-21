"""The isolated workers start from any working directory, and run this package.

A source run started each worker as ``python -m gleapp._<name>``, and ``-m``
looks for ``gleapp`` in the current directory first. From anywhere but the
checkout root the child could not import it, so every video, GPU texture and
Windows.edb read failed with no sign that the worker had never run. From a
directory holding another ``gleapp`` package, the child ran that package's
worker instead. Each test here runs the real worker through the call site
GLEAPP uses, with the working directory moved away from the checkout
(``gleapp/workers.py`` has the launch).
"""

import struct
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from gleapp import imaging, media, winsearch

WORKERS = ("vidworker", "texworker", "edbworker")


def _clip(path):
    """A 64x48 MP4 of ten frames, written by OpenCV."""
    # pylint: disable=no-member
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (64, 48))
    for i in range(10):
        vw.write(np.full((48, 64, 3), i * 20, np.uint8))
    vw.release()
    return path


def _ktx(path, w=16, h=8):
    """An uncompressed RGBA8 KTX 1. KTX is a GPU-texture format, so
    ``transcode_isolated`` decodes it in the worker process, not in-process."""
    data = np.full((h, w, 4), 200, np.uint8).tobytes()
    header = struct.pack("<12s13I", b"\xabKTX 11\xbb\r\n\x1a\n", 0x04030201,
                         0x1401, 1, 0x1908, 0x8058, 0x1908, w, h, 0, 0, 1, 1, 0)
    path.write_bytes(header + struct.pack("<I", len(data)) + data)
    return path


def _video_ran(tmp_path, _monkeypatch) -> bool:
    clip = _clip(tmp_path / "clip.mp4")
    single = media.extract_video_isolated(clip, tmp_path / "thumbs", count=3, screen=False)
    batch = media.extract_videos_batch([str(clip)], tmp_path / "thumbs_batch", count=3,
                                       screen=False)[str(clip)]
    return all(r is not None and r["info"]["width"] == 64 and r["frames"]
               for r in (single, batch))


def _texture_ran(tmp_path, _monkeypatch) -> bool:
    dest = tmp_path / "texture.jpg"
    if not imaging.transcode_isolated(_ktx(tmp_path / "texture.ktx"), dest):
        return False
    with Image.open(dest) as im:
        return im.size == (16, 8)


def _edb_ran(tmp_path, monkeypatch) -> bool:
    """Run the edb reader on a file that does not exist.

    ``edb_map_isolated`` returns ``{}`` both when the reader finds nothing and
    when the child could not start, so the child's exit code is what tells
    them apart: ``_edbworker.run`` exits 3 when the reader raises, and a child
    that could not import ``gleapp`` exits 1."""
    runs = []
    real_run = subprocess.run

    def spy(*args, **kwargs):
        # the caller's own check= arrives in kwargs
        # pylint: disable=subprocess-run-check
        runs.append(real_run(*args, **kwargs))
        return runs[-1]

    monkeypatch.setattr(subprocess, "run", spy)
    assert winsearch.edb_map_isolated(tmp_path / "missing" / "Windows.edb", timeout=60) == {}
    assert len(runs) == 1
    return runs[0].returncode == 3


RAN = {"vidworker": _video_ran, "texworker": _texture_ran, "edbworker": _edb_ran}


def _decoy(root: Path, name: str) -> Path:
    """Another ``gleapp`` package whose worker ``name`` only leaves a marker."""
    (root / "gleapp").mkdir(parents=True)
    (root / "gleapp" / "__init__.py").write_text("")
    marker = root / f"ran_{name}"
    (root / "gleapp" / f"_{name}.py").write_text(
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
        "def main(argv=None):\n"
        "    return 0\n")
    return marker


@pytest.mark.parametrize("name", WORKERS)
def test_a_worker_runs_from_a_directory_outside_the_checkout(name, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert RAN[name](tmp_path, monkeypatch), f"the {name} did not run"


@pytest.mark.parametrize("name", WORKERS)
def test_a_worker_does_not_run_a_gleapp_package_in_the_working_directory(
        name, tmp_path, monkeypatch):
    other = tmp_path / "other-checkout"
    marker = _decoy(other, name)
    monkeypatch.chdir(other)
    # the control: started the way a source run used to start it, the child
    # imports the package in the working directory, so the decoy is live
    subprocess.run([sys.executable, "-m", f"gleapp._{name}"], check=False,
                   capture_output=True, timeout=60)
    assert marker.exists()
    marker.unlink()

    ran = RAN[name](tmp_path, monkeypatch)
    assert not marker.exists(), "the worker ran the package in the working directory"
    assert ran, f"the {name} did not run"


@pytest.mark.parametrize("name", WORKERS)
def test_a_frozen_build_starts_its_own_executable_with_a_flag_it_dispatches(name, monkeypatch):
    from gleapp.workers import worker_command
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert worker_command(name) == [sys.executable, f"--{name}"]
    entry = (Path(__file__).resolve().parents[1] / "packaging" / "entrypoint.py").read_text()
    assert f'sys.argv[1] == "--{name}"' in entry
    assert f"from gleapp._{name} import main" in entry
