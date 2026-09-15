"""Open GLEAPP's native desktop window from a source checkout: ``python gleappGUI.py``.

Identical to the ``gleapp-desktop`` console script, named to match
``ileappGUI.py``. Needs pywebview, which ``pip install -r requirements.txt``
installs (the module is imported as ``webview``; PyPI's package named ``webview``
is a different project).
"""

from gleapp.desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
