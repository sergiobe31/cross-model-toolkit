#!/usr/bin/env python3
"""Pre-send check for PAL hand-offs (adoption item A, deliberation alfred 17-sep).

Scans the EXACT bytes that would be sent to an external model via PAL: the
attached payload files + the composed prompt (file or stdin). Hard-fails (exit 1)
on a blacklisted filename, a secret-pattern hit, or a pre-committed budget
exceeded. Prints an estimated token count and cost header.

This is a script, not a mental grep — in headless nobody verifies that a mental
grep ran. Invoke it from any skill that sends content to PAL (`debate`,
`interceptor`, future ones).

Usage:
  pal_pre_send_check.py --payload FILE [--payload FILE ...]
                        [--prompt-file FILE | --stdin]
                        [--model SLUG] [--max-tokens N] [--max-usd USD]
                        [--price-table PATH]

Exit codes: 0 = OK (send allowed), 1 = HARD-FAIL (abort pre-send),
2 = usage/config error.
"""

import argparse
import datetime
import json
import os
import re
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
    args = ap.parse_args()

    if not args.payload and not args.prompt_file and not args.stdin:
        print("[pal-pre-send] nothing to scan (--payload / --prompt-file / --stdin)", file=sys.stderr)
        return 2

    failures = []  # (kind, where, label, detail)

    # 1) blacklist by filename (basename and path), then content scan
    texts = []  # (name, content)
    for path in args.payload:
        if not os.path.exists(path):
            print(f"[pal-pre-send] payload file not found: {path}", file=sys.stderr)
            return 2
        hit = blacklisted(path)
        if hit:
            failures.append(("blacklist", path, hit, "blacklisted filename must not be sent"))
        with open(path, encoding="utf-8", errors="replace") as fh:
            texts.append((path, fh.read()))

    if args.prompt_file:
        if not os.path.exists(args.prompt_file):
            print(f"[pal-pre-send] prompt file not found: {args.prompt_file}", file=sys.stderr)
            return 2
        with open(args.prompt_file, encoding="utf-8", errors="replace") as fh:
            texts.append(("<composed-prompt>", fh.read()))
    elif args.stdin:
        texts.append(("<composed-prompt>", sys.stdin.read()))

    for name, content in texts:
        failures.extend(iter_findings(name, content))

    # 2) token estimate + budget
    total_tokens = sum(est_tokens(t) for _, t in texts)
    if args.max_tokens and total_tokens > args.max_tokens:
        failures.append(("budget", "token-estimate", "max-tokens",
                         f"est. {total_tokens} > ceiling {args.max_tokens}"))

    cost_line = "cost estimate: SKIPPED (pass --model)"
    cost_total = None
    if args.model:
        prices, status = load_prices(args.price_table)
        if prices is None:
            cost_line = f"cost estimate: SKIPPED ({status})"
        elif args.model not in prices.get("prices", {}):
            cost_line = (f"cost estimate: SKIPPED (no price for {args.model} — "
                         f"update {args.price_table})")
        elif status != "ok":
            cost_line = f"cost estimate: SKIPPED ({status})"
        else:
            p = prices["prices"][args.model]
            cost_in = total_tokens / 1e6 * p["input"]
            cost_out = total_tokens * args.output_allowance / 1e6 * p["output"]
            cost_total = cost_in + cost_out
            cost_line = (f"cost estimate: ${cost_in:.4f} in + ${cost_out:.4f} "
                         f"out-allowance ({args.output_allowance}x) = ${cost_total:.4f} "
                         f"(prices updated {prices['updated']})")
            if args.max_usd and cost_total > args.max_usd:
                failures.append(("budget", "cost-estimate", "max-usd",
                                 f"est. ${cost_total:.4f} > ceiling ${args.max_usd:.4f}"))

    # 3) header + verdict
    n_files = len(args.payload)
    has_prompt = bool(args.prompt_file or args.stdin)
    print(f"[pal-pre-send] payload: {n_files} file(s)"
          + (" + composed prompt" if has_prompt else ""))
    print(f"[pal-pre-send] est. tokens: ~{total_tokens:,} (chars/4 estimate)")
    print(f"[pal-pre-send] {cost_line}")
    if failures:
        print(f"[pal-pre-send] HARD-FAIL — {len(failures)} finding(s), do NOT send:")
        for kind, where, label, detail in failures:
            print(f"  [{kind}] {label} @ {where}: {detail}")
        return 1
    print("[pal-pre-send] OK — no blacklisted names, no secret-pattern hits, budget within ceiling")
    return 0


if __name__ == "__main__":
    sys.exit(main())
