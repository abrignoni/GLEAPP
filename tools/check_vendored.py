#!/usr/bin/env python3
"""Confirm the vendored files still match what was vendored.

Vendored code is a copy, so it drifts in two directions and both are silent. A
local edit looks like a fix until the next re-vendor reverts it, and an upstream
release leaves this copy quietly old. Neither shows up in a diff of this repo.

    python3 tools/check_vendored.py                       # has the copy changed?
    python3 tools/check_vendored.py --upstream ../ewfprobe
    python3 tools/check_vendored.py --update              # after a deliberate re-vendor

Drift and "could not check" are reported separately and exit differently. They are
different results: one says the copy is wrong, the other says nothing was compared,
and folding the second into the first turns an unreachable upstream into a finding
that reads as a defect.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(REPO, "gleapp", "vendor", "vendored.json")


def sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--upstream", metavar="DIR", action="append", default=[],
                        help="a checkout of an upstream repo, to compare against as "
                             "well; may be given more than once")
    parser.add_argument("--update", action="store_true",
                        help="rewrite the recorded hashes from the files on disk, "
                             "for use only after a deliberate re-vendor")
    args = parser.parse_args()

    with open(MANIFEST, encoding="utf-8") as handle:
        manifest = json.load(handle)

    drifted, unchecked = [], []
    for entry in manifest["vendored"]:
        path = os.path.join(REPO, entry["path"])
        if not os.path.isfile(path):
            unchecked.append(f"{entry['path']}: recorded in the manifest but not on disk")
            continue
        actual = sha256(path)
        if args.update:
            entry["sha256"] = actual
            print(f"  recorded {entry['path']} at {actual}")
            continue
        if actual != entry["sha256"]:
            drifted.append(
                f"{entry['path']}: does not match what was vendored\n"
                f"    recorded {entry['sha256']}\n"
                f"    on disk  {actual}\n"
                f"    Either it was edited here, which is not the place to fix it, or it\n"
                f"    was re-vendored without running --update.")
            continue
        print(f"  {entry['path']}  matches {entry['name']} {entry['version']} "
              f"({entry['commit'][:7]})")

        for checkout in args.upstream:
            up = os.path.join(checkout, entry["upstream_file"])
            if not os.path.isfile(up):
                continue                    # this checkout is a different upstream
            if sha256(up) != actual:
                drifted.append(
                    f"{entry['path']}: upstream has moved on\n"
                    f"    vendored {actual}\n"
                    f"    upstream {sha256(up)}\n"
                    f"    Re-vendor if the upstream change is wanted here.")
            else:
                print(f"    and matches the upstream checkout at {checkout}")

    if args.update:
        with open(MANIFEST, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
        print("manifest updated")
        return 0

    if drifted:
        print("\nVendored files have drifted:\n")
        for item in drifted:
            print(f"  {item}\n")
        return 1
    if unchecked:
        print("\nCould not check every vendored file:\n")
        for item in unchecked:
            print(f"  {item}\n")
        return 2

    print(f"\n{len(manifest['vendored'])} vendored file(s), all matching what was recorded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
