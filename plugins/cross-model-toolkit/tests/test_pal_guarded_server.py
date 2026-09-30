"""Tests for pal_guarded_server.py — the in-process guard (Guardrail iteración 2,
Fase 2).

The pinned PAL fork is STUBBED via sys.modules before pal_guarded_server is
imported: the stubs reproduce, in the minimal surface the guard touches, the
fork truths the plan v2 §2 was verified against:

- server.py: SimpleTool.execute branches on the prompt field; a NEW
  conversation gets prepare_prompt + follow-ups appended AFTER (branch a);
  a continuation whose prompt contains "=== CONVERSATION HISTORY ===" is used
  as-is (branch b, pre-embedded); otherwise the thread is reconstructed in-tool
  with add_turn BEFORE the file filter + a stale-snapshot history build (branch
  c, the dedup-bug shape: recorded != embedded-in-history).
- tools/shared/base_tool.py: _prepare_file_content_for_prompt filters request
  files against the thread's recorded files, embeds BEGIN/END FILE blocks,
  adds the SKIPPED FILES (TOKEN LIMIT) marker and the "already available in
  our conversation context" NOTE; returns (blob, processed).
- utils/conversation_memory.py: the history section is
  "=== FILES REFERENCED IN THIS CONVERSATION ===" with the same BEGIN FILE
  blocks; get_thread returns a copy whose file list is the stale snapshot.
- tools/simple/base.py catch-all: `except ToolExecutionError: raise` then
  `except Exception` -> generic ToolExecutionError. GuardReject is a
  BaseException, so it must survive — that is what these tests pin.
- providers/openai_compatible.py: generate_content is the single funnel for
  the effective prompt; client is a lazy property.

The empirical incident that motivates the guard produced blobs where the
budget-skip marker and the thread NOTE coexist; the stub reproduces that with
an explicit BUDGET_SKIP knob (deterministic, documented per test).

Everything runs under PAL_STATE_DIR into tmp_path; nothing touches the real
state/ dir.
"""

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import types

import pytest

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Real plugin modules (same pattern as test_pal_run_check.py)
# ---------------------------------------------------------------------------
pre_check = _load("pal_pre_send_check", os.path.join(SCRIPTS, "pal_pre_send_check.py"))
guard_pins = _load("pal_guard_pins", os.path.join(SCRIPTS, "pal_guard_pins.py"))

# ---------------------------------------------------------------------------
# Fork stubs
# ---------------------------------------------------------------------------
mcp = types.ModuleType("mcp")
mcp_types = types.ModuleType("mcp.types")


class TextContent:
    def __init__(self, type="text", text=""):
        self.type = type
        self.text = text


class ServerCapabilities:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class ToolsCapability:
    pass


class PromptsCapability:
    pass


mcp_types.TextContent = TextContent
mcp_types.ServerCapabilities = ServerCapabilities
mcp_types.ToolsCapability = ToolsCapability
mcp_types.PromptsCapability = PromptsCapability
mcp.types = mcp_types

mcp_server = types.ModuleType("mcp.server")
mcp_server_models = types.ModuleType("mcp.server.models")


class InitializationOptions:
    def __init__(self, **kw):
        self.__dict__.update(kw)


mcp_server_models.InitializationOptions = InitializationOptions
mcp_server_stdio = types.ModuleType("mcp.server.stdio")


class _StdioServer:
    async def __aenter__(self):
        return (None, None)

    async def __aexit__(self, *a):
        return False


def stdio_server():
    return _StdioServer()


mcp_server_stdio.stdio_server = stdio_server
mcp_server.models = mcp_server_models
mcp_server.stdio = mcp_server_stdio
mcp.server = mcp_server

server_stub = types.ModuleType("server")
server_stub.TOOLS = {}
server_stub.__version__ = "0.0.0-stub"
server_stub.__file__ = os.path.join(os.path.dirname(__file__), "stub_fork", "server.py")


def _configure_providers():
    server_stub.configured = True


def _follow_up_instructions(turn_count, max_turns=None):
    return "FOLLOW-UP INSTRUCTIONS STUB"


class _Server:
    async def run(self, *a, **k):
        return None


server_stub.configure_providers = _configure_providers
server_stub.get_follow_up_instructions = _follow_up_instructions
server_stub.server = _Server()

providers = types.ModuleType("providers")
openai_compatible = types.ModuleType("providers.openai_compatible")


class StubModelResponse:
    def __init__(self, content, metadata=None, usage=None):
        self.content = content
        self.metadata = metadata
        self.usage = usage


class FakeHTTPClient:
    def __init__(self):
        self.event_hooks = {"request": [], "response": []}


class OpenAICompatibleProvider:
    instances = []
    response_text = "STUB MODEL RESPONSE"
    raise_exc = None

    def __init__(self):
        self.calls = []
        self._client = None
        OpenAICompatibleProvider.instances.append(self)

    def generate_content(self, prompt, model_name=None, system_prompt=None,
                         temperature=None, **kw):
        self.calls.append({"prompt": prompt, "model_name": model_name,
                           "system_prompt": system_prompt})
        if OpenAICompatibleProvider.raise_exc is not None:
            raise OpenAICompatibleProvider.raise_exc
        return StubModelResponse(content=OpenAICompatibleProvider.response_text)

    @property
    def client(self):
        if self._client is None:
            self._client = FakeHTTPClient()
        return self._client


openai_compatible.OpenAICompatibleProvider = OpenAICompatibleProvider
openai_compatible.OpenAICompatibleProvider.__module__ = "providers.openai_compatible"
providers.openai_compatible = openai_compatible

tools = types.ModuleType("tools")
tools_models = types.ModuleType("tools.models")


class ContinuationOffer:
    def __init__(self, continuation_id, note="", remaining_turns=0):
        self.continuation_id = continuation_id
        self.note = note
        self.remaining_turns = remaining_turns


class ToolOutput:
    def __init__(self, status="success", content=None, content_type="text",
                 metadata=None, continuation_offer=None):
        self.status = status
        self.content = content
        self.content_type = content_type
        self.metadata = metadata
        self.continuation_offer = continuation_offer

    def model_dump(self):
        return {
            "status": self.status,
            "content": self.content,
            "content_type": self.content_type,
            "metadata": self.metadata,
            "continuation_offer": (
                {"continuation_id": self.continuation_offer.continuation_id,
                 "note": self.continuation_offer.note,
                 "remaining_turns": self.continuation_offer.remaining_turns}
                if self.continuation_offer else None),
        }

    def model_dump_json(self):
        return json.dumps(self.model_dump())


class ToolModelCategory:
    FAST_RESPONSE = "fast_response"


tools_models.ToolOutput = ToolOutput
tools_models.ContinuationOffer = ContinuationOffer
tools_models.ToolModelCategory = ToolModelCategory
tools.models = tools_models

tools_shared = types.ModuleType("tools.shared")
shared_exceptions = types.ModuleType("tools.shared.exceptions")


class ToolExecutionError(RuntimeError):
    def __init__(self, payload):
        super().__init__(payload)
        self.payload = payload


shared_exceptions.ToolExecutionError = ToolExecutionError
base_tool = types.ModuleType("tools.shared.base_tool")
base_tool.BUDGET_SKIP = []          # incident knob: budget-skipped this round
base_tool.DELETE_AFTER_READ = []    # test 2: file gone before the audit


class BaseTool:
    name = "chat"

    def _prepare_file_content_for_prompt(self, request_files, continuation_id,
                                         context_description="New files", **kw):
        from utils import conversation_memory as cm
        if not request_files:
            return "", []
        if continuation_id:
            recorded = set(cm.get_embedded_files(continuation_id))
            files_to_embed = [f for f in request_files if f not in recorded]
            skipped_thread = [f for f in request_files if f in recorded]
        else:
            files_to_embed = list(request_files)
            skipped_thread = []
        parts = []
        processed = []
        for path in files_to_embed:
            if path in base_tool.BUDGET_SKIP:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    content = fh.read()
            except OSError:
                content = f"<error reading {path}>"
            processed.append(path)
            if path in base_tool.DELETE_AFTER_READ:
                os.unlink(path)
            parts.append(
                f"\n--- BEGIN FILE: {path} (Last modified: 2026-01-01 00:00:00 UTC) ---\n"
                f"{content}\n"
                f"--- END FILE: {path} ---\n")
        if skipped_thread:
            note = ("\n\n--- NOTE: Additional files referenced in conversation history ---\n"
                    "The following files are already available in our conversation context:\n")
            note += "\n".join(f"  - {f}" for f in skipped_thread)
            note += "\n--- END NOTE ---"
            parts.append(note)
        # The empirical incident blob: the budget marker may list files the
        # thread filter claimed in the same blob. Knobbed for determinism.
        budget = [f for f in request_files if f in base_tool.BUDGET_SKIP]
        if budget:
            marker = "\n\n--- SKIPPED FILES (TOKEN LIMIT) ---\n"
            marker += f"Total skipped: {len(budget)}\n"
            marker += "".join(f"  - {f}\n" for f in budget[:10])
            marker += "--- END SKIPPED FILES ---\n"
            parts.append(marker)
        return "".join(parts), processed


base_tool.BaseTool = BaseTool
tools_shared.exceptions = shared_exceptions
tools_shared.base_tool = base_tool

tools_simple = types.ModuleType("tools.simple")
simple_base = types.ModuleType("tools.simple.base")

DEFAULT_PROVIDER = OpenAICompatibleProvider()


class SimpleTool(BaseTool):
    def __init__(self):
        self.provider = None

    def get_name(self):
        return "chat"

    async def prepare_prompt(self, arguments):
        import server as srv
        await asyncio.sleep(0)  # real yield point: lets coroutines interleave
        files = arguments.get("absolute_file_paths") or []
        user_content = arguments.get("prompt", "")
        if files:
            file_content, processed = self._prepare_file_content_for_prompt(
                files, arguments.get("continuation_id"), "Context files")
            if file_content:
                user_content = (f"{user_content}\n\n=== CONTEXT FILES ===\n"
                                f"{file_content}\n=== END CONTEXT ===")
        return (f"=== USER REQUEST ===\n{user_content}\n=== END REQUEST ===\n"
                f"{srv.get_follow_up_instructions(0)}")

    def _parse_response(self, raw, arguments):
        from utils import conversation_memory as cm
        continuation_id = arguments.get("continuation_id")
        offer = None
        if not continuation_id:
            tid = cm.create_thread(tool_name="chat")
            cm.add_turn(tid, "user", arguments.get("prompt", ""),
                        files=arguments.get("absolute_file_paths") or [])
            cm.add_turn(tid, "assistant", raw)
            cm.commit_files(tid)
            offer = ContinuationOffer(continuation_id=tid, note="continue",
                                      remaining_turns=49)
        else:
            offer = ContinuationOffer(continuation_id=continuation_id,
                                      note="continue", remaining_turns=10)
        return ToolOutput(status="success", content=raw, content_type="text",
                          metadata={"model_used": arguments.get("model", "stub-model")},
                          continuation_offer=offer)

    async def execute(self, arguments):
        # Branch structure replicated from tools/simple/base.py:331-588.
        try:
            self._current_arguments = arguments
            continuation_id = arguments.get("continuation_id")
            if continuation_id:
                field_value = arguments.get("prompt", "")
                if "=== CONVERSATION HISTORY ===" in field_value:
                    # branch (b): pre-embedded history, prompt used as-is
                    prompt = field_value
                else:
                    # branch (c): in-tool reconstruction (add_turn BEFORE the
                    # file filter; history built from the stale snapshot)
                    from utils import conversation_memory as cm
                    thread_context = cm.get_thread(continuation_id)
                    if thread_context:
                        user_prompt = arguments.get("prompt", "")
                        user_files = arguments.get("absolute_file_paths") or []
                        if user_prompt:
                            cm.add_turn(continuation_id, "user", user_prompt,
                                        files=user_files)
                            thread_context = cm.get_thread(continuation_id)
                        history, _ = cm.build_conversation_history(thread_context, None)
                        base_prompt = await self.prepare_prompt(arguments)
                        if history:
                            prompt = f"{history}\n\n=== NEW USER INPUT ===\n{base_prompt}"
                        else:
                            prompt = base_prompt
                    else:
                        prompt = await self.prepare_prompt(arguments)
            else:
                # branch (a): new conversation + follow-ups appended AFTER
                prompt = await self.prepare_prompt(arguments)
            provider = self.provider or DEFAULT_PROVIDER
            model_response = provider.generate_content(
                prompt=prompt,
                model_name=arguments.get("model", "stub-model"),
                system_prompt="STUB SYSTEM PROMPT")
            tool_output = self._parse_response(model_response.content, arguments)
            payload = tool_output.model_dump_json()
            if tool_output.status == "error":
                raise ToolExecutionError(payload)
            return [TextContent(type="text", text=payload)]
        except ToolExecutionError:
            raise
        except Exception as e:
            # the fork's generic catch-all (base.py:573-588), replicated
            if str(e).startswith("MCP_SIZE_CHECK:"):
                raise ToolExecutionError(str(e)[len("MCP_SIZE_CHECK:"):])
            error_output = ToolOutput(
                status="error",
                content=f"Error in {self.get_name()}: {e}",
                content_type="text")
            raise ToolExecutionError(error_output.model_dump_json()) from e


simple_base.SimpleTool = SimpleTool
tools_simple.base = simple_base

tools_chat = types.ModuleType("tools.chat")


class ChatTool(SimpleTool):
    pass


tools_chat.ChatTool = ChatTool
tools.chat = tools_chat
tools.simple = tools_simple
tools.shared = tools_shared

utils = types.ModuleType("utils")
conversation_memory = types.ModuleType("utils.conversation_memory")
conversation_memory.MAX_CONVERSATION_TURNS = 50
conversation_memory.CONVERSATION_TIMEOUT_HOURS = 3
_THREAD_SEQ = [0]


class StubThread:
    def __init__(self, thread_id, turns, snapshot_files):
        self.thread_id = thread_id
        self.turns = turns
        self.snapshot_files = snapshot_files
        self.tool_name = "chat"


def _cm_reset():
    conversation_memory.THREADS.clear()
    _THREAD_SEQ[0] = 0


conversation_memory.THREADS = {}
conversation_memory.reset = _cm_reset


def _cm_create_thread(tool_name=None, initial_request=None):
    _THREAD_SEQ[0] += 1
    tid = f"thread-{_THREAD_SEQ[0]}"
    conversation_memory.THREADS[tid] = {"turns": [], "recorded": [], "snapshot": []}
    return tid


def _cm_add_turn(tid, role, content, files=None, images=None, tool_name=None, **kw):
    t = conversation_memory.THREADS.get(tid)
    if t is None:
        return False
    t["turns"].append({"role": role, "content": content})
    for f in (files or []):
        if f not in t["recorded"]:
            t["recorded"].append(f)
    return True


def _cm_commit_files(tid):
    t = conversation_memory.THREADS[tid]
    for f in t["recorded"]:
        if f not in t["snapshot"]:
            t["snapshot"].append(f)


def _cm_get_thread(tid):
    t = conversation_memory.THREADS.get(tid)
    if t is None:
        return None
    # get_thread copia (conversation_memory.py:301): the snapshot is STALE
    # w.r.t. files recorded by the boundary add_turn of the current round —
    # this is the fork dedup bug the guard must detect (plan §2.7).
    return StubThread(tid, list(t["turns"]), list(t["snapshot"]))


def _cm_get_embedded_files(tid):
    t = conversation_memory.THREADS.get(tid)
    return list(t["recorded"]) if t else []


def _cm_build_conversation_history(thread_ctx, model_context=None):
    if not thread_ctx.turns:
        return "", 0
    parts = ["=== CONVERSATION HISTORY (CONTINUATION) ===",
             f"Thread: {thread_ctx.thread_id}",
             "Tool: chat",
             "You are continuing this conversation thread from where it left off.", ""]
    if thread_ctx.snapshot_files:
        parts += ["=== FILES REFERENCED IN THIS CONVERSATION ===",
                  "The following files have been shared and analyzed during our conversation.",
                  "Refer to these when analyzing the context and requests below:", ""]
        for f in thread_ctx.snapshot_files:
            try:
                with open(f, encoding="utf-8") as fh:
                    content = fh.read()
            except OSError:
                content = "<unreadable>"
            parts.append(f"\n--- BEGIN FILE: {f} (Last modified: 2026-01-01 00:00:00 UTC) ---\n"
                         f"{content}\n--- END FILE: {f} ---\n")
        parts.append("=== END REFERENCED FILES ===")
    parts.append("=== END CONVERSATION HISTORY ===")
    return "\n".join(parts), 0


conversation_memory.create_thread = _cm_create_thread
conversation_memory.add_turn = _cm_add_turn
conversation_memory.commit_files = _cm_commit_files
conversation_memory.get_thread = _cm_get_thread
conversation_memory.get_embedded_files = _cm_get_embedded_files
conversation_memory.build_conversation_history = _cm_build_conversation_history
utils.conversation_memory = conversation_memory

for name, mod in [
    ("mcp", mcp), ("mcp.types", mcp_types), ("mcp.server", mcp_server),
    ("mcp.server.models", mcp_server_models), ("mcp.server.stdio", mcp_server_stdio),
    ("server", server_stub),
    ("providers", providers), ("providers.openai_compatible", openai_compatible),
    ("tools", tools), ("tools.models", tools_models), ("tools.chat", tools_chat),
    ("tools.shared", tools_shared), ("tools.shared.exceptions", shared_exceptions),
    ("tools.shared.base_tool", base_tool),
    ("tools.simple", tools_simple), ("tools.simple.base", simple_base),
    ("utils", utils), ("utils.conversation_memory", conversation_memory),
]:
    sys.modules[name] = mod

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
guard = _load("pal_guarded_server", os.path.join(SCRIPTS, "pal_guarded_server.py"))
guard.install_generate_content_guard()
guard.install_http_hook()

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_stubs(tmp_path, monkeypatch):
    monkeypatch.setenv("PAL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("PAL_GUARD_STRICT", raising=False)
    monkeypatch.delenv("PAL_REQ_DUMP", raising=False)
    monkeypatch.delenv("DISABLED_TOOLS", raising=False)
    OpenAICompatibleProvider.instances.clear()
    OpenAICompatibleProvider.response_text = "STUB MODEL RESPONSE"
    OpenAICompatibleProvider.raise_exc = None
    base_tool.BUDGET_SKIP = []
    base_tool.DELETE_AFTER_READ = []
    _cm_reset()
    yield
    _cm_reset()


def make_file(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return str(p)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_jsonl(path):
    assert path.exists(), f"missing {path}"
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def ledger_entries(tmp_path):
    return read_jsonl(tmp_path / "state" / "pal_send_ledger.jsonl")


def sidecar_entries(tmp_path):
    return read_jsonl(tmp_path / "state" / "pal_guard_responses.jsonl")


def make_manifest(tmp_path, slug, files):
    mdir = tmp_path / "state" / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    entries = []
    for f in files:
        entries.append({"path": f, "sha256": hashlib.sha256(
            open(f, "rb").read()).hexdigest()} if os.path.exists(f) else
            {"path": f, "sha256": None})
    (mdir / f"{slug}.json").write_text(json.dumps({"files": entries}))
    return str(mdir / f"{slug}.json")


def new_tool(provider=None):
    tool = guard.GuardedChatTool()
    tool.provider = provider or OpenAICompatibleProvider()
    return tool


# --- 1) the wrapper audits the FINAL prompt in all three branches -----------

def test_branch_a_new_conversation_prompt_includes_followups(tmp_path):
    f1 = make_file(tmp_path, "a.py", "alpha\nbravo")
    slug = "sess-a"
    make_manifest(tmp_path, slug, [f1])
    tool = new_tool()
    provider = tool.provider
    args = {"prompt": f"[guard-session: {slug}]\nreview this",
            "absolute_file_paths": [f1], "working_directory_absolute_path": str(tmp_path)}
    result = asyncio.run(tool.execute(args))
    assert len(provider.calls) == 1
    sent = provider.calls[0]["prompt"]
    # branch (a): prepare_prompt THEN follow-ups appended after (D1)
    assert "FOLLOW-UP INSTRUCTIONS STUB" in sent
    assert "=== CONTEXT FILES ===" in sent
    (entry,) = ledger_entries(tmp_path)
    assert entry["prompt"]["sha256"] == sha(sent)  # hash covers follow-ups
    assert entry["guard"] is True
    assert [p["path"] for p in entry["payloads"]] == [f1]


def test_branch_b_preembedded_prompt_audited_as_is(tmp_path):
    f_old = make_file(tmp_path, "old.py", "old content")
    tid = _cm_create_thread(tool_name="chat")
    _cm_add_turn(tid, "user", "first question", files=[f_old])
    _cm_commit_files(tid)
    history, _ = _cm_build_conversation_history(_cm_get_thread(tid), None)
    # pre-embedded: server-side history + FILES REFERENCED + the substring
    # SimpleTool.execute looks for ("=== CONVERSATION HISTORY ===")
    embedded = (f"{history}\n\n=== CONVERSATION HISTORY ===\n"
                f"=== NEW USER INPUT ===\nfollow-up question")
    tool = new_tool()
    args = {"prompt": embedded, "continuation_id": tid,
            "working_directory_absolute_path": str(tmp_path)}
    asyncio.run(tool.execute(args))
    provider = tool.provider
    (entry,) = ledger_entries(tmp_path)
    assert entry["prompt"]["sha256"] == sha(provider.calls[0]["prompt"])
    assert provider.calls[0]["prompt"] == embedded  # used as-is
    # history files parse from the FILES REFERENCED section of the prompt
    assert entry["history_files"] == [f_old]
    assert entry["payloads"] == []  # nothing embedded this round


def test_branch_c_intool_reconstruction_audits_composed_prompt(tmp_path):
    f_old = make_file(tmp_path, "old.py", "old content")
    f_new = make_file(tmp_path, "new.py", "new content")
    tid = _cm_create_thread(tool_name="chat")
    _cm_add_turn(tid, "user", "first question", files=[f_old])
    _cm_commit_files(tid)
    slug = "sess-c"
    # manifest declares only what the history certifies; f_new is undeclared
    # (a real run would hit the missing_from_payload block — see below)
    make_manifest(tmp_path, slug, [f_old])
    tool = new_tool()
    args = {"prompt": f"[guard-session: {slug}]\nnow review the new file",
            "continuation_id": tid,
            "absolute_file_paths": [f_old, f_new],
            "working_directory_absolute_path": str(tmp_path)}
    asyncio.run(tool.execute(args))
    provider = tool.provider
    sent = provider.calls[0]["prompt"]
    assert "=== CONVERSATION HISTORY (CONTINUATION) ===" in sent
    assert "=== NEW USER INPUT ===" in sent
    (entry,) = ledger_entries(tmp_path)
    assert entry["prompt"]["sha256"] == sha(sent)
    # stale snapshot only has f_old; f_new is claimed by the thread NOTE but
    # NOT embedded (dedup bug shape) -> f_new lands in files_lost_to_dedup_bug
    assert entry["history_files"] == [f_old]
    assert entry["processed_files_delta"] == []
    assert entry["files_lost_to_dedup_bug"] == [f_new]


# --- 2) capture: per-file sha from the blob, no re-read (D3/F2) -------------

def test_per_file_sha_comes_from_blob_not_disk(tmp_path):
    f1 = make_file(tmp_path, "gone.py", "content that will vanish")
    base_tool.DELETE_AFTER_READ = [f1]
    make_manifest(tmp_path, "sess-del", [f1])
    tool = new_tool()
    args = {"prompt": "[guard-session: sess-del]\nread this",
            "absolute_file_paths": [f1],
            "working_directory_absolute_path": str(tmp_path)}
    asyncio.run(tool.execute(args))
    (entry,) = ledger_entries(tmp_path)
    blob_sha = pre_check.parse_file_blocks(
        "\n--- BEGIN FILE: %s (Last modified: m) ---\ncontent that will vanish\n--- END FILE: %s ---\n"
        % (f1, f1))[f1]
    (payload,) = entry["payloads"]
    assert payload["sha256"] == blob_sha
    assert payload["sha256"] == sha("content that will vanish")
    assert "sha256_source" not in payload  # no disk re-read fallback taken
    assert payload["inode"] is None       # stat after deletion -> None


# --- 3) reject: secret -> fail-closed; 4) GuardReject vs catch-all ----------

def test_secret_in_prompt_rejected_before_send(tmp_path):
    secret = "sk-or-v1-" + "a" * 32
    tool = new_tool()
    args = {"prompt": f"check this key\napi_key = {secret}\n",
            "working_directory_absolute_path": str(tmp_path)}
    with pytest.raises(ToolExecutionError) as excinfo:
        asyncio.run(tool.execute(args))
    provider = tool.provider
    assert provider.calls == []  # fail-closed: the provider never saw it
    payload = json.loads(excinfo.value.payload)
    # our handler produced this, NOT the stub's generic catch-all ("Error in
    # chat: ...") — GuardReject (BaseException) survived the catch-all (C5)
    assert payload["status"] == "error"
    assert payload["metadata"]["guard"] == "pal_pre_send_check"
    assert payload["metadata"]["run_id"]
    assert any(f["label"] == "openrouter-key" for f in payload["metadata"]["failures"])
    assert "PAL guard rejected" in payload["content"]
    (entry,) = ledger_entries(tmp_path)
    assert entry["verdict"] == "hard_fail"
    assert any(f["label"] == "openrouter-key" for f in entry["failures"])
    (sc,) = sidecar_entries(tmp_path)
    assert sc["phase"] == "guard_reject"
    assert sc["run_id"] == entry["run_id"]


# --- 5) ContextVar isolation + empty capture --------------------------------

def test_contextvar_no_cross_between_interleaved_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("PAL_GUARD_STRICT", "0")
    fa = make_file(tmp_path, "a.py", "file A")
    fb = make_file(tmp_path, "b.py", "file B")

    async def run(path):
        tool = new_tool()
        args = {"prompt": "review", "absolute_file_paths": [path],
                "working_directory_absolute_path": str(tmp_path)}
        await tool.execute(args)
        return tool.provider

    async def main():
        return await asyncio.gather(run(fa), run(fb))

    pa, pb = asyncio.run(main())
    assert {c["prompt"] for c in pa.calls} != {c["prompt"] for c in pb.calls}
    entries = ledger_entries(tmp_path)
    payloads = [e["payloads"][0]["path"] for e in entries]
    assert sorted(payloads) == sorted([fa, fb])


def test_empty_capture_means_no_files_this_round(tmp_path):
    tool = new_tool()
    args = {"prompt": "no files here\n",
            "working_directory_absolute_path": str(tmp_path)}
    asyncio.run(tool.execute(args))
    (entry,) = ledger_entries(tmp_path)
    assert entry["payloads"] == []
    assert entry["processed_files_delta"] == []
    assert entry["files_lost_to_dedup_bug"] == []
    assert entry["verdict"] == "ok"


# --- 6) STRICT / slug rules (F1/F7/D2) ---------------------------------------

def test_strict_files_without_slug_rejected(tmp_path):
    f1 = make_file(tmp_path, "x.py", "x")
    tool = new_tool()
    args = {"prompt": "review\n", "absolute_file_paths": [f1],
            "working_directory_absolute_path": str(tmp_path)}
    with pytest.raises(ToolExecutionError):
        asyncio.run(tool.execute(args))
    assert tool.provider.calls == []
    (sc,) = sidecar_entries(tmp_path)
    assert sc["phase"] == "guard_reject"


def test_strict_files_with_slug_but_missing_manifest_rejected(tmp_path):
    f1 = make_file(tmp_path, "x.py", "x")
    tool = new_tool()
    args = {"prompt": "[guard-session: nope]\nreview\n",
            "absolute_file_paths": [f1],
            "working_directory_absolute_path": str(tmp_path)}
    with pytest.raises(ToolExecutionError):
        asyncio.run(tool.execute(args))
    assert tool.provider.calls == []
    assert not (tmp_path / "state" / "manifests" / "nope.json").exists()


def test_no_files_no_slug_passes_with_manifest_false(tmp_path):
    tool = new_tool()
    args = {"prompt": "just chatting\n",
            "working_directory_absolute_path": str(tmp_path)}
    asyncio.run(tool.execute(args))
    (entry,) = ledger_entries(tmp_path)
    assert entry["verdict"] == "ok"
    assert entry["manifest"] is False
    assert entry["strict"] is True


def test_strict_off_allows_files_without_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("PAL_GUARD_STRICT", "0")
    f1 = make_file(tmp_path, "x.py", "x")
    tool = new_tool()
    args = {"prompt": "review\n", "absolute_file_paths": [f1],
            "working_directory_absolute_path": str(tmp_path)}
    asyncio.run(tool.execute(args))
    assert len(tool.provider.calls) == 1
    (entry,) = ledger_entries(tmp_path)
    assert entry["strict"] is False
    assert entry["manifest"] is False


def test_slug_marker_off_first_line_rejected_as_ambiguous(tmp_path):
    tool = new_tool()
    args = {"prompt": "intro line\n[guard-session: late]\nmore",
            "working_directory_absolute_path": str(tmp_path)}
    with pytest.raises(ToolExecutionError) as excinfo:
        asyncio.run(tool.execute(args))
    assert "ambiguous" in str(excinfo.value.payload)
    assert tool.provider.calls == []


# --- 7) sidecar phases + fail-open ------------------------------------------

def test_sidecar_response_ok_with_sha_and_continuation(tmp_path):
    tool = new_tool()
    args = {"prompt": "hello\n", "working_directory_absolute_path": str(tmp_path)}
    result = asyncio.run(tool.execute(args))
    assert isinstance(result, list) and result[0].text  # response INALTERADO
    (sc,) = sidecar_entries(tmp_path)
    assert sc["phase"] == "response_ok"
    assert sc["response_sha256"] == sha(result[0].text)
    data = json.loads(result[0].text)
    assert sc["continuation_id"] == data["continuation_offer"]["continuation_id"]
    assert sc["model"] == "stub-model"


def test_sidecar_pre_send_failed_when_provider_raises(tmp_path):
    OpenAICompatibleProvider.raise_exc = ValueError("boom before send")
    tool = new_tool()
    args = {"prompt": "hello\n", "working_directory_absolute_path": str(tmp_path)}
    with pytest.raises(ToolExecutionError):
        asyncio.run(tool.execute(args))
    (sc,) = sidecar_entries(tmp_path)
    assert sc["phase"] == "pre_send_failed"
    assert sc["cause_type"] == "ValueError"


def test_sidecar_post_send_failed_after_http_hook_fired(tmp_path):
    class FakeRequest:
        method = "POST"
        url = "https://api.example.com/v1/chat"
        headers = {"authorization": "Bearer sk-secret", "content-type": "application/json"}

        def read(self):
            return b'{"model": "stub-model"}'

    OpenAICompatibleProvider.raise_exc = RuntimeError("boom after send")
    tool = new_tool()
    # simulate the httpx layer firing the request hook before the provider
    # raises: the send happened, so the sidecar must say post_send_failed
    orig_generate = tool.provider.generate_content

    def generate_and_mark(*a, **k):
        guard._http_request_hook(FakeRequest())
        return orig_generate(*a, **k)

    tool.provider.generate_content = generate_and_mark
    args = {"prompt": "hello\n", "working_directory_absolute_path": str(tmp_path)}
    with pytest.raises(ToolExecutionError):
        asyncio.run(tool.execute(args))
    (sc,) = sidecar_entries(tmp_path)
    assert sc["phase"] == "post_send_failed"
    assert sc["cause_type"] == "RuntimeError"


def test_request_dump_sanitized(tmp_path, monkeypatch):
    class FakeRequest:
        method = "POST"
        url = "https://api.example.com/v1/chat"
        headers = {"authorization": "Bearer sk-secret", "content-type": "application/json"}

        def read(self):
            return b'{"model": "x"}'

    dump = tmp_path / "req_dump.jsonl"
    monkeypatch.setenv("PAL_REQ_DUMP", str(dump))
    guard._http_request_hook(FakeRequest())
    (record,) = read_jsonl(dump)
    assert record["method"] == "POST"
    assert record["url"].startswith("https://api.example.com")
    assert "authorization" not in {k.lower() for k in record["headers"]}
    assert json.loads(record["body"]) == {"model": "x"}


def test_sidecar_fail_open_keeps_response(tmp_path, capsys):
    # a directory at the sidecar path forces OSError on open: the response
    # must still reach the caller (fail-open post-send), with the failure
    # logged to stderr + state/guard_errors.jsonl
    (tmp_path / "state").mkdir(exist_ok=True)
    os.mkdir(tmp_path / "state" / "pal_guard_responses.jsonl")
    tool = new_tool()
    args = {"prompt": "hello\n", "working_directory_absolute_path": str(tmp_path)}
    result = asyncio.run(tool.execute(args))  # must NOT raise
    assert result[0].text
    assert "sidecar write failed" in capsys.readouterr().err
    errors = read_jsonl(tmp_path / "state" / "guard_errors.jsonl")
    assert errors and errors[0]["entry"]["phase"] == "response_ok"


# --- 8) dedup-bug formula (F4) + manifest block (C13) ------------------------

def test_dedup_bug_mixed_budget_and_lost(tmp_path, monkeypatch):
    monkeypatch.setenv("PAL_GUARD_STRICT", "0")  # this test targets the formula
    a_old = make_file(tmp_path, "a_old.py", "old")
    b_budget = make_file(tmp_path, "b_budget.py", "big")
    c_lost = make_file(tmp_path, "c_lost.py", "lost")
    tid = _cm_create_thread(tool_name="chat")
    _cm_add_turn(tid, "user", "round 1", files=[a_old])
    _cm_commit_files(tid)  # only a_old made it into the history snapshot
    base_tool.BUDGET_SKIP = [b_budget]
    tool = new_tool()
    args = {"prompt": "round 2", "continuation_id": tid,
            "absolute_file_paths": [a_old, b_budget, c_lost],
            "working_directory_absolute_path": str(tmp_path)}
    asyncio.run(tool.execute(args))
    (entry,) = ledger_entries(tmp_path)
    assert entry["processed_files_delta"] == []
    assert entry["files_skipped_by_thread"] == [a_old, b_budget, c_lost]
    assert entry["files_skipped_by_budget"] == [b_budget]
    assert entry["history_files"] == [a_old]
    # F4: requested but neither embedded, nor budget-skipped, nor in history
    assert entry["files_lost_to_dedup_bug"] == [c_lost]


def test_dedup_lost_file_declared_in_manifest_blocks(tmp_path):
    a_old = make_file(tmp_path, "a_old.py", "old")
    c_lost = make_file(tmp_path, "c_lost.py", "lost")
    tid = _cm_create_thread(tool_name="chat")
    _cm_add_turn(tid, "user", "round 1", files=[a_old])
    _cm_commit_files(tid)
    slug = "sess-dedup"
    # the plan bundle declares BOTH files as covered by this run
    make_manifest(tmp_path, slug, [a_old, c_lost])
    tool = new_tool()
    args = {"prompt": f"[guard-session: {slug}]\nround 2",
            "continuation_id": tid,
            "absolute_file_paths": [a_old, c_lost],
            "working_directory_absolute_path": str(tmp_path)}
    with pytest.raises(ToolExecutionError) as excinfo:
        asyncio.run(tool.execute(args))
    assert tool.provider.calls == []  # blocked BEFORE the send
    payload = json.loads(excinfo.value.payload)
    labels = [f["label"] for f in payload["metadata"]["failures"]]
    assert "missing_from_payload" in labels
    (entry,) = ledger_entries(tmp_path)
    assert entry["exclusions"]["missing_from_payload"] == [os.path.realpath(c_lost)]
    assert os.path.realpath(a_old) not in entry["exclusions"]["missing_from_payload"]


def test_manifest_covered_via_history_passes(tmp_path):
    # a pre-embedded continuation (the normal MCP shape): the round embeds
    # nothing new; the manifest is certified entirely through the FILES
    # REFERENCED section of the audited prompt (F3)
    a_old = make_file(tmp_path, "a_old.py", "old")
    tid = _cm_create_thread(tool_name="chat")
    _cm_add_turn(tid, "user", "first question", files=[a_old])
    _cm_commit_files(tid)
    history, _ = _cm_build_conversation_history(_cm_get_thread(tid), None)
    embedded = (f"{history}\n\n=== CONVERSATION HISTORY ===\n"
                f"=== NEW USER INPUT ===\nfollow-up")
    slug = "sess-ok"
    make_manifest(tmp_path, slug, [a_old])
    tool = new_tool()
    args = {"prompt": f"[guard-session: {slug}]\n{embedded}",
            "continuation_id": tid,
            "absolute_file_paths": [a_old],  # re-requested, filtered by thread
            "working_directory_absolute_path": str(tmp_path)}
    asyncio.run(tool.execute(args))
    (entry,) = ledger_entries(tmp_path)
    assert entry["verdict"] == "ok"
    assert entry["exclusions"]["missing_from_payload"] == []
    assert entry["exclusions"]["extra_in_payload"] == []
    assert len(tool.provider.calls) == 1


# --- 9) pins + heartbeat + DISABLED_TOOLS ------------------------------------

def _make_fork_tree(tmp_path):
    root = tmp_path / "fork"
    for rel in guard_pins.FORK_MODULES:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# stub fork module {rel}\n")
    return root


def test_pins_update_verify_mismatch_and_missing(tmp_path, monkeypatch, capsys):
    fork = _make_fork_tree(tmp_path)
    monkeypatch.setattr(server_stub, "__file__", str(fork / "server.py"))
    registry = tmp_path / "registry.json"
    registry.write_text('{"models": {}}')
    monkeypatch.setenv("OPENROUTER_MODELS_CONFIG_PATH", str(registry))
    state = tmp_path / "state"

    with pytest.raises(SystemExit) as e:
        guard.verify_pins(str(state))
    assert e.value.code == 2
    assert "pal_guard_pins.py --update" in capsys.readouterr().err

    guard_pins.update(str(state))
    assert guard.verify_pins(str(state)) is None

    stored = json.loads((state / "guard_pins.json").read_text())
    stored["fork_modules_sha256"] = "0" * 64
    (state / "guard_pins.json").write_text(json.dumps(stored))
    with pytest.raises(SystemExit) as e:
        guard.verify_pins(str(state))
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "PIN MISMATCH fork_modules_sha256" in err
    assert "0" * 64 in err


def test_pins_detect_registry_drift(tmp_path, monkeypatch):
    fork = _make_fork_tree(tmp_path)
    monkeypatch.setattr(server_stub, "__file__", str(fork / "server.py"))
    registry = tmp_path / "registry.json"
    registry.write_text('{"models": {}}')
    monkeypatch.setenv("OPENROUTER_MODELS_CONFIG_PATH", str(registry))
    state = tmp_path / "state"
    guard_pins.update(str(state))
    registry.write_text('{"models": {"new": {}}}')  # drift
    with pytest.raises(SystemExit) as e:
        guard.verify_pins(str(state))
    assert e.value.code == 1


def test_heartbeat_writes_entry(tmp_path):
    guard.write_heartbeat(str(tmp_path / "state"))
    (entry,) = read_jsonl(tmp_path / "state" / "guard_sessions.jsonl")
    assert entry["pid"] == os.getpid()
    assert entry["guard_version"] == guard.GUARD_VERSION
    assert entry["pins_sha256"] is None  # no pins file -> None, no crash


def test_main_refuses_when_chat_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DISABLED_TOOLS", "chat")
    assert guard.main() == 1


# --- 10) >64KB guard entry -> fail-closed; CLI bit-identity ------------------

def test_oversize_guard_entry_hard_fails_before_send(tmp_path, monkeypatch):
    monkeypatch.setenv("PAL_GUARD_STRICT", "0")  # target: ledger line limit
    paths = [str(tmp_path / f"file_{i:04d}.py") for i in range(600)]
    tool = new_tool()
    args = {"prompt": "lots of files\n", "absolute_file_paths": paths,
            "working_directory_absolute_path": str(tmp_path)}
    with pytest.raises(ToolExecutionError) as excinfo:
        asyncio.run(tool.execute(args))
    assert tool.provider.calls == []
    assert "ledger fail-closed" in str(excinfo.value.payload)
    (sc,) = sidecar_entries(tmp_path)
    assert sc["phase"] == "guard_reject"


def test_cli_bit_identity_unchanged(tmp_path):
    # the guard extension is inert for the CLI path: same entry bytes with
    # no coverage_extra / extra fields (Fase-1 golden, re-pinned here)
    state = tmp_path / "state"
    payload = tmp_path / "p.txt"
    payload.write_text("hello\n")
    code, entry, failures, out = pre_check.run_check(
        payload_paths=[str(payload)],
        prompt_content="fine\n",
        ledger_mode=True, run_id="bit-run", state=str(state))
    assert code == 0
    (line,) = read_jsonl(state / "pal_send_ledger.jsonl")
    assert set(line) == {"run_id", "ts", "model", "verdict", "prompt", "payloads",
                         "mcp_paths", "declared_extra", "plan_manifest",
                         "exclusions", "est_tokens", "est_cost_usd", "failures"}


# --- coverage_extra contract --------------------------------------------------

def test_coverage_extra_extends_mcp_path_check_and_exclusions(tmp_path):
    state = tmp_path / "state"
    delta = make_file(tmp_path, "delta.py", "new this round")
    history = make_file(tmp_path, "history.py", "from a previous round")
    # the manifest declares the history file; the round delta is NOT declared
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": [
        {"path": history, "sha256": hashlib.sha256(open(history, "rb").read()).hexdigest()},
    ]}))
    entry_in = {"path": delta, "realpath": os.path.realpath(delta),
                "sha256": "aa" * 32, "inode": 1, "size": 1, "mtime": 1.0}
    code, entry, failures, _ = pre_check.run_check(
        payload_entries=[entry_in],
        scan_texts=[("<new-user-input>", "clean\n")],
        prompt_content="ok\n",
        ledger_mode=True, run_id="cov-run", state=str(state),
        mcp_paths=[delta, history],
        coverage_extra=[history],
        plan_manifest=str(manifest))
    assert code == 0, failures
    assert failures == []
    # history is covered via coverage_extra -> not missing from the manifest
    assert entry["exclusions"]["missing_from_payload"] == []
    # delta is in the covered union but not in the manifest -> extra
    assert entry["exclusions"]["extra_in_payload"] == [os.path.realpath(delta)]

    # without coverage_extra the history file is an uncovered mcp_path and a
    # manifest miss (this is the bit the guard mode fixes)
    code2, entry2, failures2, _ = pre_check.run_check(
        payload_entries=[entry_in],
        scan_texts=[("<new-user-input>", "clean\n")],
        prompt_content="ok\n",
        ledger_mode=True, run_id="cov-run-2", state=str(state),
        mcp_paths=[delta, history],
        plan_manifest=str(manifest))
    assert code2 == 1
    assert any(f[0] == "mcp-path" for f in failures2)
    assert entry2["exclusions"]["missing_from_payload"] == [os.path.realpath(history)]
