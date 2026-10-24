# Kimi Code variant — running this toolkit outside Claude Code

The plugin in `plugins/` targets Claude Code (marketplace install, keychain-backed key,
plugin-namespaced tools). The **same engine** also runs under **Kimi Code CLI** — and that is in
fact the setup the maintainer uses day-to-day. This document describes that variant faithfully,
as deployed in the `rule_extraction` project.

## What is identical

- **Engine:** the same PAL MCP server, launched via `uvx` from the same SHA-pinned fork
  (`sergiobe31/pal-mcp-server@e4ffd3609b56882510ccdca27883eea15d85b68a`) with the OpenRouter
  reasoning patch.
- **Registry:** the same generated superset `config/pal_openrouter_models.json` (27 upstream base
  models + 12 curated overlay entries, 12 flagged `supports_extended_thinking`), wired via
  `OPENROUTER_MODELS_CONFIG_PATH`. The project keeps **no copy**: both of its launchers point that
  variable at this repo's generated file by absolute path, so the registry cannot drift.
- **Env:** `DEFAULT_MODEL=auto`, no `OPENROUTER_ALLOWED_MODELS` — any OpenRouter model per call;
  `DISABLED_TOOLS=clink` (agent-CLI bridge with edit permissions, outside the guard).
- **Skills:** `debate` and `interceptor` with the same loop, evidence gate and adjudication
  protocol.

## What differs from the plugin

| Aspect | Claude Code plugin | Kimi Code variant |
|---|---|---|
| Install | `/plugin marketplace add` + `/plugin install` | Manual: wire the server in the project's `.mcp.json` |
| API key | `userConfig` (`sensitive`) → system keychain | `OPENROUTER_API_KEY` in the project's `.env` (gitignored), loaded by a bootstrapper |
| Launch | `.mcp.json` runs `uvx` directly (plain PAL) | `.mcp.json` runs `mcp/pal_guarded_server.sh` (guard in-process, STRICT); `mcp/pal_server.sh` is the unguarded fallback. Both source `.env`, export the registry path and `exec uvx ...` |
| Tool names | `mcp__plugin_cross-model-toolkit_pal__*` | `mcp__pal__*` |
| Skills | Plugin-namespaced: `/cross-model-toolkit:debate` | User-scope: `~/.kimi-code/skills/{debate,interceptor}/SKILL.md` |
| Shared references | `plugins/.../references/*.md` | `~/.kimi-code/skills/pal-references/{pal-call-conventions,adjudication-protocol}.md` |

## The bootstrapper (`mcp/pal_server.sh` in the project)

Responsibilities, in order:

1. Resolve the repo root relative to the script and `cd` into it.
2. Source `.env` (without clobbering already-exported variables) so `OPENROUTER_API_KEY` never
   appears in `.mcp.json` or any committed file.
3. Fail fast with a clear error if `OPENROUTER_API_KEY` is unset.
4. Export `OPENROUTER_MODELS_CONFIG_PATH` (this repo's generated registry, by absolute path),
   `DEFAULT_MODEL=auto`, `LOG_LEVEL=INFO`, `DISABLED_TOOLS=clink`.
5. `exec uvx --from git+https://github.com/sergiobe31/pal-mcp-server.git@<pinned SHA> pal-mcp-server`.

The project's `.mcp.json` then points the `pal` server at that script:

```json
"pal": {
  "command": "/path/to/project/mcp/pal_server.sh",
  "args": [],
  "env": {},
  "toolTimeoutMs": 1200000
}
```

(`toolTimeoutMs` is generous because reasoning calls at `thinking_mode: max` can take minutes.)

**Guarded launcher (`mcp/pal_guarded_server.sh`).** The project's `.mcp.json` now points at this
mirror of `pal_server.sh` instead. Same steps, plus `PAL_GUARD_STRICT=1` and `PAL_FORK_COMMIT`, and
step 5 runs this repo's `scripts/pal_guarded_server.py` inside the pinned fork
(`uvx --from git+…@<pinned SHA> python …/pal_guarded_server.py`), so every send is audited
in-process — see *Guard server* in `references/pal-call-conventions.md`. Pointing `.mcp.json` back
at `pal_server.sh` bypasses the guard.

## Maintenance rule

When the registry is updated here (overlay edit + `build_registry.py`), there is nothing to copy —
the project reads this repo's file directly — but **re-sign the guard pins**: the guarded server
recomputes the registry hash at startup and refuses to serve on any mismatch. The same applies to
the pinned env values (`DISABLED_TOOLS`, `DEFAULT_MODEL`, `LOCALE`, conversation limits). Run
`scripts/pal_guard_pins.py --update` in the guarded launcher's environment (same
`uvx --from <fork@SHA>` and env vars, since it imports the fork to hash its modules). When the PAL
fork SHA is bumped, bump it in this plugin's `.mcp.json` **and** in both project launchers
(including `PAL_FORK_COMMIT`), then re-sign the pins.

**User-scope skills: symlink, don't copy (since v1.4.0).** The skills/references under
`~/.kimi-code/skills/` used to be a manual port that silently diverged from the canonical
versions. Deploy them as symlinks instead so they always track this repo:

```
rm -rf ~/.kimi-code/skills/{debate,interceptor,pal-references}
ln -s /path/to/cross-model-toolkit/plugins/cross-model-toolkit/skills/debate        ~/.kimi-code/skills/debate
ln -s /path/to/cross-model-toolkit/plugins/cross-model-toolkit/skills/interceptor   ~/.kimi-code/skills/interceptor
ln -s /path/to/cross-model-toolkit/plugins/cross-model-toolkit/references           ~/.kimi-code/skills/pal-references
```

If you must keep file copies (e.g. the repo lives on another volume), re-sync from
`plugins/.../skills/` on every change and use the drift check below.

If a project keeps its own **registry copy** instead of pointing at this repo's file, detect drift
instead of waiting for it to bite:

```
python3 plugins/cross-model-toolkit/scripts/build_registry.py --diff /path/to/project/config/pal_openrouter_models.json
```

It rebuilds the registry fresh and compares only the `models` array (external copies may carry
their own `_README`), reporting models missing on either side and field-level differences. Exit 0
means in sync; exit 1 prints the drift.
