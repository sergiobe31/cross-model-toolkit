#!/usr/bin/env python3
"""Plan-bundle manifest generator for PAL hand-offs.

Run in PLANNING, over the file(s) the plan was written to. The manifest is
later passed as --plan-manifest to pal_pre_send_check.py, which diffs its
paths against the payloads actually attached at send time.

aggregate_sha256 is the sha256 of the hex hashes concatenated in the order
of their canonical paths (os.path.realpath(os.path.abspath(p))): it is
invariant to the order of the --file arguments. Two distinct paths that
canonize to the same realpath are a usage error.

Usage:
  pal_plan_manifest.py --file PATH [--file PATH ...] --out PATH

Exit codes: 0 = OK, 2 = usage/config error (missing --file, unreadable
file, unwritable --out, duplicate canonical path).
"""

import argparse
import datetime
import hashlib
import json
import os
import sys


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", action="append", default=[],
                    help="plan file to hash (repeatable, at least one)")
    ap.add_argument("--out", help="output path for the manifest JSON")
    args = ap.parse_args()

    if not args.file:
        print("[pal-plan-manifest] nothing to hash (pass --file)", file=sys.stderr)
        return 2
    if not args.out:
        print("[pal-plan-manifest] --out is required", file=sys.stderr)
        return 2

    entries = []  # (canonical_path, given_path, hex_digest)
    seen = set()
    for path in args.file:
        canon = os.path.realpath(os.path.abspath(path))
        if canon in seen:
            print(f"[pal-plan-manifest] duplicate canonical path: {canon}", file=sys.stderr)
            return 2
        seen.add(canon)
        if not os.path.exists(path):
            print(f"[pal-plan-manifest] file not found: {path}", file=sys.stderr)
            return 2
        entries.append((canon, path, sha256_file(path)))

    ordered = sorted(entries, key=lambda e: e[0])
    aggregate = hashlib.sha256("".join(e[2] for e in ordered).encode("ascii")).hexdigest()

    manifest = {
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "files": [{"path": p, "sha256": d} for _, p, d in ordered],
        "aggregate_sha256": aggregate,
    }
    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        print(f"[pal-plan-manifest] cannot write {args.out}: {exc}", file=sys.stderr)
        return 2

    print(f"[pal-plan-manifest] aggregate: {aggregate} ({len(entries)} file(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
