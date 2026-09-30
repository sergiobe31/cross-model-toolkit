#!/usr/bin/env python3
"""Pin signing and verification for the guarded PAL server.

The guard server runs the pinned PAL fork in-process; drift of either the
model registry config or the fork's own code would silently change what the
guard audits. `pal_guard_pins.py --update` recomputes and writes
``state/guard_pins.json``; ``pal_guarded_server.verify_pins()`` recomputes at
startup and refuses to serve on any mismatch.

Pins recorded:
  registry_sha256        sha256 of the OPENROUTER_MODELS_CONFIG_PATH registry file
  locale / default_model / disabled_tools / max_conversation_turns /
  conversation_timeout_hours   the env/config values the fork reads at import
  fork_commit            commit sha of the uv git checkout (or PAL_FORK_COMMIT)
  fork_modules_sha256    digest over the fork modules the guard relies on
                         (relative paths sorted + contents), located via
                         ``server.__file__``

Usage:
  pal_guard_pins.py --update
"""

import argparse
import datetime
import hashlib
import json
import os
import sys

# Fork modules the guard's design depends on (plus the __init__ files of their
# packages, so a package-level change is also caught). Paths relative to the
# fork checkout root, located via server.__file__.
FORK_MODULES = [
    "__init__.py",
    "providers/__init__.py",
    "providers/openai_compatible.py",
    "server.py",
    "tools/__init__.py",
    "tools/chat.py",
    "tools/models.py",
    "tools/shared/__init__.py",
    "tools/shared/base_tool.py",
    "tools/simple/__init__.py",
    "tools/simple/base.py",
    "utils/__init__.py",
    "utils/conversation_memory.py",
]


def _plugin_config_default():
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "config",
        "pal_openrouter_models.json")


def registry_path():
    return os.environ.get("OPENROUTER_MODELS_CONFIG_PATH") or _plugin_config_default()


def _fork_root():
    import server  # the pinned fork checkout (stubbed in tests)
    return os.path.dirname(os.path.abspath(server.__file__))


def fork_commit():
    """Commit sha of the fork checkout, best effort.

    uv git checkouts live at .../git-v0/checkouts/<cache-key>/<commit>/; if the
    path does not match, PAL_FORK_COMMIT is used; otherwise "unknown".
    """
    parts = _fork_root().split(os.sep)
    if "checkouts" in parts:
        idx = parts.index("checkouts")
        if idx + 2 < len(parts) and parts[idx + 2]:
            return parts[idx + 2]
    return os.environ.get("PAL_FORK_COMMIT", "unknown")


def fork_modules_sha256():
    root = _fork_root()
    h = hashlib.sha256()
    for rel in sorted(FORK_MODULES):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            with open(os.path.join(root, rel), "rb") as fh:
                h.update(fh.read())
        except OSError:
            h.update(b"<missing>")
        h.update(b"\0")
    return h.hexdigest()


def _parse_disabled_tools():
    raw = (os.environ.get("DISABLED_TOOLS") or "").strip()
    if not raw:
        return []
    return sorted({t.strip().lower() for t in raw.split(",") if t.strip()})


def _env_int(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def compute_pins():
    """Recompute the current pin values (no file writes)."""
    import server  # noqa: F401  (ensures the fork is importable; path source)
    with open(registry_path(), "rb") as fh:
        registry_sha = hashlib.sha256(fh.read()).hexdigest()
    return {
        "registry_sha256": registry_sha,
        "locale": os.environ.get("LOCALE", "") or "",
        "default_model": os.environ.get("DEFAULT_MODEL", "auto") or "auto",
        "disabled_tools": _parse_disabled_tools(),
        "max_conversation_turns": _env_int("MAX_CONVERSATION_TURNS", 50),
        "conversation_timeout_hours": _env_int("CONVERSATION_TIMEOUT_HOURS", 3),
        "fork_commit": fork_commit(),
        "fork_modules_sha256": fork_modules_sha256(),
    }


def pins_file(state=None):
    if state is None:
        from pal_pre_send_check import state_dir
        state = state_dir()
    return os.path.join(state, "guard_pins.json")


def update(state=None):
    pins = compute_pins()
    pins["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    path = pins_file(state)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(pins, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return path, pins


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true",
                    help="recompute and write state/guard_pins.json")
    args = ap.parse_args(argv)
    if not args.update:
        ap.error("nothing to do: pass --update")
    path, pins = update()
    print(f"[pal-guard-pins] wrote {path}")
    print(f"[pal-guard-pins] fork_commit={pins['fork_commit']} "
          f"fork_modules_sha256={pins['fork_modules_sha256'][:16]}... "
          f"registry_sha256={pins['registry_sha256'][:16]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
