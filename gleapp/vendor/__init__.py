"""Third-party files copied in verbatim, with their provenance in vendored.json.

These are copies, not dependencies. Fix them upstream and re-vendor; an edit made
here is silent, and the next sync reverts it. ``tests/test_archive_ewf.py`` compares
every file against the hash recorded when it was vendored, so an edit fails the suite
rather than travelling.

Both are single-file, standard-library-only and MIT, which is why they are carried
this way rather than declared as requirements: GLEAPP ships as a frozen desktop app
and adding a package with a build step to that is a cost with nothing behind it.
"""
