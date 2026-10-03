#!/usr/bin/env python3
"""Pre-send check for PAL hand-offs (adoption item A, deliberation alfred 17-sep).

Scans the EXACT bytes that would be sent to an external model via PAL: the
attached payload files + the composed prompt (file or stdin). Hard-fails (exit 1)
on a blacklisted filename, a secret-pattern hit, or a pre-committed budget
exceeded. Prints an estimated token count and cost header.

This is a script, not a mental grep — in headless nobody verifies that a mental
grep ran. Invoke it from any skill that sends content to PAL (`debate`,
`interceptor`, future ones).

The scanning/ledger core lives in the importable `run_check()` so the iteration-2
guard server can run the exact same logic in-process on contents it has already
read (no path re-reading). `main()` is a thin shim over it: argument parsing,
existence checks and the usage-error ledger entries stay here, byte-for-byte as
before. `parse_file_blocks()` extracts per-file digests from PAL-fork
`--- BEGIN/END FILE ---` blobs.

Ledger mode (--ledger, or PAL_LEDGER=1 unless --no-ledger) additionally
appends one JSONL record per run to state/pal_send_ledger.jsonl and persists a
copy of the prompt under state/prompts/. A mechanical check rejects --mcp-path
entries not covered by --payload or --declared-extra. Ledger writes are
fail-closed: an unwritable ledger is itself a HARD-FAIL.

Env:
  PAL_LEDGER=1        enable ledger mode unless --no-ledger is given
  PAL_STATE_DIR=PATH  override the state directory (used by tests)

Usage:
  pal_pre_send_check.py --payload FILE [--payload FILE ...]
                        [--prompt-file FILE | --stdin]
                        [--model SLUG] [--max-tokens N] [--max-usd USD]
                        [--price-table PATH]
                        [--ledger | --no-ledger] [--run-id ID]
                        [--mcp-path PATH ...] [--declared-extra PATH ...]
                        [--plan-manifest PATH]

Exit codes: 0 = OK (send allowed), 1 = HARD-FAIL (abort pre-send),
2 = usage/config error.
"""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import random
import re
import string
import sys

# Filenames that must never leave the machine in a PAL hand-off.
BLACKLIST_NAMES = [
    r"(^|/)\.env(\..*)?$",
    r"(^|/)\.npmrc$", r"(^|/)\.netrc$",
    r".*\.pem$", r".*\.key$", r".*\.p12$", r".*\.pfx$", r".*\.keystore$",
    r"(^|/)id_rsa.*$", r"(^|/)id_ed25519.*$", r"(^|/)id_ecdsa.*$",
    r"(^|/)credentials.*\.json$", r"(^|/)service-account.*\.json$",
    r".*secret.*", r".*password.*",
]

# Secret patterns (value heuristics keep obvious doc examples from tripping).
SECRET_PATTERNS = [
    ("openrouter-key", r"sk-or-v1-[0-9a-f]{32,}"),
    ("anthropic-key", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("openai-style-key", r"sk-(?!or-|ant-)[A-Za-z0-9]{20,}"),
    ("aws-access-key", r"AKIA[0-9A-Z]{16}"),
    ("github-token", r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{20,}"),
    ("gitlab-token", r"glpat-[A-Za-z0-9_-]{20,}"),
    ("slack-token", r"xox[bpars]-[A-Za-z0-9-]{10,}"),
    ("jwt", r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),
    ("private-key-block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("credential-assignment",
     r"(?i)\b(api[_-]?key|secret|password|access[_-]?token)\b\s*[=:]\s*['\"]?"
     r"(?=[^'\"\s]*\d)[A-Za-z0-9+/_=-]{16,}"),
]

DEFAULT_PRICE_TABLE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "pal_price_table.json"
)
DEFAULT_OUTPUT_ALLOWANCE = 2.0  # v3: estimate input + 2x input tokens at output price
STALE_WARN_DAYS = 90
TOKENS_PER_CHAR = 1 / 4.0  # rough estimate; documented as estimate, not exact
LEDGER_ROTATE_BYTES = 5 * 1024 * 1024
LEDGER_MAX_LINE_BYTES = 64 * 1024


class UsageInputError(Exception):
    """An input that passed the existence check could not be read at scan time.

    The CLI surfaces this exactly as before: the message on stderr plus the
    usage-error ledger entry, exit code 2. Raised (rather than printed) so
    run_check() stays importable for the guard server, which pre-validates.
    """

class LedgerWriteError(Exception):
    """The fail-closed ledger write failed.

    The CLI surfaces this exactly as before: `[pal-pre-send] HARD-FAIL — …`
    on stderr, exit code 1, with nothing on stdout beyond the run-id line.
    """


def iter_findings(name: str, text: str):
    for label, pattern in SECRET_PATTERNS:
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(pattern, line):
                yield ("secret", f"{name}:{i}", label, line.strip()[:120])


def blacklisted(path: str):
    base = os.path.basename(path)
    for pattern in BLACKLIST_NAMES:
        if re.search(pattern, path) or re.search(pattern, base):
            return pattern
    return None


def est_tokens(text: str) -> int:
    return int(len(text) * TOKENS_PER_CHAR)


def load_prices(path: str):
    if not os.path.exists(path):
        return None, f"price table not found: {path}"
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return None, f"price table unreadable: {exc}"
    updated = data.get("updated", "")
    try:
        age = (datetime.date.today() - datetime.date.fromisoformat(updated)).days
    except ValueError:
        age = None
    return data, ("ok" if age is not None and age <= STALE_WARN_DAYS else
                  f"price table stale (updated {updated}, >{STALE_WARN_DAYS}d)")


def canon(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def state_dir() -> str:
    override = os.environ.get("PAL_STATE_DIR")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_ledger(state: str, entry: dict, prompt_copy) -> str:
    try:
        os.makedirs(os.path.join(state, "prompts"), exist_ok=True)
        line = (json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8")
        if len(line) > LEDGER_MAX_LINE_BYTES:
            return f"ledger line exceeds {LEDGER_MAX_LINE_BYTES} bytes"
        if prompt_copy is not None:
            with open(prompt_copy[0], "w", encoding="utf-8") as fh:
                fh.write(prompt_copy[1])
        ledger = os.path.join(state, "pal_send_ledger.jsonl")
        lock_fd = os.open(os.path.join(state, "ledger.lock"),
                          os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if os.path.exists(ledger) and os.path.getsize(ledger) > LEDGER_ROTATE_BYTES:
                os.replace(ledger, os.path.join(state, "pal_send_ledger.1.jsonl"))
            fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        finally:
            os.close(lock_fd)
    except OSError as exc:
        return f"ledger write failed: {exc}"
    return None


def gen_run_id() -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{stamp}-{suffix}"


_BEGIN_FILE_RE = re.compile(r"^--- BEGIN FILE: (.+?) \(Last modified: .*\) ---$")
_END_FILE_RE = re.compile(r"^--- END FILE: (.+?) ---$")


def parse_file_blocks(blob: str) -> dict:
    """Parse PAL-fork file blocks out of a blob into per-file content digests.

    Block format (one per file; marker lines appear verbatim in the blob)::

        --- BEGIN FILE: {path} (Last modified: {mtime}) ---
        {contenido}
        --- END FILE: {path} ---

    Returns ``{path: sha256_hex_del_contenido_utf8}`` for every closed block,
    in order of appearance. The content of a block is exactly the lines
    between its BEGIN and END marker lines, joined with ``\\n`` (markers
    excluded); a line that merely looks like a marker but does not match the
    full pattern above is content, not a boundary.

    The parse is best-effort: a block that is never closed is ignored, and a
    new BEGIN abandons any previously open block — when a block is missing
    from the result, the caller must fall back to its own source of the bytes.
    """
    blocks = {}
    current_path = None
    current_lines = []
    for line in blob.splitlines():
        m = _BEGIN_FILE_RE.match(line)
        if m:
            current_path = m.group(1)
            current_lines = []
            continue
        m = _END_FILE_RE.match(line)
        if m and current_path is not None and m.group(1) == current_path:
            content = "\n".join(current_lines)
            blocks[current_path] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            current_path = None
            current_lines = []
            continue
        if current_path is not None:
            current_lines.append(line)
    return blocks


def run_check(
    *,
    payload_paths=None,        # modo CLI: lee/stat/hashea como main() hace hoy
    payload_entries=None,      # modo guard: entries pre-computadas
                               # [{path, realpath, sha256, inode, size, mtime}];
                               # si se da, no se re-leen paths
    scan_texts=None,           # lista explicita de (name, content) a escanear;
                               # si None, se construye desde payloads+prompt
    prompt_content=None,       # contenido del prompt (copy persistida y scan)
    prompt_sha256=None,        # sha256 ya calculado del prompt (modo guard);
                               # si None y hay prompt_file, sha256_file(prompt_file)
    prompt_file=None,          # modo CLI: path del prompt (lectura + sha256_file)
    model=None,
    mcp_paths=(),
    declared_extra=(),
    coverage_extra=(),         # modo guard: paths añadidos al conjunto de cobertura
                               # (processed ∪ history_files) sin ser payload del delta;
                               # el CLI nunca lo pasa -> bit-identidad intacta
    plan_manifest=None,
    coverage_fail_hard=False,  # modo guard: missing_from_payload del plan
                               # bundle pasa a failure (verdict hard_fail);
                               # el CLI nunca lo pasa -> bit-identidad intacta
    ledger_mode=False,
    run_id=None,               # si None y ledger_mode: gen_run_id()
    max_tokens=None,
    max_usd=None,
    price_table=DEFAULT_PRICE_TABLE,
    output_allowance=DEFAULT_OUTPUT_ALLOWANCE,
    state=None,                # si None: state_dir()
    extra_entry_fields=None,   # dict MERGEADO en la entry del ledger (modo guard);
                               # en modo CLI es None -> entry byte-identica
):
    """Run the scan/budget/ledger core. Importable; see module docstring.

    Returns ``(exit_code, entry_or_None, failures, output_lines)`` where
    ``failures`` is a list of ``(kind, where, label, detail)`` tuples and
    ``output_lines`` the stdout lines main() prints verbatim (the run-id line
    is main()'s, printed before calling this). ``entry`` is the ledger record
    written when ledger_mode, else None.

    Raises UsageInputError (caller maps to exit 2 + usage-error ledger entry;
    main() has already done the existence checks this corresponds to) and
    LedgerWriteError (caller maps to exit 1 + stderr HARD-FAIL).

    Guard mode: ``coverage_extra`` extends the coverage set — its canonized
    paths count for the mcp-path check AND for the plan-manifest exclusions
    (``missing_from_payload``/``extra_in_payload`` are computed against
    ``payload_canon | coverage_canon``). ``coverage_fail_hard`` (guard mode
    only) additionally turns every ``missing_from_payload`` path into a
    hard failure, so a blocked send is ledgered as ``verdict:"hard_fail"``
    instead of "ok". The CLI never passes either flag, so CLI behavior
    stays bit-identical; ``exclusions`` is emitted the same in both modes.
    """
    if state is None:
        state = state_dir()
    if run_id is None and ledger_mode:
        run_id = gen_run_id()

    failures = []  # (kind, where, label, detail)
    texts = []  # (name, content)
    payload_entries = list(payload_entries) if payload_entries is not None else []

    if not payload_entries and payload_paths is None and scan_texts is None:
        # nothing to scan: mirror the CLI usage error without touching the
        # ledger (main() owns usage_error_entry and has already checked this)
        raise UsageInputError(
            "[pal-pre-send] nothing to scan (--payload / --prompt-file / --stdin)")

    if payload_paths is not None:
        # 1a) blacklist by filename (basename and path), then read/stat/hash
        for path in payload_paths:
            hit = blacklisted(path)
            if hit:
                failures.append(("blacklist", path, hit, "blacklisted filename must not be sent"))
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                st = os.stat(path)
                digest = sha256_file(path)
            except OSError as exc:
                raise UsageInputError(
                    f"[pal-pre-send] payload unreadable: {path}: {exc}") from exc
            texts.append((path, content))
            payload_entries.append({
                "path": path,
                "realpath": canon(path),
                "sha256": digest,
                "inode": st.st_ino,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
    else:
        # 1b) guard mode: entries pre-computed, pero el blacklist por nombre
        # sigue aplicando (no requiere leer el fichero)
        for e in payload_entries:
            hit = blacklisted(e["path"])
            if hit:
                failures.append(("blacklist", e["path"], hit,
                                 "blacklisted filename must not be sent"))

    if scan_texts is not None:
        texts = list(scan_texts)
    if prompt_content is None and prompt_file is not None:
        if not os.path.exists(prompt_file):
            raise UsageInputError(f"[pal-pre-send] prompt file not found: {prompt_file}")
        try:
            with open(prompt_file, encoding="utf-8", errors="replace") as fh:
                prompt_content = fh.read()
        except OSError as exc:
            raise UsageInputError(
                f"[pal-pre-send] prompt file unreadable: {prompt_file}: {exc}") from exc
    if prompt_content is not None:
        texts.append(("<composed-prompt>", prompt_content))

    for name, content in texts:
        failures.extend(iter_findings(name, content))

    # mechanical check (ledger mode only): every mcp_path must be covered
    if ledger_mode:
        payload_canon = {e["realpath"] for e in payload_entries}
        declared_canon = {canon(p) for p in declared_extra}
        coverage_canon = {canon(p) for p in coverage_extra}
        covered = payload_canon | declared_canon | coverage_canon
        for path in mcp_paths:
            if canon(path) not in covered:
                failures.append(("mcp-path", path, "outside payload/declared-extra",
                                 f"{canon(path)} is not a payload nor a declared extra"))

    # 2) token estimate + budget
    total_tokens = sum(est_tokens(t) for _, t in texts)
    if max_tokens and total_tokens > max_tokens:
        failures.append(("budget", "token-estimate", "max-tokens",
                         f"est. {total_tokens} > ceiling {max_tokens}"))

    cost_line = "cost estimate: SKIPPED (pass --model)"
    cost_total = None
    if model:
        prices, status = load_prices(price_table)
        if prices is None:
            cost_line = f"cost estimate: SKIPPED ({status})"
        elif model not in prices.get("prices", {}):
            cost_line = (f"cost estimate: SKIPPED (no price for {model} — "
                         f"update {price_table})")
        elif status != "ok":
            cost_line = f"cost estimate: SKIPPED ({status})"
        else:
            p = prices["prices"][model]
            cost_in = total_tokens / 1e6 * p["input"]
            cost_out = total_tokens * output_allowance / 1e6 * p["output"]
            cost_total = cost_in + cost_out
            cost_line = (f"cost estimate: ${cost_in:.4f} in + ${cost_out:.4f} "
                         f"out-allowance ({output_allowance}x) = ${cost_total:.4f} "
                         f"(prices updated {prices['updated']})")
            if max_usd and cost_total > max_usd:
                failures.append(("budget", "cost-estimate", "max-usd",
                                 f"est. ${cost_total:.4f} > ceiling ${max_usd:.4f}"))

    # 3) ledger record (fail-closed here)
    entry = None
    if ledger_mode:
        prompt_entry = None
        prompt_copy = None
        if prompt_content is not None:
            copy_path = os.path.join(
                state, "prompts",
                f"{run_id}_{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.prompt.txt")
            prompt_copy = (copy_path, prompt_content)
            if prompt_sha256 is not None:
                prompt_digest = prompt_sha256
            elif prompt_file is not None:
                prompt_digest = sha256_file(prompt_file)
            else:
                prompt_digest = None
            prompt_entry = {
                "sha256": prompt_digest,
                "copy_path": copy_path,
            }
        plan_manifest_entry = None
        exclusions = None
        if plan_manifest:
            plan_manifest_entry = {"path": plan_manifest, "sha256": None}
            try:
                plan_manifest_entry["sha256"] = sha256_file(plan_manifest)
                with open(plan_manifest, encoding="utf-8") as fh:
                    manifest = json.load(fh)
                manifest_paths = {canon(f["path"]) for f in manifest.get("files", [])}
                guard_covered = payload_canon | coverage_canon
                exclusions = {
                    "missing_from_payload": sorted(manifest_paths - guard_covered),
                    "extra_in_payload": sorted(guard_covered - manifest_paths),
                }
            except (OSError, ValueError, KeyError, TypeError) as exc:
                failures.append(("plan-manifest", plan_manifest, "unreadable",
                                 f"cannot read plan manifest: {exc}"))
        if coverage_fail_hard and exclusions:
            # guard mode: the plan bundle is a pre-committed contract — a
            # declared file missing from the covered set blocks the send and
            # the ledger must say hard_fail, not "ok"
            for p in exclusions.get("missing_from_payload") or []:
                failures.append(("plan-manifest", p, "missing_from_payload",
                                 "declared in the plan bundle but not sent "
                                 "nor in history"))
        entry = {
            "run_id": run_id,
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "model": model,
            "verdict": "hard_fail" if failures else "ok",
            "prompt": prompt_entry,
            "payloads": payload_entries,
            "mcp_paths": [canon(p) for p in mcp_paths],
            "declared_extra": [canon(p) for p in declared_extra],
            "plan_manifest": plan_manifest_entry,
            "exclusions": exclusions,
            "est_tokens": total_tokens,
            "est_cost_usd": cost_total,
            "failures": [{"kind": k, "where": w, "label": l, "detail": d}
                         for k, w, l, d in failures],
        }
        if extra_entry_fields:
            entry.update(extra_entry_fields)
        err = write_ledger(state, entry, prompt_copy)
        if err:
            raise LedgerWriteError(err)

    # 4) header + verdict
    n_files = len(payload_paths) if payload_paths is not None else len(payload_entries)
    has_prompt = prompt_content is not None
    output_lines = [
        f"[pal-pre-send] payload: {n_files} file(s)"
        + (" + composed prompt" if has_prompt else ""),
        f"[pal-pre-send] est. tokens: ~{total_tokens:,} (chars/4 estimate)",
        f"[pal-pre-send] {cost_line}",
    ]
    if failures:
        output_lines.append(
            f"[pal-pre-send] HARD-FAIL — {len(failures)} finding(s), do NOT send:")
        for kind, where, label, detail in failures:
            output_lines.append(f"  [{kind}] {label} @ {where}: {detail}")
        return 1, entry, failures, output_lines
    output_lines.append(
        "[pal-pre-send] OK — no blacklisted names, no secret-pattern hits, budget within ceiling")
    return 0, entry, failures, output_lines


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--payload", action="append", default=[], help="file sent to PAL (repeatable)")
    ap.add_argument("--prompt-file", help="composed prompt exported to a file")
    ap.add_argument("--stdin", action="store_true", help="read composed prompt from stdin")
    ap.add_argument("--model", help="OpenRouter slug for the cost estimate")
    ap.add_argument("--max-tokens", type=int, help="pre-committed dossier ceiling (hard-fail above)")
    ap.add_argument("--max-usd", type=float, help="pre-committed cost ceiling (hard-fail above)")
    ap.add_argument("--price-table", default=DEFAULT_PRICE_TABLE)
    ap.add_argument("--output-allowance", type=float, default=DEFAULT_OUTPUT_ALLOWANCE)
    ap.add_argument("--ledger", action="store_true", help="append a JSONL record to the send ledger")
    ap.add_argument("--no-ledger", action="store_true", help="disable ledger even if PAL_LEDGER=1")
    ap.add_argument("--run-id", help="id recorded in the ledger (default: generated)")
    ap.add_argument("--mcp-path", action="append", default=[],
                    help="file a PAL tool would read server-side (repeatable)")
    ap.add_argument("--declared-extra", action="append", default=[],
                    help="extra file declared as intentionally sent (repeatable)")
    ap.add_argument("--plan-manifest", help="JSON manifest {files:[{path,sha256}]} for diffing")
    args = ap.parse_args()

    ledger_mode = args.ledger or (os.environ.get("PAL_LEDGER") == "1" and not args.no_ledger)

    if ledger_mode and args.stdin:
        print("[pal-pre-send] --stdin cannot be combined with ledger mode", file=sys.stderr)
        return 2

    run_id = None
    if ledger_mode:
        run_id = args.run_id or gen_run_id()

    state = state_dir()

    def usage_error_entry():
        if not ledger_mode:
            return
        entry = {
            "run_id": run_id,
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "model": args.model,
            "verdict": "usage_error",
            "prompt": None,
            "payloads": [],
            "mcp_paths": [canon(p) for p in args.mcp_path],
            "declared_extra": [canon(p) for p in args.declared_extra],
            "plan_manifest": None,
            "exclusions": None,
            "est_tokens": None,
            "est_cost_usd": None,
            "failures": [],
        }
        write_ledger(state, entry, None)

    if not args.payload and not args.prompt_file and not args.stdin:
        print("[pal-pre-send] nothing to scan (--payload / --prompt-file / --stdin)", file=sys.stderr)
        usage_error_entry()
        return 2

    if ledger_mode:
        print(f"[pal-pre-send] run-id: {run_id}")

    # existence checks for payloads only; the prompt file is validated inside
    # run_check right after the payload loop, so first-error-wins ordering on
    # compound errors (unreadable payload + bad prompt) is exactly as before
    for path in args.payload:
        if not os.path.exists(path):
            print(f"[pal-pre-send] payload file not found: {path}", file=sys.stderr)
            usage_error_entry()
            return 2

    prompt_content = sys.stdin.read() if args.stdin else None

    try:
        exit_code, _entry, _failures, output_lines = run_check(
            payload_paths=args.payload,
            prompt_content=prompt_content,
            prompt_file=args.prompt_file,
            model=args.model,
            mcp_paths=args.mcp_path,
            declared_extra=args.declared_extra,
            plan_manifest=args.plan_manifest,
            ledger_mode=ledger_mode,
            run_id=run_id,
            max_tokens=args.max_tokens,
            max_usd=args.max_usd,
            price_table=args.price_table,
            output_allowance=args.output_allowance,
            state=state,
        )
    except UsageInputError as exc:
        print(str(exc), file=sys.stderr)
        usage_error_entry()
        return 2
    except LedgerWriteError as exc:
        print(f"[pal-pre-send] HARD-FAIL — {exc}", file=sys.stderr)
        return 1

    for line in output_lines:
        print(line)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
