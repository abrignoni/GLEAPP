"""Run GLEAPP from a source checkout the way the other LEAPPs run: ``python gleapp.py web``.

Identical to ``python -m gleapp``. This file exists so the command matches
``python ileapp.py`` and the rest of the family. Frozen builds do not use it;
they start from packaging/entrypoint.py.
"""

from gleapp.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
