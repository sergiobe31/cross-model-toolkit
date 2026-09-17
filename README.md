# cross-model-toolkit

> **Renamed 2026-09-02** — this repo was `claude-glm-toolkit`. The old name misdescribed it twice:
> it is not GLM-only (any OpenRouter model works) and not Claude-only (the same engine runs under
> Kimi Code — see [docs/kimi-code-variant.md](docs/kimi-code-variant.md)). GitHub redirects the old
> URL, so existing clones and marketplace entries keep working.

A **Claude Code plugin** that gives your agent a *second model* — **any OpenRouter model** (GLM-5.2
by default) — plus two skills, so it can collaborate with a model from a **different training
distribution**. The second model catches what self-review misses; the discipline below keeps it
honest.

> **This README is documentation for humans** (and for any agent you explicitly point at it).
> Claude does **not** read it at startup. What Claude auto-loads when the plugin is enabled is the
> two **skills** (their `SKILL.md`) and the **`.mcp.json`** (which starts the PAL server). This file
> exists to record *what this is* and *how to run/verify it*.

---

## What it gives you (the tools — no more, no less)

**1. A second model via the bundled "PAL" MCP server** (any OpenRouter model; GLM-5.2 by default). Exposed as tools named
`mcp__plugin_cross-model-toolkit_pal__*`. The full menu:

| Group | Tools |
|---|---|
| Think / decide | `chat` (free-form second opinion), `consensus` (structured for/against), `challenge` (devil's advocate on one claim), `thinkdeep`, `planner` |
| Code | `codereview`, `precommit`, `secaudit`, `testgen`, `refactor`, `debug`, `tracer`, `docgen`, `analyze` |
| Util | `listmodels`, `apilookup`, `version` |

**2. Two skills** (namespaced under the plugin):

- **`/cross-model-toolkit:interceptor <idea>`** — turns a rough idea into a complete, ready-to-paste
  prompt: captures project context, diagnoses gaps, optionally has the second model draft/red-team
  the prompt, returns it for review. Advisory — it never executes the task.
- **`/cross-model-toolkit:debate <decision>`** — Claude forms a position, the second model attacks
  it under an *evidence gate*, Claude adjudicates claim-by-claim, then synthesizes a tri-valued
  verdict (`APROBADO` / `APROBADO-CON-CONDICIONES` / `RECHAZADO`), capped at 3 rounds (debate
  amplifies shared bias after round 1).

**3. A pre-send secrets guard** — `scripts/pal_pre_send_check.py` scans the exact bytes that
would leave the machine (payload files + composed prompt) against a filename blacklist and secret
regexes, hard-fails before the send, and prints token/cost estimates against a committed price
table. The `debate` and `interceptor` skills invoke it automatically; exit 1 = do not send.

---

## How the cross-model interaction actually works (the mechanism)

1. The plugin bundles an **MCP server** ("PAL", run via `uvx`) that connects to **OpenRouter**
   (GLM-5.2 by default; **any** OpenRouter model works — see *Choosing the second model*) and exposes
   it as the tools above. PAL runs as a **minimal fork** (`sergiobe31/pal-mcp-server`, pinned by SHA)
   whose only change is a one-file patch enabling OpenRouter reasoning — see *Reasoning* below and
   **[CREDITS.md](CREDITS.md)**. When the plugin is enabled, Claude Code starts this server and the
   tools become callable by the main agent.
2. **Division of labor:** the **main agent orchestrates and gathers ground truth** — it has file,
   web, and repo access. **The second model reasons/critiques over what the main agent passes it**
   (in the prompt, or as attached file paths). It has **no web/file browsing of its own**.
3. **The discipline (non-negotiable):** the second model's output is a *proposal*, never truth,
   until the main agent **verifies it claim-by-claim** against ground truth (`file:line` / source /
   trace) and tags each as **REAL / SMELL / FALSE-POSITIVE / HALLUCINATION**. This is what turns a
   second model from a liability into an asset.
4. The two **skills are pre-built choreographies** of this loop — interceptor for prompt
   drafting/red-teaming, debate for an adversarial decision check.

So "interaction between models" = the main agent calling the PAL tools, then **adjudicating** the
result. Never a blind hand-off.

### Reasoning

Flagged models run with **OpenRouter reasoning enabled** — the fork maps PAL's per-call
`thinking_mode` (minimal/low/medium/high/max) onto OpenRouter's `reasoning` effort, and prepends the
model's `<reasoning>` trace to its answer. The skills call the second model at **xhigh**
(`thinking_mode: max`) by default and dial down for trivial tasks.

It's **generalizable but opt-in per model**: only models with `supports_extended_thinking: true` in
`config/pal_openrouter_models.json` get reasoning. By default **7 models** are flagged —
`z-ai/glm-5.2`, `deepseek/deepseek-v4-pro`, `deepseek/deepseek-v4.1-flash`, `moonshotai/kimi-k3`,
`minimax/minimax-m3`, `tencent/hy3` and `openai/gpt-5.6-sol-pro` — every other model is byte-identical to upstream
PAL. To enable reasoning for another OpenRouter model, flip its flag to `true`; to disable one, flip
it to `false`.

---

## Install / start

> **Bring your own key.** This plugin ships *no* API key. Each user supplies their **own** OpenRouter
> key at install time; it's stored in *their* system keychain, never in the repo. So sharing this repo
> never exposes anyone's key. Who built what is spelled out in **[CREDITS.md](CREDITS.md)**.

**From inside Claude Code** (type these in the prompt; they start with `/`):
```
/plugin marketplace add https://github.com/sergiobe31/cross-model-toolkit
/plugin install cross-model-toolkit@sergio-tools
```
→ You'll be prompted for **your own OpenRouter API key** (get one at openrouter.ai; stored in your
system **keychain**, never in the repo). → **Restart** Claude Code (or `/reload-plugins`) so the PAL
server starts.

**If the interactive prompts don't surface** (it happened to us — the `/plugin install` /
`/plugin configure` dialogs didn't appear), use the CLI instead:
```
claude plugin install cross-model-toolkit@sergio-tools --config openrouter_api_key=YOUR_KEY
```
The key still lands in the keychain because the manifest marks it `sensitive`.

**Prerequisites:** **`uvx`** (the PAL server runs via `uvx`) and an **OpenRouter account** with a
little credit. Nothing else to set up.

> Maintainer / local dev: `marketplace add` also accepts a local path, e.g.
> `/plugin marketplace add /path/to/cross-model-toolkit`.

**Not on Claude Code?** The same engine runs under **Kimi Code CLI** (and any MCP-capable agent)
via a small bootstrapper script — same pinned fork, same registry, key from `.env`, skills at user
scope. See **[docs/kimi-code-variant.md](docs/kimi-code-variant.md)**.

## Verify it's live
```
claude mcp list | grep pal                          → plugin:cross-model-toolkit:pal ... ✔ Connected
mcp__plugin_cross-model-toolkit_pal__listmodels     → the OpenRouter catalog (gpt-5, gemini-2.5-pro, claude-*, grok-4, …)
/cross-model-toolkit:debate <a decision>            → the skill responds
```

## Use it day-to-day
- Stress-test a decision → `/cross-model-toolkit:debate <the decision>`
- Polish a rough idea into a prompt → `/cross-model-toolkit:interceptor <the idea>`
- Or just ask: *"get GLM's second opinion on X"* / *"have **gpt-5** red-team Y"* — the agent calls
  the PAL tools, with the model you name, and adjudicates the answer.

---

## Choosing the second model

This toolkit is **not locked to GLM** — PAL runs with `DEFAULT_MODEL=auto`, which means *you name the
model per call* and **any OpenRouter model works**:

- **Name it in the request:** *"get **openai/gpt-5**'s opinion on X"*, *"have
  **google/gemini-2.5-pro** red-team Y"*. The skills default to `z-ai/glm-5.2` if you don't name one.
- **Why it just works:** PAL's bundled registry already knows the common models (`openai/gpt-5`,
  `google/gemini-2.5-pro`, `anthropic/claude-*`, `x-ai/grok-4`, `deepseek/*`, …) with their real
  context windows. A model PAL doesn't know **still works** — it just uses a generic ~32K window
  (verified: `providers/openrouter.py:_lookup_capabilities`).
- **No cost guard:** there's no `OPENROUTER_ALLOWED_MODELS` allowlist, so PAL will use whatever model
  you (or a skill) name — including expensive ones. To hard-limit to one model for cost safety, add
  `"OPENROUTER_ALLOWED_MODELS": "your/model"` back to `.mcp.json`.
- **GLM at 1M + reasoning (default):** `config/pal_openrouter_models.json` is a **superset** registry
  wired by default (`OPENROUTER_MODELS_CONFIG_PATH`), so GLM-5.2 runs at its full **1M** window while
  every other model keeps its correct one. Reasoning is on too (see *Reasoning*): 7 models are flagged
  `supports_extended_thinking` (GLM-5.2, deepseek-v4-pro, deepseek-v4.1-flash, kimi-k3,
  minimax-m3, hy3, gpt-5.6-sol-pro), so all other models are byte-identical to upstream. This rides a small PAL
  fork pinned by SHA — see CREDITS.
- **`/debate` caveat:** its value comes from a *different-vendor* model. Point it at a model from the
  same vendor as your main agent and it becomes self-adversarial — the different-distribution
  benefit is largely lost.

---

## Structure
```
cross-model-toolkit/
├── README.md                                  # this file (humans)
├── CHANGELOG.md                               # version history (reconstructed from git)
├── CREDITS.md                                 # who built what — borrowed vs original
├── LICENSE                                    # MIT (covers the original parts; see CREDITS.md)
├── CLAUDE.md                                  # what Claude auto-loads here (incl. cross_model_collab.html upkeep rule)
├── cross_model_collab.html                    # visual map of the collaboration (offline; see CLAUDE.md)
├── docs/kimi-code-variant.md                  # running the same engine under Kimi Code CLI (bootstrapper + user-scope skills)
├── .claude-plugin/marketplace.json            # the marketplace catalog ("sergio-tools")
└── plugins/cross-model-toolkit/               # the self-contained plugin
    ├── .claude-plugin/plugin.json             # manifest + userConfig (openrouter_api_key, sensitive)
    ├── .mcp.json                              # PAL server: pinned reasoning fork + superset registry, ${user_config.openrouter_api_key}, DEFAULT_MODEL=auto
    ├── references/adjudication-protocol.md    # the verify-don't-trust protocol both skills share
    ├── references/pal-call-conventions.md     # shared PAL mechanics: division of labor, standard call, pre-send guard, evidence gate, native tools
    ├── skills/{interceptor,debate}/SKILL.md
    ├── scripts/build_registry.py              # registry builder: upstream base @ pinned SHA + overlay (--check for CI, --diff for external copies)
    ├── scripts/pal_pre_send_check.py          # pre-send secrets guard: filename blacklist + 10 regexes, exit 1 = abort, token/cost estimate
    ├── config/pal_registry_overlay.json       # the only hand-edited registry file (7 curated entries + flag overrides)
    ├── config/pal_price_table.json            # dated price source for the guard's cost estimate (warns if >90 days old)
    └── config/pal_openrouter_models.json      # GENERATED registry (27 base + 7 curated, incl. GLM @ 1M); wired by default — 7 models flagged for reasoning
```

## How it's built (record of decisions)
- Packaged as a **native Claude Code plugin**, not a hand-rolled repo + symlinks + install script.
  This was decided via a `/debate` session cross-checked against the official Claude Code docs: a
  plugin bundles skills + the MCP server + config as **one versioned, installable unit**, the MCP
  **auto-registers** on install, and the secret is handled natively.
- **Secret:** the OpenRouter key is a `userConfig` field with `"sensitive": true` → stored in the
  keychain, referenced in `.mcp.json` as `${user_config.openrouter_api_key}`. Never committed.
- **Model choice:** PAL runs with `DEFAULT_MODEL=auto` and **no** `OPENROUTER_ALLOWED_MODELS`, so the
  caller names the model per call and **any** OpenRouter model works. This was decided via a GLM
  consult adjudicated against PAL's own source (the engine, not memory) — see *Choosing the second
  model*.
- **Config:** `config/pal_openrouter_models.json` is a **generated superset** registry — built by
  `scripts/build_registry.py` from PAL's 27 bundled models (fetched at the SHA pinned in `.mcp.json`)
  plus the curated overlay `config/pal_registry_overlay.json` (7 added entries: GLM-5.2 @ 1M,
  deepseek-v4-pro, deepseek-v4.1-flash, kimi-k3, minimax-m3, hy3, gpt-5.6-sol-pro; plus intentional
  `supports_extended_thinking:false` overrides on 11 base models upstream ships as true). Wired by
  default via `OPENROUTER_MODELS_CONFIG_PATH`, so GLM gets 1M while every other model keeps its
  correct window.
- **Reasoning:** PAL is pinned by SHA to a minimal fork (`sergiobe31/pal-mcp-server`) that maps
  `thinking_mode` onto OpenRouter's `reasoning` field. Only models flagged `supports_extended_thinking`
  reason — by default 7 (GLM-5.2, deepseek-v4-pro, deepseek-v4.1-flash, kimi-k3, minimax-m3, hy3,
  gpt-5.6-sol-pro); everything else is byte-identical to upstream. See CREDITS / issue #462.

## Credits & license

This toolkit is built on other people's work, and it matters to be clear about which parts.
**Full, honest breakdown in [CREDITS.md](CREDITS.md).** The short version:

**Already built by others (not original here):**
- `BeehiveInnovations/pal-mcp-server` — the entire second-model **engine** (all the `pal` tools). Run
  as-is via `uvx`, not forked. *This is the biggest piece, and it isn't mine.*
- `affaan-m/ECC` (MIT) — the `/interceptor` **skeleton** + SKILL conventions.
- `linshenkx/prompt-optimizer` (copyleft) — the "modify-don't-execute" guard, *as a pattern* (no code copied).
- `Alex-R-A/llm-argumentation-protocol` (no license) — `/debate`'s loop **design**, *as inspiration* (no code copied).

**Original to this project (Sergio, with Claude):**
- The **packaging** as a native Claude Code plugin (marketplace + manifest + keychain-backed key + MCP wiring).
- The GLM-5.2 **1M-context fix** (`config/pal_openrouter_models.json`, a superset registry) — diagnosed and declared so PAL stops falling back to 32K; **wired by default**.
- The **OpenRouter reasoning fix** — a minimal SHA-pinned PAL fork that maps `thinking_mode` onto OpenRouter's `reasoning` field (flagged models reason at xhigh by default); see CREDITS / issue #462.
- The **claim-by-claim adjudication discipline** (REAL / SMELL / FALSE-POSITIVE / HALLUCINATION) that makes the second model safe to rely on.
- The two **skills as written** and all the **docs** (README, CLAUDE.md, the `cross_model_collab.html` brief).

Licensed **MIT** — see [LICENSE](LICENSE).

## Possible extensions (not built)
- **Project-map bootstrapper** — a skill that scans the repo and writes `.claude/interceptor.md` (the
  component map / invariants that `/interceptor` Phase 0 already reads). Deferred on purpose: it mainly
  pays off on large or team repos; for small projects `/interceptor`'s inline Phase-0 scan is enough.
  Modeled on ECC's `brand-voice` producer→schema→consumer pattern if anyone wants to build it.

## Notes / recovery
- Editing a `SKILL.md` takes effect immediately; changes to `.mcp.json` / `plugin.json` need
  `/reload-plugins` or a restart.
- Registry maintenance: `config/pal_openrouter_models.json` is **generated** — never edit it by hand.
  Edit `config/pal_registry_overlay.json` (the only hand-maintained file) and run
  `python3 plugins/cross-model-toolkit/scripts/build_registry.py`; `--check` exits 1 if the committed
  file differs from a fresh build (for CI). Re-run it after any PAL SHA bump in `.mcp.json`.
- Cost: GLM via OpenRouter ≈ $0.95/M input, $3/M output as of 2026 (other models vary — check
  openrouter.ai for current pricing; with no allowlist there's no cost ceiling). Reach for the second
  model for heavy reading, a second training distribution, or red-teaming, not for trivial things you
  can do inline.
- Reasoning calls at `thinking_mode: max` can take minutes — give the PAL server a generous tool
  timeout in your client (the Kimi Code variant uses `toolTimeoutMs: 1200000`; Claude Code's default
  MCP timeout may need raising too if you see timeouts on heavy calls).
- Moonshot rate-limits the shared OpenRouter tier for `kimi-k3` (transient 429s,
  `retry_after` ~20-26s). For heavy/debate use, bring your own Moonshot key at
  openrouter.ai/settings/integrations (BYOK) and the 429s disappear.
- This plugin **replaced an earlier scattered setup** (loose skills + a user-scope PAL server), now
  consolidated into one versioned, installable unit.
