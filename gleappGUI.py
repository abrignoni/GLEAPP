"""Open GLEAPP's native desktop window from a source checkout: ``python gleappGUI.py``.

Identical to the ``gleapp-desktop`` console script, named to match
``ileappGUI.py``. Needs the desktop extra: ``pip install -e .[desktop]``.
"""

from gleapp.desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
