"""Isolated Windows.edb (ESE) read.

The vendored ESE reader (``gleapp/vendor/impacket_ese.py``, adapted from
impacket) can loop forever on a truncated or malformed database:
``getPage()`` retries a short read against a file already at EOF, and nothing
stops it once every further read returns zero bytes - measured spinning one
core indefinitely on a 20-byte file that merely started with the right magic.
Read here in a short-lived child process instead, so a bad ``Windows.edb``
costs a timeout, not a hung ingest. See ``gleapp/winsearch.py``.

    <python> -m gleapp._edbworker  <path>
stdout = JSON {"<16-hex ThumbnailCacheId>": [path, name], ...}
exit 0 = read (an empty object is a valid, complete result); non-zero = could
not open or parse the file at all.
"""

from __future__ import annotations

import json
import sys


def run(path: str) -> int:
    from .winsearch import edb_map  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
    try:
        result = edb_map(path)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return 3
    print(json.dumps({f"{k:016x}": list(v) for k, v in result.items()}))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    return run(argv[0])


if __name__ == "__main__":
    raise SystemExit(main())
