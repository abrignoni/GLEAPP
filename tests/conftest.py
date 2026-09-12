"""Session-wide isolation of the per-user config directory.

``gleapp.appconfig`` reads ``$GLEAPP_CONFIG_DIR`` and falls back to the
platform's per-user application directory when it is unset.  Opening a case
through the web app calls ``appconfig.push_recent`` (``gleapp/web/app.py``), so
a test that builds the Flask app against a tmp case writes into the examiner's
own recent-cases list, and the twelve-entry cap in ``push_recent`` then evicts
the cases they actually work on.  Measured on macOS: after one suite run all
twelve entries in the real config were pytest tmp paths.

Thirteen test modules already pointed the variable at a tmp directory in their
own autouse fixture and nineteen did not, so isolation was per module rather
than global.  This fixture sets the variable for the whole session before any
test runs, on every platform, so the suite cannot reach a real config whichever
modules are collected.  The per-module fixtures keep working unchanged: they
narrow it further per test, and their teardown now restores this directory
instead of unsetting the variable.

The hash store and the stash live under the same directory and each cache a
module-level connection, so both are closed around the session.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_user_config(tmp_path_factory):
    """Point the config, app-data and stash locations at a tmp directory."""
    cfg = tmp_path_factory.mktemp("user-config")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GLEAPP_CONFIG_DIR", str(cfg))
        # stash_path() reads this before it consults the config dir, so an
        # examiner who keeps their stash on a shared drive would otherwise have
        # the suite write into it.
        mp.delenv("GLEAPP_STASH_PATH", raising=False)
        from gleapp import hashstore, stash
        hashstore.close()  # drop anything opened while collecting modules
        stash.close()
        try:
            yield cfg
        finally:
            hashstore.close()
            stash.close()
