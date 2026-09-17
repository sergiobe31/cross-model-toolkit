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
  models + 6 curated overlay entries, 6 flagged `supports_extended_thinking`), wired via
  `OPENROUTER_MODELS_CONFIG_PATH`. The project copy is byte-identical in its `models` array to the
  one generated here by `scripts/build_registry.py`.
- **Env:** `DEFAULT_MODEL=auto`, no `OPENROUTER_ALLOWED_MODELS` — any OpenRouter model per call.
- **Skills:** `debate` and `interceptor` with the same loop, evidence gate and adjudication
  protocol.

## What differs from the plugin

| Aspect | Claude Code plugin | Kimi Code variant |
|---|---|---|
| Install | `/plugin marketplace add` + `/plugin install` | Manual: wire the server in the project's `.mcp.json` |
| API key | `userConfig` (`sensitive`) → system keychain | `OPENROUTER_API_KEY` in the project's `.env` (gitignored), loaded by a bootstrapper |
| Launch | `.mcp.json` runs `uvx` directly | `.mcp.json` runs `mcp/pal_server.sh`, which sources `.env`, exports the registry path and `exec uvx ...` |
| Tool names | `mcp__plugin_cross-model-toolkit_pal__*` | `mcp__pal__*` |
| Skills | Plugin-namespaced: `/cross-model-toolkit:debate` | User-scope: `~/.kimi-code/skills/{debate,interceptor}/SKILL.md` |
| Shared references | `plugins/.../references/*.md` | `~/.kimi-code/skills/pal-references/{pal-call-conventions,adjudication-protocol}.md` |

## The bootstrapper (`mcp/pal_server.sh` in the project)

Responsibilities, in order:

1. Resolve the repo root relative to the script and `cd` into it.
2. Source `.env` (without clobbering already-exported variables) so `OPENROUTER_API_KEY` never
   appears in `.mcp.json` or any committed file.
3. Fail fast with a clear error if `OPENROUTER_API_KEY` is unset.
4. Export `OPENROUTER_MODELS_CONFIG_PATH` (project's registry copy), `DEFAULT_MODEL=auto`,
   `LOG_LEVEL=INFO`.
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

## Maintenance rule

When the registry is updated here (overlay edit + `build_registry.py`), copy the regenerated
`pal_openrouter_models.json` to the project's `config/`. When the PAL fork SHA is bumped, bump it
in **both** this plugin's `.mcp.json` and the project's `mcp/pal_server.sh`.

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

To detect drift instead of waiting for it to bite, run:

```
python3 plugins/cross-model-toolkit/scripts/build_registry.py --diff /path/to/project/config/pal_openrouter_models.json
```

It rebuilds the registry fresh and compares only the `models` array (external copies may carry
their own `_README`), reporting models missing on either side and field-level differences. Exit 0
means in sync; exit 1 prints the drift.
