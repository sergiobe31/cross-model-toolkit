"""Tests for the ledger mode of pal_pre_send_check.py.

All runs redirect the state directory via the PAL_STATE_DIR env hook, so
nothing is written to the repo's real state/ dir. Subprocesses are used
throughout to exercise the real CLI surface.
"""

import hashlib
import json
import os
import subprocess
import sys

import pytest

SCRIPT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "scripts", "pal_pre_send_check.py"))


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


def read_ledger(state_dir):
    ledger = state_dir / "pal_send_ledger.jsonl"
    assert ledger.exists()
    return [json.loads(line) for line in ledger.read_text().splitlines() if line]


def base_args(tmp_path, state_dir, payload, prompt):
    return ["--payload", str(payload), "--prompt-file", str(prompt),
            "--ledger", "--run-id", "test-run"]


def test_ledger_ok_golden_sha256(tmp_path):
    state = tmp_path / "state"
    payload = make_payload(tmp_path, content="payload bytes for hashing\n")
    prompt = make_prompt(tmp_path, content="prompt bytes for hashing\n")
    r = run_cli(base_args(tmp_path, state, payload, prompt),
                env_extra={"PAL_STATE_DIR": str(state)})
    assert r.returncode == 0, r.stderr
    entries = read_ledger(state)
    assert len(entries) == 1
    e = entries[0]
    assert e["run_id"] == "test-run"
    assert e["verdict"] == "ok"
    assert e["prompt"]["sha256"] == hashlib.sha256(
        prompt.read_bytes()).hexdigest()
    assert len(e["payloads"]) == 1
    assert e["payloads"][0]["sha256"] == hashlib.sha256(
        payload.read_bytes()).hexdigest()
    assert e["payloads"][0]["size"] == payload.stat().st_size
    assert e["payloads"][0]["inode"] == payload.stat().st_ino


def test_no_ledger_bit_identical_against_pre_ledger_golden(tmp_path):
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    old = tmp_path / "pal_pre_send_check_pre_ledger.py"
    g = subprocess.run(
        ["git", "show",
         "4de687a:plugins/cross-model-toolkit/scripts/pal_pre_send_check.py"],
        capture_output=True, text=True, cwd=repo)
    if g.returncode != 0:
        pytest.skip("git show of pre-ledger script unavailable")
    old.write_text(g.stdout)
    state = tmp_path / "state"
    payload = make_payload(tmp_path)
    prompt = make_prompt(tmp_path)
    args = ["--payload", str(payload), "--prompt-file", str(prompt),
            "--max-tokens", "5"]  # max-tokens trips the failure path too
    env = dict(os.environ)
    env.pop("PAL_LEDGER", None)
    env["PAL_STATE_DIR"] = str(state)
    r_old = subprocess.run([sys.executable, str(old)] + args,
                           capture_output=True, text=True, env=env)
    r_new = run_cli(args, env_extra={"PAL_STATE_DIR": str(state)})
    assert r_old.returncode == r_new.returncode
    assert r_old.stdout == r_new.stdout
    assert "run-id" not in r_new.stdout
    assert not (state / "pal_send_ledger.jsonl").exists()
    assert not (state / "prompts").exists()


def test_stdin_with_ledger_exit_2_no_entry(tmp_path):
    state = tmp_path / "state"
    payload = make_payload(tmp_path)
    r = run_cli(["--payload", str(payload), "--stdin", "--ledger"],
                env_extra={"PAL_STATE_DIR": str(state)},
                input_text="prompt from stdin\n")
    assert r.returncode == 2
    assert not (state / "pal_send_ledger.jsonl").exists()


def test_mcp_path_outside_payload_hard_fail(tmp_path):
    state = tmp_path / "state"
    payload = make_payload(tmp_path)
    prompt = make_prompt(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not declared\n")
    args = base_args(tmp_path, state, payload, prompt) + ["--mcp-path", str(outside)]
    r = run_cli(args, env_extra={"PAL_STATE_DIR": str(state)})
    assert r.returncode == 1
    entries = read_ledger(state)
    assert len(entries) == 1
    assert entries[0]["verdict"] == "hard_fail"
    assert any(f["kind"] == "mcp-path" for f in entries[0]["failures"])


def test_declared_extra_covers_mcp_path(tmp_path):
    state = tmp_path / "state"
    payload = make_payload(tmp_path)
    prompt = make_prompt(tmp_path)
    extra = tmp_path / "extra.txt"
    extra.write_text("declared extra\n")
    args = base_args(tmp_path, state, payload, prompt) + [
        "--mcp-path", str(extra), "--declared-extra", str(extra)]
    r = run_cli(args, env_extra={"PAL_STATE_DIR": str(state)})
    assert r.returncode == 0, r.stderr
    entries = read_ledger(state)
    assert entries[0]["verdict"] == "ok"
    assert entries[0]["failures"] == []


def test_plan_manifest_exclusions_diff(tmp_path):
    state = tmp_path / "state"
    payload = make_payload(tmp_path, name="a.txt")
    prompt = make_prompt(tmp_path)
    b = tmp_path / "b.txt"
    b.write_text("in manifest only\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": [
        {"path": str(payload), "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()},
        {"path": str(b), "sha256": hashlib.sha256(b.read_bytes()).hexdigest()},
    ]}))
    args = base_args(tmp_path, state, payload, prompt) + ["--plan-manifest", str(manifest)]
    r = run_cli(args, env_extra={"PAL_STATE_DIR": str(state)})
    assert r.returncode == 0, r.stderr
    entries = read_ledger(state)
    e = entries[0]
    assert e["exclusions"]["missing_from_payload"] == [str(b)]
    assert e["exclusions"]["extra_in_payload"] == []
    assert e["plan_manifest"]["sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()


def test_unwritable_state_dir_fail_closed(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    payload = make_payload(tmp_path)
    prompt = make_prompt(tmp_path)
    args = base_args(tmp_path, state, payload, prompt)
    state.chmod(0o555)
    try:
        r = run_cli(args, env_extra={"PAL_STATE_DIR": str(state)})
    finally:
        state.chmod(0o755)
    assert r.returncode == 1
    assert "HARD-FAIL" in r.stderr


def test_run_id_explicit_and_generated(tmp_path):
    state = tmp_path / "state"
    payload = make_payload(tmp_path)
    prompt = make_prompt(tmp_path)
    r = run_cli(base_args(tmp_path, state, payload, prompt),
                env_extra={"PAL_STATE_DIR": str(state)})
    assert r.returncode == 0
    assert read_ledger(state)[0]["run_id"] == "test-run"
    state2 = tmp_path / "state2"
    args = ["--payload", str(payload), "--prompt-file", str(prompt), "--ledger"]
    r = run_cli(args, env_extra={"PAL_STATE_DIR": str(state2)})
    assert r.returncode == 0
    generated = read_ledger(state2)[0]["run_id"]
    assert generated and generated != "test-run"


def test_prompt_copy_sha_matches_entry(tmp_path):
    state = tmp_path / "state"
    payload = make_payload(tmp_path)
    prompt = make_prompt(tmp_path)
    r = run_cli(base_args(tmp_path, state, payload, prompt),
                env_extra={"PAL_STATE_DIR": str(state)})
    assert r.returncode == 0
    e = read_ledger(state)[0]
    copy_path = e["prompt"]["copy_path"]
    assert os.path.exists(copy_path)
    assert e["prompt"]["sha256"] == hashlib.sha256(prompt.read_bytes()).hexdigest()
    assert open(copy_path, encoding="utf-8").read() == prompt.read_text()


def test_mcp_path_relative_normalization(tmp_path):
    state = tmp_path / "state"
    payload = make_payload(tmp_path, name="shared.txt")
    prompt = make_prompt(tmp_path)
    args = ["--payload", str(payload), "--prompt-file", str(prompt),
            "--ledger", "--run-id", "norm",
            "--mcp-path", "./shared.txt"]
    r = run_cli(args, env_extra={"PAL_STATE_DIR": str(state)}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert read_ledger(state)[0]["verdict"] == "ok"


def test_concurrent_writes(tmp_path):
    import concurrent.futures
    state = tmp_path / "state"
    payload = make_payload(tmp_path)
    prompt = make_prompt(tmp_path)

    def one(prefix):
        def run(i):
            args = ["--payload", str(payload), "--prompt-file", str(prompt),
                    "--ledger", "--run-id", f"{prefix}-{i}"]
            return run_cli(args, env_extra={"PAL_STATE_DIR": str(state)})
        return run

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(one("a"), i) for i in range(100)]
        futs += [ex.submit(one("b"), i) for i in range(100)]
        results = [f.result() for f in futs]
    assert all(r.returncode == 0 for r in results)
    lines = (state / "pal_send_ledger.jsonl").read_text().splitlines()
    assert len(lines) == 200
    seen = set()
    for line in lines:
        e = json.loads(line)
        seen.add(e["run_id"])
        assert e["verdict"] == "ok"
    assert seen == {f"{p}-{i}" for p in "ab" for i in range(100)}


def test_env_pal_ledger_enables_and_no_ledger_wins(tmp_path):
    state = tmp_path / "state"
    payload = make_payload(tmp_path)
    prompt = make_prompt(tmp_path)
    common = ["--payload", str(payload), "--prompt-file", str(prompt)]
    env = {"PAL_STATE_DIR": str(state), "PAL_LEDGER": "1"}
    r = run_cli(common, env_extra=env)
    assert r.returncode == 0
    assert "run-id:" in r.stdout
    assert len(read_ledger(state)) == 1
    state2 = tmp_path / "state2"
    env["PAL_STATE_DIR"] = str(state2)
    r = run_cli(common + ["--no-ledger"], env_extra=env)
    assert r.returncode == 0
    assert "run-id:" not in r.stdout
    assert not (state2 / "pal_send_ledger.jsonl").exists()


def test_missing_payload_usage_error_entry(tmp_path):
    state = tmp_path / "state"
    prompt = make_prompt(tmp_path)
    r = run_cli(["--payload", str(tmp_path / "nope.txt"),
                 "--prompt-file", str(prompt), "--ledger", "--run-id", "uerr"],
                env_extra={"PAL_STATE_DIR": str(state)})
    assert r.returncode == 2
    entries = read_ledger(state)
    assert len(entries) == 1
    assert entries[0]["verdict"] == "usage_error"


def test_invalid_manifest_is_failure_not_crash(tmp_path):
    state = tmp_path / "state"
    payload = make_payload(tmp_path)
    prompt = make_prompt(tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{not json")
    args = base_args(tmp_path, state, payload, prompt) + ["--plan-manifest", str(manifest)]
    r = run_cli(args, env_extra={"PAL_STATE_DIR": str(state)})
    assert r.returncode == 1
    e = read_ledger(state)[0]
    assert any(f["kind"] == "plan-manifest" for f in e["failures"])
    assert e["verdict"] == "hard_fail"


MANIFEST_SCRIPT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "scripts", "pal_plan_manifest.py"))


def run_manifest(args, cwd=None):
    return subprocess.run(
        [sys.executable, MANIFEST_SCRIPT] + args,
        capture_output=True, text=True, cwd=cwd)


def aggregate_contract(*paths):
    pairs = sorted(
        (os.path.realpath(os.path.abspath(p)), hashlib.sha256(open(p, "rb").read()).hexdigest())
        for p in paths)
    return hashlib.sha256("".join(d for _, d in pairs).encode("ascii")).hexdigest()


def test_manifest_golden_aggregate(tmp_path):
    fa = tmp_path / "plan_a.md"
    fb = tmp_path / "plan_b.md"
    fa.write_text("# plan A\nstep one\n")
    fb.write_text("# plan B\nstep two\n")
    out = tmp_path / "manifest.json"
    r = run_manifest(["--file", str(fa), "--file", str(fb), "--out", str(out)])
    assert r.returncode == 0, r.stderr
    expected = aggregate_contract(fa, fb)
    assert expected == hashlib.sha256(
        (hashlib.sha256(fa.read_bytes()).hexdigest()
         + hashlib.sha256(fb.read_bytes()).hexdigest()).encode("ascii")
    ).hexdigest()
    manifest = json.loads(out.read_text())
    assert manifest["aggregate_sha256"] == expected
    assert len(manifest["files"]) == 2
    assert {f["path"] for f in manifest["files"]} == {str(fa), str(fb)}
    for f in manifest["files"]:
        src = tmp_path / os.path.basename(f["path"])
        assert f["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()


def test_manifest_order_invariant(tmp_path):
    fa = tmp_path / "a.md"
    fb = tmp_path / "b.md"
    fa.write_text("alpha\n")
    fb.write_text("bravo charlie delta\n")
    out1 = tmp_path / "m1.json"
    out2 = tmp_path / "m2.json"
    r1 = run_manifest(["--file", str(fa), "--file", str(fb), "--out", str(out1)])
    r2 = run_manifest(["--file", str(fb), "--file", str(fa), "--out", str(out2)])
    assert r1.returncode == 0 and r2.returncode == 0
    m1 = json.loads(out1.read_text())
    m2 = json.loads(out2.read_text())
    m1.pop("created")
    m2.pop("created")
    assert m1 == m2


def test_manifest_missing_file_exit_2(tmp_path):
    out = tmp_path / "manifest.json"
    r = run_manifest(["--file", str(tmp_path / "nope.md"), "--out", str(out)])
    assert r.returncode == 2
    assert not out.exists()



def test_mcp_path_ignored_without_ledger_mode(tmp_path):
    state = tmp_path / "state"
    payload = make_payload(tmp_path)
    prompt = make_prompt(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not declared\n")
    r = run_cli(["--payload", str(payload), "--prompt-file", str(prompt),
                 "--mcp-path", str(outside)],
                env_extra={"PAL_STATE_DIR": str(state)})
    assert r.returncode == 0, r.stderr
    assert "HARD-FAIL" not in r.stdout
    assert not (state / "pal_send_ledger.jsonl").exists()


def test_ledger_rotation_over_5mb(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    ledger = state / "pal_send_ledger.jsonl"
    ledger.write_bytes(b"x" * (5 * 1024 * 1024 + 100))
    payload = make_payload(tmp_path)
    prompt = make_prompt(tmp_path)
    r = run_cli(base_args(tmp_path, state, payload, prompt),
                env_extra={"PAL_STATE_DIR": str(state)})
    assert r.returncode == 0, r.stderr
    backup = state / "pal_send_ledger.1.jsonl"
    assert backup.exists()
    assert backup.stat().st_size > 5 * 1024 * 1024
    lines = ledger.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["run_id"] == "test-run"


def test_directory_payload_usage_error_no_traceback(tmp_path):
    state = tmp_path / "state"
    a_dir = tmp_path / "a_directory"
    a_dir.mkdir()
    prompt = make_prompt(tmp_path)
    r = run_cli(["--payload", str(a_dir), "--prompt-file", str(prompt),
                 "--ledger", "--run-id", "dirpayload"],
                env_extra={"PAL_STATE_DIR": str(state)})
    assert r.returncode == 2
    assert "Traceback" not in r.stderr
    entries = read_ledger(state)
    assert len(entries) == 1
    assert entries[0]["verdict"] == "usage_error"


def test_nothing_to_scan_ledger_usage_error_entry(tmp_path):
    state = tmp_path / "state"
    r = run_cli(["--ledger", "--run-id", "empty"],
                env_extra={"PAL_STATE_DIR": str(state)})
    assert r.returncode == 2
    entries = read_ledger(state)
    assert len(entries) == 1
    assert entries[0]["verdict"] == "usage_error"
    assert entries[0]["run_id"] == "empty"
