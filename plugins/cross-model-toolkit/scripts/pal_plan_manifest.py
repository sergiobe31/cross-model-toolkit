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
  pal_plan_manifest.py --file PATH [--file PATH ...] --out PATH [--slug SLUG]

``--slug SLUG`` additionally writes the manifest ATOMICALLY (tmp + rename) to
``state/manifests/<slug>.json`` — the location the guarded PAL server looks
up for STRICT plan-bundle enforcement (one manifest per debate/plan).

Exit codes: 0 = OK, 2 = usage/config error (missing --file, unreadable
file, unwritable --out, duplicate canonical path, invalid slug).
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _state_dir():
    override = os.environ.get("PAL_STATE_DIR")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")


def write_manifest_atomic(manifest: dict, dest_dir: str, slug: str) -> str:
    """Write state/manifests/<slug>.json atomically (tmp + os.replace)."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, slug + ".json")
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, dest)
    return dest


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", action="append", default=[],
                    help="plan file to hash (repeatable, at least one)")
    ap.add_argument("--out", help="output path for the manifest JSON")
    ap.add_argument("--slug",
                    help="also write the manifest atomically to "
                         "state/manifests/<slug>.json (STRICT guard lookup)")
    args = ap.parse_args(argv)

    if not args.file:
        print("[pal-plan-manifest] nothing to hash (pass --file)", file=sys.stderr)
        return 2
    if not args.out:
        print("[pal-plan-manifest] --out is required", file=sys.stderr)
        return 2
    if args.slug is not None and (not SLUG_RE.match(args.slug)
                                  or args.slug in (".", "..")):
        print(f"[pal-plan-manifest] invalid slug: {args.slug!r} "
              "(must match ^[A-Za-z0-9._-]+$; path traversal rejected)",
              file=sys.stderr)
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
    if args.slug:
        dest = write_manifest_atomic(manifest,
                                     os.path.join(_state_dir(), "manifests"),
                                     args.slug)
        print(f"[pal-plan-manifest] slug {args.slug} -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
