#!/usr/bin/env python3
"""Guarded PAL MCP server — audits the effective prompt before any send.

The guard imports the pinned PAL fork in-process and swaps ``TOOLS["chat"]``
for ``GuardedChatTool``. Architecture (plan v2, deliberation 2026-09-30):

- Audit point: the provider funnel ``OpenAICompatibleProvider.generate_content``
  — the ONLY path the final effective prompt traverses in all three branches
  of SimpleTool.execute (new conversation with follow-ups, pre-embedded
  continuation, in-tool reconstruction). ``prepare_prompt`` is NOT the funnel.
- The pre-send check runs the SAME core as the CLI (``run_check`` from
  pal_pre_send_check) on bytes already in memory: the captured context-files
  blob (per-file sha parsed from the BEGIN/END FILE blocks — no re-read), the
  original user input, and the exact ``prompt``/``system_prompt`` kwargs about
  to be sent. A non-zero verdict raises ``GuardReject`` (a BaseException, so
  the fork's generic ``except Exception`` catch-all cannot swallow it) BEFORE
  the original provider call: nothing leaves the machine.
- Fail-closed ledger: a ledger write failure (including a >64KB guard entry)
  is itself a reject.
- Guard mode semantics (documented, CLI stays bit-identical): ``payloads`` is
  the per-round DELTA actually embedded; files carried via conversation
  history are certified through the prompt sha and listed in
  ``history_files``; the mcp-path coverage set is
  ``processed ∪ history_files`` (via ``coverage_extra``); plan-manifest
  exclusions diff the manifest against that same union. A manifest-declared
  file that ends up in ``missing_from_payload`` (e.g. lost to the fork's
  dedup bug) is a hard reject — the claim "declared in the plan bundle" was
  not honored.
- STRICT mode (default ON in code; ``PAL_GUARD_STRICT=0`` is the documented
  opt-out): a call with ``absolute_file_paths`` must carry a
  ``[guard-session: <slug>]`` marker alone on the FIRST line of the prompt
  and the manifest ``state/manifests/<slug>.json`` must exist. The marker
  anywhere else is ambiguous → reject.
- Sidecar ``state/pal_guard_responses.jsonl`` records response_ok /
  guard_reject / pre_send_failed / post_send_failed per run under the same
  flock as the send ledger. Post-send sidecar failures are fail-open: stderr
  plus ``state/guard_errors.jsonl``; the response is never hidden.
- A request-scoped ContextVar (``guard_ctx``) carries the capture; the fork
  keeps per-request state on ``self`` (inherited bug) — the guard does not.
- Pins (pal_guard_pins.py): the registry file and the fork modules the guard
  relies on are sha-pinned; startup recomputes and aborts on drift. A
  heartbeat is appended to ``state/guard_sessions.jsonl``.

Residual (declared in the deliberation): bypassing via the original
``pal_server.sh`` has no mechanical defense (visibility = heartbeat + run_id);
challenge/consensus tools are outside the guard this iteration; server-side
truncation inside the fork is only detectable fork-side.
"""

import contextvars
import datetime
import fcntl
import hashlib
import json
import logging
import os
import re
import sys

from pal_pre_send_check import (
    LedgerWriteError,
    UsageInputError,
    canon,
    gen_run_id,
    parse_file_blocks,
    run_check,
    sha256_file,
    state_dir,
)

import pal_guard_pins

# Pinned fork imports (stubbed via sys.modules in tests). The whole point of
# the pins is that these come from the checkout pinned in .mcp.json.
import server as pal_server
from mcp.types import TextContent
from providers.openai_compatible import OpenAICompatibleProvider
from tools.chat import ChatTool
from tools.models import ToolOutput
from tools.shared.exceptions import ToolExecutionError

GUARD_VERSION = "1.0.0"
SIDECAR_SCHEMA_VERSION = 1

logger = logging.getLogger("pal_guarded_server")

# Request-scoped guard state. The fork keeps per-request state on self
# (inherited bug, deliberation §2.5); the guard uses ContextVars so
# interleaved coroutines cannot cross streams.
guard_ctx = contextvars.ContextVar("pal_guard_ctx", default=None)
request_sent_var = contextvars.ContextVar("pal_guard_request_sent", default=False)

SLUG_FIRST_LINE_RE = re.compile(r"^\[guard-session: ([A-Za-z0-9._-]+)\]$")
GUARD_TAG = "[guard-session:"

HISTORY_SECTION_START = "=== FILES REFERENCED IN THIS CONVERSATION ==="
HISTORY_SECTION_END = "=== END REFERENCED FILES ==="
SKIPPED_BUDGET_START = "--- SKIPPED FILES (TOKEN LIMIT) ---"
SKIPPED_BUDGET_END = "--- END SKIPPED FILES ---"
SKIPPED_THREAD_NOTE = "--- NOTE: Additional files referenced in conversation history ---"
SKIPPED_THREAD_END = "--- END NOTE ---"

_BEGIN_FILE_RE = re.compile(r"^--- BEGIN FILE: (.+?) \(Last modified: .*\) ---$")
_LIST_ITEM_RE = re.compile(r"^\s+- (.+?)\s*$")


class GuardReject(BaseException):
    """Pre-send HARD-FAIL. BaseException on purpose: it must survive the
    fork's generic ``except Exception`` catch-all (SimpleTool.execute)."""

    def __init__(self, message, failures=None):
        super().__init__(message)
        self.message = message
        self.failures = list(failures or [])


class _Capture:
    """What was actually embedded THIS round, captured by the
    _prepare_file_content_for_prompt hook (fires only in branches that
    compose the prompt). Empty capture = no files embedded this round —
    valid for branches that reuse history."""

    __slots__ = ("processed", "formatted", "skipped_budget", "per_file")

    def __init__(self):
        self.processed = []
        self.formatted = None
        self.skipped_budget = []
        self.per_file = {}


class _GuardCtx:
    __slots__ = ("run_id", "arguments", "capture", "manifest_path", "strict",
                 "model", "system_prompt_sha256", "history_files", "lost_files")

    def __init__(self, run_id, arguments):
        self.run_id = run_id
        self.arguments = arguments
        self.capture = _Capture()
        self.manifest_path = None
        self.strict = True
        self.model = None
        self.system_prompt_sha256 = None
        self.history_files = []
        self.lost_files = []


def _sha256_text(text):
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_list_section(blob, start_marker, end_marker):
    """Parse ``  - path`` items between two verbatim markers in a blob."""
    if start_marker not in blob:
        return []
    section = blob.split(start_marker, 1)[1].split(end_marker, 1)[0]
    paths = []
    for line in section.splitlines():
        m = _LIST_ITEM_RE.match(line)
        if m and not m.group(1).startswith("... "):
            paths.append(m.group(1))
    return paths


def _parse_skipped_budget(blob):
    return _parse_list_section(blob, SKIPPED_BUDGET_START, SKIPPED_BUDGET_END)


def _parse_skipped_thread(blob):
    return _parse_list_section(blob, SKIPPED_THREAD_NOTE, SKIPPED_THREAD_END)


def _parse_history_files(prompt):
    """Files embedded in the conversation-history section of the EFFECTIVE
    prompt (same BEGIN FILE block format as the context-files blob)."""
    if HISTORY_SECTION_START not in prompt:
        return []
    section = prompt.split(HISTORY_SECTION_START, 1)[1].split(HISTORY_SECTION_END, 1)[0]
    paths = []
    for line in section.splitlines():
        m = _BEGIN_FILE_RE.match(line)
        if m:
            paths.append(m.group(1))
    return paths


def _build_payload_entries(ctx):
    """Per-round delta actually embedded; sha256 comes from the parsed blob
    (bytes as embedded), never a re-read. Declared fallback: if the blob
    parse missed a processed file, re-read from disk and say so."""
    entries = []
    for path in ctx.capture.processed:
        digest = ctx.capture.per_file.get(path)
        entry = {"path": path, "realpath": canon(path), "sha256": digest}
        if digest is None:
            try:
                entry["sha256"] = sha256_file(path)
                entry["sha256_source"] = "disk_reread"
            except OSError:
                entry["sha256"] = None
                entry["sha256_source"] = "unreadable"
        try:
            st = os.stat(path)
            entry.update(inode=st.st_ino, size=st.st_size, mtime=st.st_mtime)
        except OSError:
            entry.update(inode=None, size=None, mtime=None)
        entries.append(entry)
    return entries


def _build_scan_texts(ctx):
    """DELTA scan: the captured blob (this round's embedding) plus the
    original user input. Conversation history was scanned in its own round."""
    texts = []
    if ctx.capture.formatted:
        texts.append(("<context-files>", ctx.capture.formatted))
    original = ctx.arguments.get("_original_user_prompt")
    if original is None:
        original = ctx.arguments.get("prompt", "")
    texts.append(("<new-user-input>", original))
    return texts


def _compute_derived(ctx, prompt, system_prompt):
    ctx.system_prompt_sha256 = _sha256_text(system_prompt)
    ctx.history_files = _parse_history_files(prompt or "")
    request_files = list(ctx.arguments.get("absolute_file_paths") or [])
    processed = ctx.capture.processed
    skipped_budget = ctx.capture.skipped_budget
    skipped_thread = _parse_skipped_thread(ctx.capture.formatted or "")
    # F4 formula: requested but neither embedded this round, nor accounted
    # for by the budget skip, nor present in the history section — the fork
    # dedup bug (add_turn before filter + stale history) drops exactly these.
    ctx.lost_files = sorted(
        set(request_files) - set(processed) - set(skipped_budget) - set(ctx.history_files)
    )
    extra_fields = {
        "guard": True,
        "strict": ctx.strict,
        "manifest": bool(ctx.manifest_path),
        "history_files": list(ctx.history_files),
        "processed_files_delta": list(processed),
        "files_skipped_by_thread": skipped_thread,
        "files_skipped_by_budget": list(skipped_budget),
        "files_lost_to_dedup_bug": list(ctx.lost_files),
        "system_prompt_sha256": ctx.system_prompt_sha256,
    }
    return extra_fields


# --- sidecar ----------------------------------------------------------------

def append_sidecar(state, entry):
    """Append one record to state/pal_guard_responses.jsonl under the SAME
    lock as the send ledger. Fail-open post-send: a sidecar failure is logged
    to stderr + state/guard_errors.jsonl and never hides a response."""
    record = {"schema_version": SIDECAR_SCHEMA_VERSION,
              "ts": datetime.datetime.now().isoformat(timespec="seconds")}
    record.update(entry)
    try:
        os.makedirs(state, exist_ok=True)
        line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        lock_fd = os.open(os.path.join(state, "ledger.lock"),
                          os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            fd = os.open(os.path.join(state, "pal_guard_responses.jsonl"),
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        finally:
            os.close(lock_fd)
    except OSError as exc:
        print(f"[pal-guard] sidecar write failed: {exc}", file=sys.stderr)
        try:
            with open(os.path.join(state, "guard_errors.jsonl"), "a",
                      encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": record["ts"], "error": str(exc),
                                     "entry": record}) + "\n")
        except OSError:
            pass


def _sidecar_base(ctx):
    return {"run_id": ctx.run_id, "model": ctx.model}


# --- generate_content funnel guard -------------------------------------------

def install_generate_content_guard():
    """Wrap the provider funnel: the exact prompt/system_prompt kwargs are
    audited (scan + sha) and ledgered BEFORE the original call. Idempotent."""
    cls = OpenAICompatibleProvider
    if getattr(cls, "_pal_guard_wrapped", False):
        return
    original = cls.generate_content

    def guarded_generate_content(provider_self, *args, **kwargs):
        ctx = guard_ctx.get(None)
        if ctx is None:
            # Other tools (challenge, consensus, listmodels) run unaudited —
            # declared out of scope for this iteration.
            return original(provider_self, *args, **kwargs)
        prompt = kwargs.get("prompt", args[0] if args else "")
        system_prompt = kwargs.get("system_prompt")
        ctx.model = kwargs.get("model_name")
        extra_fields = _compute_derived(ctx, prompt, system_prompt)
        try:
            exit_code, _entry, failures, _out = run_check(
                ledger_mode=True,
                run_id=ctx.run_id,
                payload_entries=_build_payload_entries(ctx),
                scan_texts=_build_scan_texts(ctx),
                prompt_content=prompt,
                prompt_sha256=_sha256_text(prompt),
                model=ctx.model,
                plan_manifest=ctx.manifest_path,
                mcp_paths=sorted(set(ctx.capture.processed) | set(ctx.history_files)),
                coverage_extra=ctx.history_files,
                extra_entry_fields=extra_fields,
                state=state_dir(),
            )
        except (UsageInputError, LedgerWriteError) as exc:
            # Fail-closed includes a >64KB guard entry: nothing is sent.
            raise GuardReject(f"ledger fail-closed: {exc}") from exc
        if exit_code != 0:
            raise GuardReject("pre-send check HARD-FAIL", failures=failures)
        if ctx.manifest_path and _entry.get("exclusions"):
            # C13: the plan bundle declared files that never made it into the
            # covered set (processed ∪ history) — e.g. dropped by the fork's
            # dedup bug. Informational in CLI mode; a hard reject under the
            # guard, where the manifest is a pre-committed contract.
            missing = _entry["exclusions"].get("missing_from_payload") or []
            if missing:
                raise GuardReject(
                    "plan manifest files missing from the audited coverage set",
                    failures=[("plan-manifest", p, "missing_from_payload",
                               "declared in the plan bundle but not sent nor in history")
                              for p in missing])
        return original(provider_self, *args, **kwargs)

    cls.generate_content = guarded_generate_content
    cls._pal_guard_wrapped = True


# --- HTTP request hook --------------------------------------------------------

def _sanitize_request_dump(request):
    headers = {}
    try:
        for k, v in request.headers.items():
            if k.lower() == "authorization":
                continue
            headers[k] = v
    except Exception:
        headers = {}
    body = None
    try:
        if hasattr(request, "read"):
            content = request.read()
        else:
            content = getattr(request, "content", None)
        body = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    except Exception:
        body = None
    return {"method": getattr(request, "method", None),
            "url": str(getattr(request, "url", "")),
            "headers": headers, "body": body}


def _http_request_hook(request):
    # An httpx event hook runs at every request: the send definitely happened
    # (drives the pre_send_failed vs post_send_failed sidecar phase).
    request_sent_var.set(True)
    dump_path = os.environ.get("PAL_REQ_DUMP")
    if dump_path:
        try:
            with open(dump_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(_sanitize_request_dump(request),
                                    ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"[pal-guard] request dump failed: {exc}", file=sys.stderr)


def install_http_hook():
    """Wrap the provider's lazy ``client`` property to append a ``request``
    event hook (httpx reads event_hooks per request, so appending post-build
    works). Idempotent per provider instance."""
    cls = OpenAICompatibleProvider
    if getattr(cls, "_pal_guard_http_hook", False):
        return
    original_fget = cls.client.fget

    def guarded_client(provider_self):
        client = original_fget(provider_self)
        if client is not None and not getattr(provider_self, "_pal_guard_http_hook_installed", False):
            try:
                client.event_hooks["request"].append(_http_request_hook)
                provider_self._pal_guard_http_hook_installed = True
            except (AttributeError, KeyError, TypeError) as exc:
                logger.debug("http hook not installable on client: %s", exc)
        return client

    cls.client = property(guarded_client)
    cls._pal_guard_http_hook = True


# --- guarded chat tool --------------------------------------------------------

def build_guarded_chat_tool(ChatTool, ToolOutput, ToolExecutionError, TextContent):
    """Build GuardedChatTool against the given (real or stub) dependencies —
    the factory exists so tests can inject fakes without importing the fork."""

    class GuardedChatTool(ChatTool):
        async def execute(self, arguments):
            run_id = gen_run_id()
            ctx = _GuardCtx(run_id=run_id, arguments=dict(arguments))
            token = guard_ctx.set(ctx)
            request_sent_var.set(False)
            try:
                try:
                    self._guard_preflight(ctx)
                    result = await super().execute(arguments)
                except GuardReject as exc:
                    append_sidecar(state_dir(), {**_sidecar_base(ctx),
                                                 "phase": "guard_reject"})
                    raise self._guard_reject_error(ctx, exc) from exc
                except Exception as exc:
                    phase = ("post_send_failed" if request_sent_var.get(False)
                             else "pre_send_failed")
                    # the fork's catch-all wraps the original failure in a
                    # ToolExecutionError (`raise ... from e`) before the guard
                    # sees it — unwrap to record the true cause type
                    cause = exc.__cause__ or exc
                    append_sidecar(state_dir(), {**_sidecar_base(ctx),
                                                 "phase": phase,
                                                 "cause_type": type(cause).__name__})
                    raise
            finally:
                guard_ctx.reset(token)
            append_sidecar(state_dir(), self._response_ok_entry(ctx, result))
            return result  # INALTERADO — parity with the unguarded tool

        def _guard_preflight(self, ctx):
            ctx.strict = os.environ.get("PAL_GUARD_STRICT", "1") != "0"
            prompt = ctx.arguments.get("prompt", "") or ""
            files = list(ctx.arguments.get("absolute_file_paths") or [])
            first_line = prompt.split("\n", 1)[0]
            slug = None
            m = SLUG_FIRST_LINE_RE.match(first_line)
            if m:
                slug = m.group(1)
            elif GUARD_TAG in prompt:
                raise GuardReject(
                    "ambiguous [guard-session: ...] marker: it must appear "
                    "ALONE on the FIRST line of the prompt",
                    failures=[("slug", "prompt", "ambiguous-marker",
                               "[guard-session: ...] found outside line 1")])
            if slug:
                candidate = os.path.join(state_dir(), "manifests", slug + ".json")
                if os.path.exists(candidate):
                    ctx.manifest_path = candidate
            if ctx.strict and files and ctx.manifest_path is None:
                detail = (
                    "STRICT guard: a call with absolute_file_paths must start "
                    "line 1 of the prompt with '[guard-session: <slug>]' and "
                    "state/manifests/<slug>.json must exist (create it with "
                    "pal_plan_manifest.py --file <plan> --slug <slug>). "
                    "Without a manifest the plan-bundle coverage check cannot "
                    "run. Set PAL_GUARD_STRICT=0 only as a documented opt-out."
                )
                raise GuardReject(detail, failures=[
                    ("manifest", "prompt", "strict-manifest-required", detail)])

        def _guard_reject_error(self, ctx, exc):
            failures = [{"kind": k, "where": w, "label": l, "detail": d}
                        for k, w, l, d in exc.failures]
            remediation = (
                f"PAL guard rejected this send (run_id={ctx.run_id}): {exc.message}\n"
                "Nothing was transmitted. Fix the finding (secret, blacklist, "
                "budget, manifest/slug or coverage) and retry; every attempt "
                "is recorded in state/pal_send_ledger.jsonl and "
                "state/pal_guard_responses.jsonl."
            )
            output = ToolOutput(
                status="error",
                content=remediation,
                content_type="text",
                metadata={"guard": "pal_pre_send_check", "run_id": ctx.run_id,
                          "failures": failures},
            )
            return ToolExecutionError(output.model_dump_json())

        def _response_ok_entry(self, ctx, result):
            canonical = "\n".join(c.text for c in result)
            continuation_id = None
            try:
                data = json.loads(result[0].text)
                offer = data.get("continuation_offer") or {}
                continuation_id = offer.get("continuation_id")
            except (ValueError, IndexError, AttributeError):
                continuation_id = None
            return {**_sidecar_base(ctx), "phase": "response_ok",
                    "response_sha256": _sha256_text(canonical),
                    "continuation_id": continuation_id}

        def _prepare_file_content_for_prompt(self, *args, **kwargs):
            formatted, processed = super()._prepare_file_content_for_prompt(*args, **kwargs)
            ctx = guard_ctx.get(None)
            if ctx is not None:
                capture = _Capture()
                capture.processed = list(processed or [])
                capture.formatted = formatted or None
                capture.skipped_budget = _parse_skipped_budget(capture.formatted or "")
                capture.per_file = parse_file_blocks(capture.formatted or "")
                ctx.capture = capture
            return formatted, processed

    GuardedChatTool.__name__ = "GuardedChatTool"
    return GuardedChatTool


GuardedChatTool = build_guarded_chat_tool(ChatTool, ToolOutput,
                                          ToolExecutionError, TextContent)


# --- pins verification + heartbeat -------------------------------------------

def verify_pins(state=None):
    """Recompute pins and compare with state/guard_pins.json. Abort on drift."""
    if state is None:
        state = state_dir()
    path = os.path.join(state, "guard_pins.json")
    if not os.path.exists(path):
        print("[pal-guard] state/guard_pins.json not found — run: "
              "python scripts/pal_guard_pins.py --update", file=sys.stderr)
        sys.exit(2)
    with open(path, encoding="utf-8") as fh:
        stored = json.load(fh)
    current = pal_guard_pins.compute_pins()
    mismatches = [(k, stored.get(k, "<missing>"), current[k])
                  for k in sorted(current) if stored.get(k) != current[k]]
    if mismatches:
        for key, pinned, now in mismatches:
            print(f"[pal-guard] PIN MISMATCH {key}:\n"
                  f"  pinned : {pinned}\n"
                  f"  current: {now}", file=sys.stderr)
        print("[pal-guard] fork or registry drifted from the pins — refusing "
              "to serve. Re-pin only after review: pal_guard_pins.py --update",
              file=sys.stderr)
        sys.exit(1)


def write_heartbeat(state=None):
    if state is None:
        state = state_dir()
    try:
        with open(os.path.join(state, "guard_pins.json"), "rb") as fh:
            pins_sha = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        pins_sha = None
    entry = {"pid": os.getpid(),
             "ts": datetime.datetime.now().isoformat(timespec="seconds"),
             "pins_sha256": pins_sha,
             "guard_version": GUARD_VERSION}
    try:
        os.makedirs(state, exist_ok=True)
        line = (json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8")
        lock_fd = os.open(os.path.join(state, "ledger.lock"),
                          os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            fd = os.open(os.path.join(state, "guard_sessions.jsonl"),
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        finally:
            os.close(lock_fd)
    except OSError as exc:
        print(f"[pal-guard] heartbeat write failed: {exc}", file=sys.stderr)


# --- main ----------------------------------------------------------------------

async def _guarded_main():
    """Replica of the fork's server.py main() (lines ~1449-1513): same
    logging, same handshake instructions, same InitializationOptions."""
    from config import DEFAULT_MODEL, IS_AUTO_MODE, DEFAULT_THINKING_MODE_THINKDEEP
    from mcp.server.models import InitializationOptions
    from mcp.server.stdio import stdio_server
    from mcp.types import (PromptsCapability, ServerCapabilities,
                           ToolsCapability)

    logger.info("PAL Guarded MCP Server starting up...")
    if IS_AUTO_MODE:
        logger.info("Model mode: AUTO (CLI will select the best model for each task)")
    else:
        logger.info("Model mode: Fixed model '%s'", DEFAULT_MODEL)
    logger.info("Default thinking mode (ThinkDeep): %s", DEFAULT_THINKING_MODE_THINKDEEP)
    logger.info("Available tools: %s", list(pal_server.TOOLS.keys()))
    logger.info("Server ready - waiting for tool requests...")

    if IS_AUTO_MODE:
        handshake_instructions = (
            "When the user names a specific model (e.g. 'use chat with gpt5'), send that exact model in the tool call. "
            "When no model is mentioned, first use the `listmodels` tool from PAL to obtain available models to choose the best one from."
        )
    else:
        handshake_instructions = (
            "When the user names a specific model (e.g. 'use chat with gpt5'), send that exact model in the tool call. "
            f"When no model is mentioned, default to '{DEFAULT_MODEL}'."
        )

    async with stdio_server() as (read_stream, write_stream):
        await pal_server.server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="PAL",
                server_version=pal_server.__version__,
                instructions=handshake_instructions,
                capabilities=ServerCapabilities(
                    tools=ToolsCapability(),
                    prompts=PromptsCapability(),
                ),
            ),
        )


def main():
    disabled = {t.strip().lower()
                for t in (os.environ.get("DISABLED_TOOLS") or "").split(",")
                if t.strip()}
    if "chat" in disabled:
        print("[pal-guard] DISABLED_TOOLS contains 'chat' — a disabled tool "
              "cannot be audited; refusing to start.", file=sys.stderr)
        return 1
    pal_server.TOOLS["chat"] = GuardedChatTool()
    pal_server.configure_providers()
    install_generate_content_guard()
    install_http_hook()
    verify_pins()
    write_heartbeat()
    import asyncio
    asyncio.run(_guarded_main())
    return 0


if __name__ == "__main__":
    sys.exit(main())
