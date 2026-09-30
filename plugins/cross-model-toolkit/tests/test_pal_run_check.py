"""Tests for the importable run_check() and parse_file_blocks() helpers.

Iteration 2 of the guard work needs the pal_pre_send_check core in-process:
the guard server has already read the contents, so run_check() must accept
pre-computed payload entries, explicit scan texts and a pre-hashed prompt,
without re-reading a single path. These tests pin that contract AND the
bit-identity of the CLI surface after the refactor.

Golden mechanism (CLI vs run_check): each golden case runs the same inputs
through (a) the CLI subprocess and (b) run_check() imported in-process,
both with a fixed run_id and the same PAL_STATE_DIR, and requires:

  - stdout identical — main() prints the run-id line itself before calling
    run_check(), so the in-process expected stdout is that line plus the
    output_lines run_check() returns (each printed with a trailing newline);
  - the ledger line byte-identical after normalizing the wall-clock fields
    (``ts`` and the stamp embedded in ``prompt.copy_path``, both from
    datetime.now()). Normalization is load -> zero the field -> re-dump with
    separators=(",", ":"); CPython preserves dict order through loads/dumps,
    so a key-order drift in the written entry would still fail the compare.
    Payload stat fields (inode/size/mtime) are NOT normalized: both modes
    stat the same files, so they must already match.

Everything runs under PAL_STATE_DIR pointing into tmp_path; nothing touches
the repo's real state/ dir.
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

SCRIPT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "scripts", "pal_pre_send_check.py"))


def load_module():
    spec = importlib.util.spec_from_file_location("pal_pre_send_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_cli(args, env_extra=None, cwd=None, input_text=None):
    env = dict(os.environ)
    env.pop("PAL_LEDGER", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, SCRIPT] + args,
        capture_output=True, text=True, env=env, cwd=cwd, input=input_text)


def make_payload(tmp_path, name="payload.txt", content="hello world\n"):
    p = tmp_path / name
    p.write_text(content)
    return p


def make_prompt(tmp_path, content="review this code please\n"):
    p = tmp_path / "prompt.txt"
    p.write_text(content)
    return p


def read_ledger_line(state_dir):
    ledger = state_dir / "pal_send_ledger.jsonl"
    assert ledger.exists()
    lines = ledger.read_text().splitlines()
    assert len(lines) == 1
    return lines[0]


def normalize_ledger_line(line):
    """Zero the datetime- and state-dir-derived fields so runs compare.

    ``run_id`` is fixed by the caller on both sides and needs no normalization.
    ``ts``, the stamp inside ``prompt.copy_path`` (both from datetime.now())
    and the state-dir prefix of ``copy_path`` (tests may use different state
    dirs for the CLI and the in-process run) are zeroed. Everything else in
    the line — payload stat fields included — must already be byte-identical.
    """
    e = json.loads(line)
    e["ts"] = "<ts>"
    if e.get("prompt"):
        copy_path = re.sub(r"^.*?/prompts/", "<state>/prompts/",
                           e["prompt"]["copy_path"])
        e["prompt"]["copy_path"] = re.sub(
            r"\d{8}-\d{6}(?=\.prompt\.txt$)", "<stamp>", copy_path)
    return json.dumps(e, separators=(",", ":"))


def run_golden(tmp_path, *, prompt_content, payload=None, payload_name="payload.txt",
               extra_cli_args=(), run_check_extra=None):
    """Run one case through the CLI and through run_check(); assert identity.

    Returns (cli_result, inprocess_exit_code, inprocess_entry, inprocess_failures).
    """
    state = tmp_path / "state"
    payload = payload or make_payload(tmp_path, name=payload_name)
    prompt = make_prompt(tmp_path, content=prompt_content)
    args = ["--payload", str(payload), "--prompt-file", str(prompt),
            "--ledger", "--run-id", "golden-run"] + list(extra_cli_args)
    r = run_cli(args, env_extra={"PAL_STATE_DIR": str(state)})
    line_cli = read_ledger_line(state)

    shutil.rmtree(state)
    mod = load_module()
    kw = dict(
        payload_paths=[str(payload)],
        prompt_file=str(prompt),
        prompt_content=prompt_content,
        ledger_mode=True,
        run_id="golden-run",
        state=str(state),
    )
    kw.update(run_check_extra or {})
    exit_code, entry, failures, out_lines = mod.run_check(**kw)

    expected_stdout = "".join(
        line + "\n" for line in [f"[pal-pre-send] run-id: golden-run"] + out_lines)
    assert expected_stdout == r.stdout, (
        f"stdout mismatch:\nCLI:\n{r.stdout}\nrun_check:\n{expected_stdout}")
    assert exit_code == r.returncode
    line_rc = read_ledger_line(state)
    assert normalize_ledger_line(line_rc) == normalize_ledger_line(line_cli)
    return r, exit_code, entry, failures


# --- 1) golden CLI <-> run_check, six cases ---------------------------------

def test_golden_ok_simple(tmp_path):
    r, code, entry, failures = run_golden(tmp_path, prompt_content="looks fine\n")
    assert r.returncode == 0
    assert code == 0
    assert entry["verdict"] == "ok"
    assert failures == []


def test_golden_hard_fail_secret_in_prompt(tmp_path):
    secret_line = "api_key = sk-or-v1-" + "a" * 32
    r, code, entry, failures = run_golden(
        tmp_path, prompt_content=f"please review this\n{secret_line}\n")
    assert r.returncode == 1
    assert code == 1
    assert entry["verdict"] == "hard_fail"
    assert any(f["label"] == "openrouter-key" for f in entry["failures"])
    assert any(f[0] == "secret" for f in failures)


def test_golden_blacklist_by_filename(tmp_path):
    r, code, entry, _ = run_golden(
        tmp_path, prompt_content="fine\n", payload_name="creds.pem")
    assert r.returncode == 1
    assert code == 1
    assert entry["verdict"] == "hard_fail"
    assert any(f["kind"] == "blacklist" for f in entry["failures"])


def test_golden_budget_max_tokens(tmp_path):
    r, code, entry, _ = run_golden(
        tmp_path, prompt_content="too long for this ceiling\n",
        extra_cli_args=["--max-tokens", "5"],
        run_check_extra={"max_tokens": 5})
    assert r.returncode == 1
    assert code == 1
    assert entry["verdict"] == "hard_fail"
    assert any(f["label"] == "max-tokens" for f in entry["failures"])


def test_golden_mcp_path_outside_payload(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("not declared\n")
    r, code, entry, _ = run_golden(
        tmp_path, prompt_content="fine\n",
        extra_cli_args=["--mcp-path", str(outside)],
        run_check_extra={"mcp_paths": [str(outside)]})
    assert r.returncode == 1
    assert code == 1
    assert entry["verdict"] == "hard_fail"
    assert any(f["kind"] == "mcp-path" for f in entry["failures"])


def test_golden_plan_manifest_exclusions_diff(tmp_path):
    payload = make_payload(tmp_path, name="a.txt")
    b = tmp_path / "b.txt"
    b.write_text("in manifest only\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": [
        {"path": str(payload), "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()},
        {"path": str(b), "sha256": hashlib.sha256(b.read_bytes()).hexdigest()},
    ]}))
    r, code, entry, _ = run_golden(
        tmp_path, prompt_content="fine\n", payload=payload,
        extra_cli_args=["--plan-manifest", str(manifest)],
        run_check_extra={"plan_manifest": str(manifest)})
    assert r.returncode == 0
    assert code == 0
    assert entry["exclusions"]["missing_from_payload"] == [str(b)]
    assert entry["exclusions"]["extra_in_payload"] == []


# --- 2) parse_file_blocks ----------------------------------------------------

def test_parse_file_blocks_single():
    mod = load_module()
    blob = ("\n--- BEGIN FILE: src/a.py (Last modified: 2026-09-30 10:00:00) ---\n"
            "print('hi')\n"
            "--- END FILE: src/a.py ---\n")
    out = mod.parse_file_blocks(blob)
    assert out == {"src/a.py": hashlib.sha256(b"print('hi')").hexdigest()}


def test_parse_file_blocks_two_order_preserved():
    mod = load_module()
    blob = (
        "\n--- BEGIN FILE: a.txt (Last modified: m1) ---\nalpha\n--- END FILE: a.txt ---\n"
        "--- BEGIN FILE: b.txt (Last modified: m2) ---\n"
        "bravo\ncharlie\n--- END FILE: b.txt ---\n")
    out = mod.parse_file_blocks(blob)
    assert list(out) == ["a.txt", "b.txt"]
    assert out["a.txt"] == hashlib.sha256(b"alpha").hexdigest()
    assert out["b.txt"] == hashlib.sha256("bravo\ncharlie".encode("utf-8")).hexdigest()


def test_parse_file_blocks_almost_marker_is_content():
    mod = load_module()
    # a line that looks like an END marker but lacks the path/closing dashes
    # must be treated as content, not close the block
    blob = ("\n--- BEGIN FILE: a.txt (Last modified: m) ---\n"
            "line1\n"
            "--- END FILE:\n"
            "line2\n"
            "--- END FILE: a.txt ---\n")
    out = mod.parse_file_blocks(blob)
    assert out == {"a.txt": hashlib.sha256(
        "line1\n--- END FILE:\nline2".encode("utf-8")).hexdigest()}


def test_parse_file_blocks_unclosed_block_ignored():
    mod = load_module()
    blob = ("\n--- BEGIN FILE: a.txt (Last modified: m) ---\n"
            "alpha\n--- END FILE: a.txt ---\n"
            "--- BEGIN FILE: b.txt (Last modified: m) ---\n"
            "never closed\n")
    out = mod.parse_file_blocks(blob)
    assert list(out) == ["a.txt"]
    assert out["a.txt"] == hashlib.sha256(b"alpha").hexdigest()


# --- 3) entry core + extra_entry_fields ---------------------------------------

def test_entry_core_bytes_match_cli_and_extra_fields_merge(tmp_path):
    state = tmp_path / "state"
    payload = make_payload(tmp_path)
    prompt = make_prompt(tmp_path)
    args = ["--payload", str(payload), "--prompt-file", str(prompt),
            "--ledger", "--run-id", "core-run"]
    r = run_cli(args, env_extra={"PAL_STATE_DIR": str(state)})
    assert r.returncode == 0
    line_cli = normalize_ledger_line(read_ledger_line(state))

    mod = load_module()
    base = dict(payload_paths=[str(payload)], prompt_file=str(prompt),
                prompt_content=prompt.read_text(),
                ledger_mode=True, run_id="core-run")

    state_plain = tmp_path / "state_plain"
    code, entry, failures, out_lines = mod.run_check(state=str(state_plain), **base)
    assert code == 0
    line_plain = read_ledger_line(state_plain)
    # sin extra_entry_fields la entry es la misma (modulo ts) que la del CLI
    assert normalize_ledger_line(line_plain) == line_cli

    state_extra = tmp_path / "state_extra"
    code, entry, failures, out_lines = mod.run_check(
        state=str(state_extra),
        extra_entry_fields={"guard": "guard-srv", "channel": "mcp"},
        **base)
    assert code == 0
    e = json.loads(normalize_ledger_line(read_ledger_line(state_extra)))
    assert e["guard"] == "guard-srv"
    assert e["channel"] == "mcp"
    for k in ("guard", "channel"):
        e.pop(k)
    # los campos mergeados se añaden al final; el resto de la entry no cambia
    assert json.dumps(e, separators=(",", ":")) == normalize_ledger_line(line_plain)


# --- 4) guard mode: pre-computed entries, no path re-read ---------------------

def test_guard_mode_precomputed_entries_no_reread(tmp_path):
    state = tmp_path / "state"
    mod = load_module()
    never_written = tmp_path / "does_not_exist.py"  # must never be opened
    entry_in = {
        "path": str(never_written),
        "realpath": os.path.realpath(os.path.abspath(str(never_written))),
        "sha256": "aa" * 32,
        "inode": 123456789,
        "size": 5,
        "mtime": 1.5,
    }
    code, entry, failures, out_lines = mod.run_check(
        payload_entries=[entry_in],
        scan_texts=[(str(never_written), "clean content\n")],
        prompt_content="ok to send\n",
        prompt_sha256="bb" * 32,
        ledger_mode=True,
        run_id="guard-run",
        state=str(state),
    )
    assert code == 0, failures
    assert failures == []
    assert entry["payloads"] == [entry_in]  # se usa lo dado, no lo re-leído
    assert entry["prompt"]["sha256"] == "bb" * 32
    e = json.loads(read_ledger_line(state))
    assert e["run_id"] == "guard-run"
    assert e["payloads"][0]["sha256"] == "aa" * 32
    copy_path = e["prompt"]["copy_path"]
    assert open(copy_path, encoding="utf-8").read() == "ok to send\n"
    assert out_lines[0] == "[pal-pre-send] payload: 1 file(s) + composed prompt"
