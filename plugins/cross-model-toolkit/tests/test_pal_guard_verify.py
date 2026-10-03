"""Tests for pal_guard_verify.py — deliberation↔sidecar hash verification.

Contract under test (shared with pal_guarded_server.py): the canonical hash
is ``sha256("\n".join(c.text for c in result))`` — for chat, one TextContent
whose .text is the ToolOutput JSON, so the hash is over that JSON string.
"""

import hashlib
import importlib.util
import json
import os
import sys

import pytest

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


verify = _load("pal_guard_verify", os.path.join(SCRIPTS, "pal_guard_verify.py"))


def _write_response(tmp_path, payload_obj):
    """payload_obj is the ToolOutput-dict; returns (file, canonical_digest).

    The saved file mimics the MCP result serialization — a dict whose
    ``content`` is a list with ONE TextContent. The canonical string is the
    ``.text`` of that element, i.e. the JSON of the payload itself.
    """
    inner = json.dumps(payload_obj)
    digest = hashlib.sha256(inner.encode("utf-8")).hexdigest()
    wrapper = {"content": [{"type": "text", "text": inner}], "isError": False}
    f = tmp_path / "response.json"
    f.write_text(json.dumps(wrapper), encoding="utf-8")
    return f, digest


def _sidecar(state_dir, run_id, digest, phase="response_ok"):
    os.makedirs(state_dir, exist_ok=True)
    rec = {"schema_version": 1, "run_id": run_id, "phase": phase,
           "response_sha256": digest, "model": "test-model"}
    with open(os.path.join(state_dir, "pal_guard_responses.jsonl"), "a",
              encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def test_ok_hash_matches_exit_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PAL_STATE_DIR", str(tmp_path / "state"))
    state = str(tmp_path / "state")
    payload = {"status": "success", "content": "the answer is 42",
               "continuation_offer": {"continuation_id": "abc123"}}
    f, digest = _write_response(tmp_path, payload)
    _sidecar(state, "run-1", digest)
    assert verify.main(["--run-id", "run-1", "--response-json", str(f)]) == 0
    out = capsys.readouterr().out
    assert "OK" in out and digest in out and "run-1" in out


def test_mismatch_exit_1(tmp_path, monkeypatch):
    monkeypatch.setenv("PAL_STATE_DIR", str(tmp_path / "state"))
    state = str(tmp_path / "state")
    f, _digest = _write_response(tmp_path, {"content": "real answer"})
    _sidecar(state, "run-2", "0" * 64)
    assert verify.main(["--run-id", "run-2", "--response-json", str(f)]) == 1


def test_run_id_absent_exit_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PAL_STATE_DIR", str(tmp_path / "state"))
    f, _digest = _write_response(tmp_path, {"content": "x"})
    assert verify.main(["--run-id", "nope", "--response-json", str(f)]) == 1
    assert "no sidecar record" in capsys.readouterr().out


def test_raw_non_json_file(tmp_path, monkeypatch):
    """A caller that saved the bare response text (not JSON) still verifies."""
    monkeypatch.setenv("PAL_STATE_DIR", str(tmp_path / "state"))
    state = str(tmp_path / "state")
    raw = "plain model answer, not JSON"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    f = tmp_path / "raw.txt"
    f.write_text(raw, encoding="utf-8")
    _sidecar(state, "run-3", digest)
    assert verify.main(["--run-id", "run-3", "--response-json", str(f)]) == 0


def test_content_list_joins_texts(tmp_path, monkeypatch):
    """ToolOutput JSON with content as a list of {text}: join with "\n" —
    for the single-element chat case this equals the element itself."""
    monkeypatch.setenv("PAL_STATE_DIR", str(tmp_path / "state"))
    state = str(tmp_path / "state")
    inner = json.dumps({"status": "success", "content": "answer"})
    dumped = json.dumps({"status": "success",
                         "content": [{"type": "text", "text": inner}]})
    digest = hashlib.sha256(inner.encode("utf-8")).hexdigest()
    f = tmp_path / "tooloutput.json"
    f.write_text(dumped, encoding="utf-8")
    _sidecar(state, "run-4", digest)
    assert verify.main(["--run-id", "run-4", "--response-json", str(f)]) == 0


def test_non_response_ok_phase_fails(tmp_path, monkeypatch):
    """A guard_reject/pre_send_failed record carries no certified response."""
    monkeypatch.setenv("PAL_STATE_DIR", str(tmp_path / "state"))
    state = str(tmp_path / "state")
    f, digest = _write_response(tmp_path, {"content": "x"})
    _sidecar(state, "run-5", digest, phase="guard_reject")
    assert verify.main(["--run-id", "run-5", "--response-json", str(f)]) == 1


def test_extract_response_text_rules():
    assert verify.extract_response_text("not json at all") == "not json at all"
    # bare ToolOutput dump (content is a plain string): the WHOLE file is the
    # canonical artifact — do not descend into the content field
    bare = '{"status": "success", "content": "the answer"}'
    assert verify.extract_response_text(bare) == bare
    # MCP-result wrapper: join the content[].text list
    joined = verify.extract_response_text(
        '{"content": [{"text": "a"}, {"text": "b"}]}')
    assert joined == "a\nb"
    # serialized TextContent dict (rule 2, Fix 7): the text field itself
    assert verify.extract_response_text(
        '{"type": "text", "text": "the payload"}') == "the payload"
    # JSON list / any other dict: verbatim
    assert verify.extract_response_text('["x"]') == '["x"]'
    assert verify.extract_response_text('{"other": 1}') == '{"other": 1}'


def test_textcontent_dict_form_verifies(tmp_path, monkeypatch):
    """A response saved as the serialized TextContent dict ({"type":"text",
    "text": <ToolOutput JSON>}) hashes the inner text — Fix 7: previously
    this form hashed the whole file and produced a false mismatch."""
    monkeypatch.setenv("PAL_STATE_DIR", str(tmp_path / "state"))
    state = str(tmp_path / "state")
    inner = json.dumps({"status": "success", "content": "answer"})
    digest = hashlib.sha256(inner.encode("utf-8")).hexdigest()
    f = tmp_path / "textcontent.json"
    f.write_text(json.dumps({"type": "text", "text": inner}), encoding="utf-8")
    _sidecar(state, "run-7", digest)
    assert verify.main(["--run-id", "run-7", "--response-json", str(f)]) == 0


def test_bare_tooloutput_dump_verifies(tmp_path, monkeypatch):
    """The natural artifact (result[0].text saved verbatim — the ToolOutput
    JSON, whose content is a string) hashes the whole file: canonical."""
    monkeypatch.setenv("PAL_STATE_DIR", str(tmp_path / "state"))
    state = str(tmp_path / "state")
    inner = json.dumps({"status": "continuation_available",
                        "content": "some answer",
                        "continuation_offer": {"continuation_id": "t1"}})
    digest = hashlib.sha256(inner.encode("utf-8")).hexdigest()
    f = tmp_path / "bare.json"
    f.write_text(inner, encoding="utf-8")
    _sidecar(state, "run-6", digest)
    assert verify.main(["--run-id", "run-6", "--response-json", str(f)]) == 0
