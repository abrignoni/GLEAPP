"""Isolated image transcode.

The GPU-texture decoder (``texture2ddecoder``, a Rust extension) can *segfault*
on malformed data, so KTX / DDS / etc. are decoded here in a short-lived child
process.  Also handles ordinary Pillow formats so callers have one path.

    <python> -m gleapp._texworker  <src>  <dest.jpg>  [max_side]
exit 0 = wrote dest ; non-zero = could not decode
"""

from __future__ import annotations

import sys
from pathlib import Path


def run(src: str, dest: str, max_side: int = 2200) -> int:
    from .imaging import to_web_jpeg  # registers heif + pulls in the KTX path
    return 0 if to_web_jpeg(src, Path(dest), max_side=max_side) else 3


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    src, dest = argv[0], argv[1]
    max_side = int(argv[2]) if len(argv) > 2 else 2200
    return run(src, dest, max_side)


if __name__ == "__main__":
    raise SystemExit(main())
