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
- It is a **script, not a mental grep** — in a headless flow nobody verifies that a mental grep
  ran. The debate and interceptor skills invoke it automatically; so should any future skill
  that hands content to PAL.

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
