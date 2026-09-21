"""How GLEAPP starts its isolated worker processes.

Decoding a video, decoding a GPU texture and reading a Windows.edb each run in a
short-lived child process, so a native crash or an endless loop costs one file
rather than the run (``_vidworker``, ``_texworker`` and ``_edbworker``). A frozen
build starts its own executable again with a flag that
``packaging/entrypoint.py`` dispatches. A source run starts the same Python with
a one-line bootstrap that imports the worker from the ``gleapp`` package this
process is running and calls its ``main``, as the frozen entry point does.

The bootstrap replaced ``python -m gleapp._vidworker`` and its two siblings,
because ``-m`` looks for the package in the current directory first. In a
source run started from anywhere but the checkout root, the child could not
import ``gleapp``, so every video, GPU texture and Windows.edb read failed.
Started from a directory holding another ``gleapp`` package, the child ran that
package's worker instead.
"""

from __future__ import annotations

import os
import sys

# The directory that holds this ``gleapp`` package: the checkout root in a
# source run, site-packages for an installed copy.
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Run as ``python -c``, with that directory and the worker's module name ahead
# of the worker's own arguments. ``-c`` puts the current directory ('') first on
# sys.path. The bootstrap takes it off and puts the package's directory first
# unless it is already on the path, so the child resolves ``gleapp`` and its
# dependencies the way ``python gleapp.py`` does, never from wherever the
# examiner happened to start GLEAPP.
_BOOTSTRAP = (
    "import importlib, sys; root, name = sys.argv[1:3]; "
    "sys.path[:1] == [''] and sys.path.pop(0); "
    "root in sys.path or sys.path.insert(0, root); "
    "sys.exit(importlib.import_module(name).main(sys.argv[3:]))"
)


def worker_command(name: str) -> list[str]:
    """The command that starts worker ``name`` ("vidworker", "texworker" or
    "edbworker"). The caller appends the worker's own arguments."""
    if getattr(sys, "frozen", False):
        return [sys.executable, f"--{name}"]
    return [sys.executable, "-c", _BOOTSTRAP, _PACKAGE_PARENT, f"gleapp._{name}"]
