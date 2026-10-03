#!/usr/bin/env python3
"""Response verifier for the guarded PAL server (Guardrail iteración 2).

Closes the deliberation↔sidecar loop: the guard records, per run, the
canonical response hash in ``state/pal_guard_responses.jsonl``
(``response_sha256``); this script recomputes the hash from a saved response
file and compares. Any mismatch or missing record is exit 1.

Canonical contract (must stay in sync with ``pal_guarded_server.py``):
the audited artifact is the tool RESULT, a list of TextContent; the canonical
hash is::

    sha256("\n".join(c.text for c in result))          # hex digest

For the chat tool, ``result`` carries ONE TextContent whose ``.text`` is the
JSON of the ToolOutput — so the canonical string IS that full ToolOutput JSON
(``ToolOutput.content`` is a plain string field, NOT a list). Extraction rules
for --response-json FILE:

  1. FILE parses as JSON with ``content`` a LIST of {text}    -> join the
     ``text`` values with "\\n" (MCP-result wrapper serialization; a list of
     one element yields exactly that element's text, i.e. the ToolOutput JSON).
  2. FILE parses as a JSON DICT with a string ``text`` field and no list
     ``content`` (a serialized TextContent, ``{"type":"text","text":...}``)
     -> the canonical string is ``data["text"]``.
  3. ANY OTHER shape — not JSON, a JSON list, a JSON dict with ``content`` a
     plain string (a bare ToolOutput dump), any other dict —               ->
     use the raw file text verbatim. Descending into a string ``content``
     field would hash the wrong string.

Usage:
  pal_guard_verify.py --run-id ID --response-json FILE [--state DIR]

Exit codes: 0 = hash matches the sidecar record; 1 = mismatch, no
response_ok record for the run_id, or unreadable inputs. Env PAL_STATE_DIR
overrides the default state dir (anchored to the plugin scripts dir).
"""

import argparse
import hashlib
import json
import os
import sys


def state_dir():
    override = os.environ.get("PAL_STATE_DIR")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")


def extract_response_text(raw: str) -> str:
    """Apply the extraction rules from the module docstring."""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(data, dict) and isinstance(data.get("content"), list):
        texts = []
        for item in data["content"]:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
            else:
                texts.append(str(item))
        return "\n".join(texts)
    if isinstance(data, dict) and isinstance(data.get("text"), str):
        # serialized TextContent ({"type": "text", "text": ...}): the
        # canonical string is the text field itself (rule 2; a dict with a
        # list ``content`` already returned under rule 1)
        return data["text"]
    return raw


def find_sidecar_record(state: str, run_id: str):
    """Return the LAST response record for run_id, or None."""
    path = os.path.join(state, "pal_guard_responses.jsonl")
    if not os.path.exists(path):
        return None
    found = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("run_id") == run_id:
                    found = rec
    except OSError:
        return None
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-id", required=True,
                    help="run_id recorded by the guard sidecar")
    ap.add_argument("--response-json", required=True,
                    help="file holding the response to re-hash")
    ap.add_argument("--state", help="state dir (default: PAL_STATE_DIR or "
                    "<plugin>/state)")
    args = ap.parse_args(argv)

    state = args.state or state_dir()

    try:
        with open(args.response_json, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        print(f"[pal-guard-verify] cannot read {args.response_json}: {exc}",
              file=sys.stderr)
        return 1

    text = extract_response_text(raw)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    record = find_sidecar_record(state, args.run_id)
    if record is None:
        print(f"[pal-guard-verify] FAIL — no sidecar record for run_id "
              f"{args.run_id} in {os.path.join(state, 'pal_guard_responses.jsonl')}")
        return 1
    if record.get("phase") != "response_ok":
        print(f"[pal-guard-verify] FAIL — last record for run_id {args.run_id} "
              f"has phase={record.get('phase')!r} (expected 'response_ok'); the "
              f"guard never saw a successful response for this run")
        return 1
    expected = record.get("response_sha256")
    if expected != digest:
        print(f"[pal-guard-verify] FAIL — response hash mismatch for run_id "
              f"{args.run_id}:")
        print(f"  recomputed: {digest}")
        print(f"  sidecar   : {expected}")
        return 1
    print(f"[pal-guard-verify] OK — run_id {args.run_id}: response hash "
          f"{digest} matches the guard sidecar "
          f"(phase=response_ok, model={record.get('model')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
