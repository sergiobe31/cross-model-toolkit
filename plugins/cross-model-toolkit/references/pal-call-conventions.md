# PAL Call Conventions

The shared mechanics for calling the second model through the PAL MCP (`mcp__pal__*`). Both skills
— and any ad-hoc second-model call — follow these conventions instead of restating them. For the
verification discipline applied to the output, see `references/adjudication-protocol.md`.

## Division of labor

**You gather ground truth; the PAL model reasons over what you hand it.** The second model has no
file/web access of its own — it sees only what you put in the prompt or attach as file paths. So:

- YOU read the code/files/web, run the cheap checks, establish the facts (you have the tools).
- The second model reasons/critiques from a different training distribution.
- YOU adjudicate its output per `references/adjudication-protocol.md` — a proposal, never truth,
  until verified.

## Standard call

Default tool: **`mcp__pal__chat`**.

- **Model:** the OpenRouter model the user named for the session; default `z-ai/glm-5.2`. Any
  OpenRouter model works.
- **`thinking_mode: high`** by default — dial down for trivial tasks, up to `max` for hard ones.
- **`continuation_id`:** each call returns one; reuse it across rounds of the same thread so the
  second model keeps full context.
- **`absolute_file_paths`:** pass relevant files this way for grounding instead of pasting large
  contents into the prompt.

## Pre-send check (hard-fail, before every hand-off)

Before ANY PAL call that includes files or pasted content, export the composed prompt to a temp
file and run:

```
python3 <plugin-root>/scripts/pal_pre_send_check.py --payload <files...> --prompt-file <prompt.txt> --model <slug> [--max-tokens N] [--max-usd X]
```

- Scans the **exact bytes that would leave** (payload files + composed prompt) against a
  blacklist of filenames (`.env`, keys, credentials, `*secret*`…) and 10 secret regexes
  (OpenRouter/Anthropic/OpenAI/AWS/GitHub/GitLab/Slack tokens, JWTs, private-key blocks,
  credential assignments). Prints a token estimate and a cost estimate (input + 2x output
  allowance) from the committed price table `config/pal_price_table.json`.
- **Exit 1 = abort pre-send** — do not send, fix the payload. Exit 0 = safe to send.
- Pass `--max-tokens` / `--max-usd` to enforce a pre-committed dossier/cost ceiling (the
  headless substitute for a "may I attach this?" question).
- **Ledger mode** (`--ledger`, or env `PAL_LEDGER=1`; `--no-ledger` wins over the env): also
  enforces a mechanical check — every `--mcp-path` must canonically equal a `--payload` or a
  `--declared-extra` path — and appends one record to the send ledger (below). In ledger mode
  `--stdin` is forbidden: the prompt must go via `--prompt-file`.
- **Guardrail context:** calls inside the Guardrail workflow (project `AGENTS.md`) always run
  with `--ledger`, with `--plan-manifest <manifest.json>` (mandatory there), and with one
  `--mcp-path` per path passed as `absolute_file_paths` to the PAL call. The manifest is created
  in planning, before the first review round:
  `python3 <plugin-root>/scripts/pal_plan_manifest.py --file <plan.md> [--file ...] --out <manifest.json>`.
  Its `aggregate_sha256` is invariant to the order of `--file` arguments — keep the aggregate
  line with the plan.
- It is a **script, not a mental grep** — in a headless flow nobody verifies that a mental grep
  ran. The debate and interceptor skills invoke it automatically; so should any future skill
  that hands content to PAL.

### Send ledger

One JSONL line per run in ledger mode, appended to `<plugin-root>/state/pal_send_ledger.jsonl`
(prompt copies in `<plugin-root>/state/prompts/<run_id>_<ts>.prompt.txt`). Fields: `run_id`
(generated `YYYYMMDD-HHMMSS-xxxxxx`, or `--run-id`; printed as `[pal-pre-send] run-id: …`),
`ts` (ISO 8601 local), `model`, `verdict` (`ok` / `hard_fail` / `usage_error`), `prompt`
(`{sha256, copy_path}`, null without prompt), `payloads` (`[{path, realpath, sha256, inode,
size, mtime}]`), `mcp_paths`, `declared_extra`, `plan_manifest` (`{path, sha256}`, null without
`--plan-manifest`), `exclusions` (`{missing_from_payload, extra_in_payload}` from a canonical-path
diff against the manifest, null without it), `est_tokens`, `est_cost_usd`, `failures` (all
findings: blacklist / secret / budget / mcp-path / plan-manifest).

- The state dir is anchored to the script (`<plugin-root>/state`); env `PAL_STATE_DIR` overrides
  it (test hook only).
- The ledger rotates above 5 MB (`pal_send_ledger.1.jsonl`, previous backup
  overwritten). Rotation and append both happen inside a single critical section
  guarded by `fcntl.flock(LOCK_EX)` on a lateral `state/ledger.lock` file — two
  concurrent runs cannot race the rotation — and each record goes out as a single
  `os.write`.
- **Fail-closed:** an unwritable ledger or prompt copy, or a record over 64 KB, is itself a
  HARD-FAIL (exit 1 = no send) — a guard that cannot record its verdict blocks the hand-off.

**Residual scope (declared):** the `--mcp-path` check verifies the paths the caller *declared*;
it cannot see what the actual MCP call carries — structural binding (a wrapper that injects the
flags) is iteration 2.

*Note (2026-09-30): in deployments where the guard server is active (`.mcp.json` →
`mcp/pal_guarded_server.sh`), CLI ledger entries coexist with the guard's `guard:true` entries
— same schema plus declared extra fields. See "Guard server" below.*

## Guard server (iteration 2, 2026-09-30)

The PAL MCP server can run **guarded**: `pal_guarded_server.py` (plugin `scripts/`) imports the
SHA-pinned PAL fork in-process, swaps `TOOLS["chat"]` for `GuardedChatTool`, and audits the
**effective prompt** — the exact kwargs about to enter `provider.generate_content`, the only
funnel every branch of `SimpleTool.execute` traverses — BEFORE anything is transmitted. Deployed
in this repo via `mcp/pal_guarded_server.sh` (mirror of `pal_server.sh` + `PAL_GUARD_STRICT=1`);
`.mcp.json` points to it. `pal_server.sh` remains as the documented fallback (visible bypass).

**Ledger (guard mode).** Same schema as the CLI plus declared extra fields: `guard: true`,
`strict`, `manifest` (bool), `history_files`, `processed_files_delta`, `files_skipped_by_thread`,
`files_skipped_by_budget`, `files_lost_to_dedup_bug`, `system_prompt_sha256`. Guard semantics
(CLI stays bit-identical): `payloads` is the **per-round delta actually embedded** — per-file
sha256 parsed from the `--- BEGIN/END FILE ---` blob (bytes as embedded; declared fallback:
re-read from disk, labelled `sha256_source: disk_reread`, if the blob parse missed a file).
Files carried via conversation history are certified through `prompt.sha256` (computed on the
effective prompt) and listed in `history_files`; the mcp-path coverage set is
`processed ∪ history_files` and plan-manifest exclusions diff the manifest against that same
union — a manifest-declared file that ends up in `missing_from_payload` (e.g. lost to the
fork's dedup bug) is a **hard reject** under the guard, and the ledger entry itself records
`verdict:"hard_fail"` (the guard passes `coverage_fail_hard=True` so the block is visible in
the ledger, not only in the sidecar). `declared_extra` stays ∅.

**STRICT mode.** Default ON in code (`PAL_GUARD_STRICT=0` is the documented opt-out, labelled
`strict:false` in the ledger). A call with `absolute_file_paths` must open line 1 of the prompt
with `[guard-session: <slug>]` (the marker anywhere else is ambiguous → reject) and
`state/manifests/<slug>.json` must exist (create it in planning:
`pal_plan_manifest.py --file <plan> --slug <slug>`, atomic tmp+rename). A call without files
without slug passes, labelled `manifest:false`. Plan manifests are per-debate, not per-process
(`manifests/<slug>.json`). Since 2026-10-03 STRICT additionally imposes mechanically the
"round with files ⇒ fresh thread" convention: a call with `absolute_file_paths` AND a
`continuation_id` is rejected up front (`continuation-with-files`) — in the real MCP path the
server has already replaced `arguments["prompt"]` with the conversation history
(server.py:775), so a first-line marker is unreachable and the failure would otherwise surface
as the misleading "ambiguous marker". Under `PAL_GUARD_STRICT=0` the combination passes,
audited.

**Sidecar — `state/pal_guard_responses.jsonl`.** One record per run under the same flock as the
send ledger: `phase` = `response_ok | guard_reject | pre_send_failed | post_send_failed`,
`run_id`, `model`, `cause_type` (failure phases), `response_sha256` (response_ok),
`continuation_id` (response_ok). `post_send_failed` means the httpx request hook fired OR the
original provider was invoked (conservative fallback when the hook cannot be installed against
the real SDK client — assume exposure); anything earlier is `pre_send_failed`. Canonical hash:
`sha256("\n".join(c.text for c in result))` —
for chat, one TextContent whose `.text` is the ToolOutput JSON, so the hash is over that full
JSON string. Post-send sidecar failures are fail-open (stderr + `state/guard_errors.jsonl`; the
response is never hidden). Verify a saved response against the sidecar with
`pal_guard_verify.py --run-id <id> --response-json <file> [--state <dir>]`: exit 0 only when the
recomputed hash matches the `response_ok` record.

**Pins + heartbeat.** `pal_guard_pins.py --update` signs `state/guard_pins.json` (registry
sha256, locale/default model/disabled tools/turn limits, `fork_commit`, and
`fork_modules_sha256` — a digest over the fork modules the guard relies on, so code drift of the
checkout is also caught). Startup recomputes and **aborts on any mismatch**; `DISABLED_TOOLS`
containing `chat` refuses to start; a heartbeat (pid, pins sha, guard version) appends to
`state/guard_sessions.jsonl`. First run after deploying: `python scripts/pal_guard_pins.py
--update`.

**Mandatory convention — "round with files ⇒ fresh thread".** The pinned fork has a
continuation bug: `get_thread` returns a deserialized copy (conversation_memory.py:301), the
server adds the current turn BEFORE building history from the stale snapshot
(server.py:1088 vs 1209), and the tool's branch check (`base.py:335`, substring
`=== CONVERSATION HISTORY ===`) never matches the real marker
(`=== CONVERSATION HISTORY (CONTINUATION) ===`), so the in-tool reconstruction branch fires and
`add_turn` runs twice (base.py:353 on top of server.py:1088) — the enriched prompt is recorded
as a user turn and the history ends up nested. The integration golden
(`tests/test_pal_guard_integration.py`, opt-in `PAL_GUARD_INTEGRATION=1` under the pinned uv
env) fixed the empirical behavior: the new file's contents DO reach the model via disk re-read
in the in-tool history section, but the duplicated turn and nested history are real corruption,
and the incident that exposed the bug (2026-09-30) involved larger payloads where loss did
occur. Upstream fix is backlog (one-line: fresh `get_thread` at server.py:1209, or move
`add_turn` after `build_conversation_history`); the pin stays intact by Sergio's decision
2026-09-30. Until then: any PAL round that attaches files MUST run on a fresh thread; if the
thread must be kept, inline the files in the prompt with their sha256 (watch the 60,000-char
`MCP_PROMPT_SIZE_LIMIT`).

**Residual (declared 2026-09-30, same redaction as project AGENTS.md):** bypassing `.mcp.json`
to the original server has no mechanical defense (visibility = heartbeat + run_id in the
deliberation); `challenge`/`consensus` are outside the guard; the channel-local claim holds only
in CLI mode (the guard audits the effective prompt); server-side truncation inside the fork is
not detectable by the guard (fork-side diff only).

**Fase-4 corrections and notes (2026-10-03, from the first live run and the adversarial code
review):**

- **Prompt copies.** `state/prompts/` may hold copies of REJECTED prompts — a rejected prompt
  typically contains the very secret that tripped the scan. They are local-disk artifacts of the
  audit (never transmitted); delete them manually after review (no automatic retention policy).
- **Token/cost estimate.** In guard mode `est_tokens`/`est_cost_usd` may count the embedded
  blob twice (it is scanned standalone AND inside the effective prompt). These fields are an
  estimate for budgeting, not an audit artifact — the audit hashes are the per-file and prompt
  sha256.
- **Pre-guard history scan (residual corrected).** The secret-scan DOES cover re-injected
  conversation history — it travels inside the effective prompt that the guard audits and
  hashes. What is NOT re-done per-file is the certification of history-carried files (they
  arrive as embedded content, not as `payloads` entries; they are accounted via
  `history_files` and the prompt sha256).
- **Empty-response retry.** The fork retries on an empty assistant response (base.py:~501);
  both attempts of one guarded run are ledgered as separate entries under the SAME run_id, all
  of them audited.
- **Fresh-thread enforcement.** Under STRICT, `absolute_file_paths` + `continuation_id` is a
  dedicated reject (`continuation-with-files`) — see STRICT mode above; under `STRICT=0` it
  passes, audited.
- **`history_files` semantics.** The parse is anchored to the START of the effective prompt
  (every real continuation prompt begins with `=== CONVERSATION HISTORY (CONTINUATION) ===`,
  history first) — without the anchor, a fresh round embedding a file that mentions the
  `FILES REFERENCED` marker produced false history_files in the first live run
  (2026-10-03, run_id 20261003-172803-4jouol). The loss formula
  (`files_lost_to_dedup_bug = requested − processed − budget-skipped − history`) canonizes
  (realpath) all four sets before differencing — the fork may embed the realpath of a
  requested `/tmp/...` path (same run) — and reports canonical paths.
- **Response saving forms.** `pal_guard_verify.py` accepts three shapes for
  `--response-json`: the MCP-result wrapper `{"content":[{"text":…}]}` (joins the texts with
  `\n`; for chat this equals the ToolOutput JSON); a serialized TextContent dict
  `{"type":"text","text":…}` (hashes the `text` field); any other shape (raw text, JSON list,
  bare ToolOutput dump) verbatim.

## Graph grounding (optional, for mapped codebases)

When the question is structural and cross-module ("how does X flow into Y?", "is this design
sound?") and the repo has a [graphify](https://github.com/Graphify-Labs/graphify) graph
(`graphify-out/graph.json`), ground the call with the graph before or instead of attaching whole
files:

- Run 1–3 targeted lookups — `graphify explain "<symbol>"`, `graphify path "A" "B"`,
  `graphify query "<question>"` (narrow, or raise `--budget`) — and paste the resulting subgraphs
  into the prompt. They are compact, `file:line`-anchored, and carry EXTRACTED/INFERRED confidence
  tags, which fits the evidence gate.
- Keep `absolute_file_paths` for the 1–2 files that are truly central; the graph supplies the
  surrounding structure, the files supply the detail.
- The graph is a symbol map, not semantics: it anchors *where*, you still verify *why*. Check
  freshness (`graphify-out/GRAPH_REPORT.md` records the built-from commit) and run
  `graphify update .` after code changes.
- For single-function or few-file questions, plain Grep/Read is faster — skip the graph.

## Evidence gate — canonical phrasing

Every request for critique/review to the second model carries the evidence gate. Canonical
instruction (reuse verbatim or near-verbatim):

> For every claim, cite a concrete mechanism / file:line / source / reproducible reason. Claims
> that are bare assertion will be discounted.

The gate is what turns "another model's opinion" into checkable output: claims that fail it carry
less weight when adjudicated.

## PAL-native alternatives

`chat` is the general channel. Two PAL tools cover common one-shot patterns — consider them when a
full skill loop is overkill:

- **`mcp__pal__challenge`** — one-shot critical scrutiny: wraps your statement in anti-sycophancy
  instructions (single model, single pass, no loop). Use it for a quick red-team of a claim, a
  prompt, or a position when an iterative adversarial exchange isn't warranted.
- **`mcp__pal__consensus`** — multi-model synthesis: queries several models in parallel (one round;
  each model+stance pair unique — for/against/neutral) and synthesizes. Use it when a decision
  benefits from a *vote* across models rather than a single iterative adversary. It is NOT an
  iterative debate.

**These tools do not replace adjudication.** Their output is second-model output like any other
and goes through `references/adjudication-protocol.md` before anything is accepted.
