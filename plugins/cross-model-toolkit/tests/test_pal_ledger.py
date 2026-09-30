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


def test_no_ledger_bit_identical_and_no_state(tmp_path):
    state = tmp_path / "state"
    payload = make_payload(tmp_path)
    prompt = make_prompt(tmp_path)
    common = ["--payload", str(payload), "--prompt-file", str(prompt)]
    env = {"PAL_STATE_DIR": str(state)}
    r_off = run_cli(common, env_extra=env)
    r_flag = run_cli(common + ["--no-ledger"], env_extra=env)
    assert r_off.returncode == 0
    assert r_flag.returncode == 0
    assert r_off.stdout == r_flag.stdout
    assert "run-id" not in r_off.stdout
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
    assert hashlib.sha256(open(copy_path, "rb").read()).hexdigest() == e["prompt"]["sha256"]


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

    def one(i):
        args = ["--payload", str(payload), "--prompt-file", str(prompt),
                "--ledger", "--run-id", f"run-{i}"]
        return run_cli(args, env_extra={"PAL_STATE_DIR": str(state)})

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(one, range(100)))
    assert all(r.returncode == 0 for r in results)
    lines = (state / "pal_send_ledger.jsonl").read_text().splitlines()
    assert len(lines) == 100
    seen = set()
    for line in lines:
        e = json.loads(line)
        seen.add(e["run_id"])
        assert e["verdict"] == "ok"
    assert seen == {f"run-{i}" for i in range(100)}


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
