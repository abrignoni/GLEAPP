"""The c2pa reader, in a process of its own.

``provenance.read_batch`` starts this with a JSON request on stdin, the reader
settings and the files to read (``{"settings": {...}, "items": [{"path", "mime"}]}``),
and reads one JSON line per file back from stdout, written as each is read. The
native library is loaded here and nowhere else, so a fault in it ends this process
and costs the file it was reading (see ``provenance._run_reader``), never the run.
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str] | None = None) -> int:
    del argv                       # everything arrives on stdin
    from gleapp.provenance import read_outcome  # pylint: disable=import-outside-toplevel
    request = json.loads(sys.stdin.read() or "{}")
    settings = request.get("settings") or {}
    for item in request.get("items") or []:
        outcome = read_outcome(item["path"], item.get("mime") or "", settings)
        sys.stdout.write(json.dumps({"path": item["path"], "outcome": outcome}) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
