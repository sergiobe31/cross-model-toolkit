"""Tests for pal_plan_manifest.py --slug (Guardrail iteración 2, Fase 3).

--slug writes the manifest ATOMICALLY (tmp + os.replace) to
state/manifests/<slug>.json — the location the guarded PAL server resolves
under STRICT mode. Slugs must match ^[A-Za-z0-9._-]+$ (path traversal
rejected).
"""

import importlib.util
import datetime
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


manifest_mod = _load("pal_plan_manifest",
                     os.path.join(SCRIPTS, "pal_plan_manifest.py"))


@pytest.fixture()
def plan_file(tmp_path):
    f = tmp_path / "plan.md"
    f.write_text("# plan\ncontenido\n", encoding="utf-8")
    return f


def _run(plan_file, out, slug, state, capsys):
    argv = ["--file", str(plan_file), "--out", str(out)]
    if slug is not None:
        argv += ["--slug", slug]
    code = manifest_mod.main(argv)
    return code, capsys.readouterr()


def test_slug_writes_atomic(plan_file, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PAL_STATE_DIR", str(tmp_path / "state"))
    out = tmp_path / "manifest.json"
    code, captured = _run(plan_file, out, "debate-1", str(tmp_path / "state"),
                          capsys)
    assert code == 0
    dest = tmp_path / "state" / "manifests" / "debate-1.json"
    assert dest.exists()
    # no .tmp residue from the atomic write
    assert not (tmp_path / "state" / "manifests" / "debate-1.json.tmp").exists()
    with open(dest, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    with open(out, encoding="utf-8") as fh:
        via_out = json.load(fh)
    assert on_disk == via_out
    assert f"slug debate-1 -> {dest}" in captured.out


def test_slug_invalid_path_traversal_rejected(plan_file, tmp_path,
                                               monkeypatch, capsys):
    monkeypatch.setenv("PAL_STATE_DIR", str(tmp_path / "state"))
    out = tmp_path / "manifest.json"
    for bad in ("../x", "..", "a/b", "a\\b", "x y"):
        code, captured = _run(plan_file, out, bad, str(tmp_path / "state"),
                              capsys)
        assert code == 2, f"slug {bad!r} must be rejected"
        assert "invalid slug" in captured.err
        assert not (tmp_path / "x.json").exists()


def test_slug_idempotent(plan_file, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PAL_STATE_DIR", str(tmp_path / "state"))
    # congela el timestamp del manifiesto: la idempotencia es byte-exacta
    # solo dentro del mismo segundo (campo "created")
    class _FrozenDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 30, 12, 0, 0)

    monkeypatch.setattr(manifest_mod.datetime, "datetime", _FrozenDateTime)
    out = tmp_path / "manifest.json"
    assert _run(plan_file, out, "s1", str(tmp_path / "state"), capsys)[0] == 0
    first = (tmp_path / "state" / "manifests" / "s1.json").read_bytes()
    assert _run(plan_file, out, "s1", str(tmp_path / "state"), capsys)[0] == 0
    second = (tmp_path / "state" / "manifests" / "s1.json").read_bytes()
    assert first == second


def test_slug_allowed_charset(plan_file, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PAL_STATE_DIR", str(tmp_path / "state"))
    out = tmp_path / "manifest.json"
    for ok in ("A.b-c_d", "x" * 64):
        code, _ = _run(plan_file, out, ok, str(tmp_path / "state"), capsys)
        assert code == 0, f"slug {ok!r} must be accepted"
        assert (tmp_path / "state" / "manifests" / (ok + ".json")).exists()


def test_no_slug_keeps_legacy_behavior(plan_file, tmp_path, monkeypatch,
                                       capsys):
    monkeypatch.setenv("PAL_STATE_DIR", str(tmp_path / "state"))
    out = tmp_path / "manifest.json"
    code, captured = _run(plan_file, out, None, str(tmp_path / "state"),
                          capsys)
    assert code == 0
    assert "slug" not in captured.out
    assert not (tmp_path / "state" / "manifests").exists()
