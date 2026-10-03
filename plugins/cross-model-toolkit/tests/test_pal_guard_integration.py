"""Golden de integración — guard server contra el fork REAL pineado (GATE Fase 3).

Plan v2 §4 (D7/F5/F13): el golden de paridad corre contra el fork e4ffd36 con
el transport HTTP mockeado vía el hook oficial `_test_transport`
(providers/openai_compatible.py:276) y asevera IGUALDAD DEL BODY SERIALIZADO
COMPLETO entre `ChatTool` a pelo y `GuardedChatTool` (lista cerrada de
diferencias: VACÍA). Un segundo test DOCUMENTA el comportamiento real de una
ronda de continuación con fichero nuevo (rama efectiva de base.py:335 vs
reconstrucción in-tool, llegada del fichero nuevo, add_turn doble) — el
veredicto empírico de la tensión estática/empírica del plan §2.2.
Un tercer test (Fase 4, 2026-10-03) ejecuta DOS RONDAS GUARDED contra el fork
real (hilo fresco + continuación con fichero nuevo bajo STRICT=0) y asevera
sobre la entrada de ledger de la ronda 2: el guard observó la continuación,
history_files contiene ambos ficheros (canónicos) y la fórmula de perdidos no
produce falsos positivos; el golden de paridad además fija que el hook HTTP
sí se instala contra el SDK real vía `client._client` (H1).

Ejecución (opt-in):

  PAL_GUARD_INTEGRATION=1 uvx \
    --from git+https://github.com/sergiobe31/pal-mcp-server.git@e4ffd3609b56882510ccdca27883eea15d85b68a \
    --with pytest \
    python -m pytest plugins/cross-model-toolkit/tests/test_pal_guard_integration.py -q

Sin PAL_GUARD_INTEGRATION=1 el módulo entero se salta (los imports del fork no
existen fuera del entorno uv pineado). Los pins (`verify_pins`) no se exigen:
no se llama a `main()` del guard; se instala el wrapper y la tool directamente
sobre el fork real, con PAL_STATE_DIR en tmp.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile

import pytest

pytestmark = pytest.mark.integration

if os.environ.get("PAL_GUARD_INTEGRATION") != "1":
    pytest.skip("golden de integración: define PAL_GUARD_INTEGRATION=1 "
                "y corre bajo el entorno uv del fork pineado",
                allow_module_level=True)

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(TESTS_DIR, "..", "scripts"))
REGISTRY = os.path.abspath(os.path.join(TESTS_DIR, "..", "config",
                                        "pal_openrouter_models.json"))
MODEL = "z-ai/glm-5.2"

# Env ANTES de importar el fork (config.py lee el entorno en importación;
# el registry se resuelve vía get_env en providers/registries/openrouter.py).
os.environ["OPENROUTER_API_KEY"] = "sk-guard-integration-dummy-key"
os.environ["OPENROUTER_MODELS_CONFIG_PATH"] = REGISTRY
os.environ["DEFAULT_MODEL"] = MODEL
os.environ["PAL_GUARD_STRICT"] = "1"

sys.path.insert(0, SCRIPTS)

import httpx  # noqa: E402

import server as pal_server  # noqa: E402,F401  (fork real, pineado)
from providers import ModelProviderRegistry  # noqa: E402
from providers.shared import ProviderType  # noqa: E402
from tools.chat import ChatTool  # noqa: E402

import pal_guard_verify  # noqa: E402
import pal_guarded_server as guard  # noqa: E402


class RecordingTransport(httpx.BaseTransport):
    """Intercepta TODAS las peticiones HTTP del provider: registra el body
    exacto y devuelve un chat.completion OpenAI enlatado. Es el mock del
    transport del fork (openai_compatible.py:276 `hasattr _test_transport`)."""

    def __init__(self, canned_content):
        self.canned_content = canned_content
        self.requests = []  # [{"method", "url", "body_bytes"}]

    def handle_request(self, request):
        self.requests.append({"method": request.method,
                              "url": str(request.url),
                              "body": request.content})
        payload = {
            "id": "chatcmpl-guard-golden",
            "object": "chat.completion",
            "created": 0,
            "model": MODEL,
            "choices": [{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant",
                                      "content": self.canned_content}}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 7,
                      "total_tokens": 49},
        }
        return httpx.Response(200,
                              headers={"Content-Type": "application/json"},
                              content=json.dumps(payload).encode("utf-8"))


@pytest.fixture()
def fork_env(tmp_path, monkeypatch):
    """Provider configurado con transport mockeado; guard wrappers instalados;
    PAL_STATE_DIR/PAL_REQ_DUMP en tmp. Devuelve (transport, state_dir)."""
    state = tmp_path / "state"
    monkeypatch.setenv("PAL_STATE_DIR", str(state))
    dump = tmp_path / "reqdump.jsonl"
    monkeypatch.setenv("PAL_REQ_DUMP", str(dump))
    monkeypatch.setenv("LOCALE", "")

    pal_server.configure_providers()
    provider = ModelProviderRegistry.get_provider(ProviderType.OPENROUTER)
    assert provider is not None, "OpenRouterProvider no inicializado"
    transport = RecordingTransport("GUARD-INTEGRATION-MOCK-ANSWER")
    provider._test_transport = transport
    provider._client = None  # fuerza rebuild del client con el transport

    guard.install_generate_content_guard()
    guard.install_http_hook()
    return transport, state, dump


def _payload_file(directory, name, content):
    f = os.path.join(directory, name)
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(content)
    return f


@pytest.fixture()
def payload_dir(tmp_path):
    """Directorio de payloads FUERA del tmp del sistema: el fork bloquea
    /var (utils/security_config.py DANGEROUS_SYSTEM_PATHS) y macOS pone
    $TMPDIR bajo /var/folders, así que pytest tmp_path queda vetado para
    absolute_file_paths (read_files devuelve '--- NO FILES FOUND ---').
    /tmp sí está permitido (resuelve a /private/tmp)."""
    d = tempfile.mkdtemp(prefix="pal-guard-it-", dir="/tmp")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def _write_manifest(state, slug, files):
    manifests = state / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    dest = manifests / f"{slug}.json"
    tmp = manifests / f"{slug}.json.tmp"
    tmp.write_text(json.dumps({
        "files": [{"path": str(f), "sha256": _sha256_file(f)} for f in files],
    }, indent=2), encoding="utf-8")
    os.replace(tmp, dest)
    return dest


def _run(coro):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_request_body_parity_guarded_vs_bare(fork_env, tmp_path, payload_dir):
    """GATE §4: mismo input contra ChatTool a pelo vs GuardedChatTool →
    igualdad del body serializado completo (lista cerrada de diferencias:
    VACÍA). Ambos pasan por el MISMO provider transport mockeado; la única
    asimetría intencional es que el prompt abre con '[guard-session: parity]'
    EN AMBOS (el fork no la interpreta; el guard la exige)."""
    transport, state, _dump = fork_env
    f1 = _payload_file(payload_dir, "contexto.py",
                       "def saludo():\n    return 'hola guard'\n")
    _write_manifest(state, "parity", [f1])

    arguments = {
        "prompt": "[guard-session: parity]\nResume el fichero adjunto.",
        "absolute_file_paths": [str(f1)],
        "working_directory_absolute_path": payload_dir,
        "model": MODEL,
        "thinking_mode": "low",
    }

    bare = ChatTool()
    result_bare = _run(bare.execute(dict(arguments)))
    guarded = guard.GuardedChatTool()
    result_guarded = _run(guarded.execute(dict(arguments)))

    assert len(transport.requests) == 2, \
        f"se esperaban 2 peticiones HTTP, llegaron {len(transport.requests)}"
    body_bare = transport.requests[0]["body"]
    body_guarded = transport.requests[1]["body"]

    # Igualdad byte a byte del body serializado. Lista cerrada de
    # diferencias permitidas: VACÍA.
    assert body_bare == body_guarded, (
        "PARITY BREAK: el body del guarded diffiere del bare\n"
        f"  bare   : {body_bare[:400]!r}\n"
        f"  guarded: {body_guarded[:400]!r}")
    parsed = json.loads(body_bare)
    assert parsed["model"] == MODEL
    assert len(parsed["messages"]) >= 2  # system + user
    assert "hola guard" in json.dumps(parsed["messages"])  # fichero embebido

    # Fix 1 (H1): el hook HTTP SÍ se instala contra el client real del SDK —
    # install_http_hook prueba client.event_hooks y, si no existen, el
    # httpx.Client interno bajo client._client (openai_compatible.py:~312).
    # El transport mockeado coexiste con los event hooks de httpx, así que el
    # hook dispara y request_sent_var clasifica pre/post_send de verdad.
    provider = ModelProviderRegistry.get_provider(ProviderType.OPENROUTER)
    assert provider._pal_guard_http_hook_installed is True
    inner = getattr(provider.client, "_client", None)
    assert inner is not None, "el SDK OpenAI debe exponer su httpx.Client en _client"
    assert guard._http_request_hook in inner.event_hooks["request"]

    # El sidecar certifica la respuesta guarded con el hash canónico y
    # pal_guard_verify cierra el loop deliberación↔sidecar.
    sidecar = state / "pal_guard_responses.jsonl"
    records = [json.loads(l) for l in sidecar.read_text().splitlines() if l]
    ok = [r for r in records if r.get("phase") == "response_ok"]
    assert ok, f"sin registro response_ok en sidecar: {records!r}"
    last = ok[-1]
    response_file = tmp_path / "response.json"
    response_file.write_text(result_guarded[0].text, encoding="utf-8")
    assert pal_guard_verify.main(
        ["--run-id", last["run_id"], "--response-json", str(response_file),
         "--state", str(state)]) == 0


def test_continuation_round_behavior_documented(fork_env, tmp_path, payload_dir):
    """Veredicto empírico de la tensión estática/empírica del plan §2.2
    (resuelta por evidencia, plan §6.1).

    Replica el path MCP REAL: ronda 1 en hilo fresco con fichero A; ronda 2
    vía server.reconstruct_thread_context (server.py:775) con continuation_id
    y un fichero NUEVO B. Mide: (1) qué rama de SimpleTool.execute dispara
    (base.py:335 '=== CONVERSATION HISTORY ===' vs reconstrucción in-tool con
    '=== CONVERSATION HISTORY (CONTINUATION) ==='), (2) si el contenido de B
    llega al prompt efectivo y por qué vía, (3) si add_turn queda duplicado,
    (4) anidamiento del prompt enriquecido. El guard NO compone el prompt —
    comportamiento puro del fork (ChatTool a pelo; el guard solo observaría).

    VEREDICTO OBSERVADO (e4ffd36): la rama (c) se dispara (el substring de
    base.py:335 NO casa con el marcador real); el prompt enriquecido del
    servidor queda REGISTRADO como turno user (add_turn duplicado:
    server.py:1088 + base.py:353) y el historial queda ANIDADO (el prompt
    efectivo contiene el marcador (CONTINUATION) 3 veces). El fichero nuevo B
    SÍ llega al modelo — NO por el delta (filter_new_files lo salta con la
    nota 'already available'), sino por RE-LECTURA DE DISCO en la sección
    '=== FILES REFERENCED IN THIS CONVERSATION ===' de la historia in-tool.
    Corolario: el minimal repro NO reproduce la pérdida de ficheros del
    incidente del debate (2026-09-30) — esa corrida inicial estaba confundida
    por el bloqueo de /var del fork (security_config.py); la pérdida real
    exige otro factor (tamaño/truncado) que este golden minimal no reproduce.
    Lo que el golden FIJA: rama (c) + add_turn duplicado + historia anidada +
    B llegando vía history_files (coherente con la fórmula F4 del guard:
    lost = requested − processed − skipped_budget − history_files = ∅)."""
    transport, state, _dump = fork_env
    fA = _payload_file(payload_dir, "fichero_A.py", "VALOR_UNICO_A = 111\n")
    fB = _payload_file(payload_dir, "fichero_B.py", "VALOR_UNICO_B = 222\n")

    args1 = {
        "prompt": "Primera ronda: analiza el fichero A.",
        "absolute_file_paths": [str(fA)],
        "working_directory_absolute_path": payload_dir,
        "model": MODEL,
        "thinking_mode": "low",
    }
    bare = ChatTool()
    r1 = _run(bare.execute(dict(args1)))
    offer = json.loads(r1[0].text)["continuation_offer"]
    continuation_id = offer["continuation_id"]

    args2 = {
        "prompt": "Segunda ronda: ahora necesito también el fichero B.",
        "absolute_file_paths": [str(fB)],
        "working_directory_absolute_path": payload_dir,
        "model": MODEL,
        "thinking_mode": "low",
        "continuation_id": continuation_id,
    }
    import asyncio
    enhanced = asyncio.new_event_loop().run_until_complete(
        pal_server.reconstruct_thread_context(dict(args2)))
    assert enhanced["prompt"] != args2["prompt"], \
        "reconstruct_thread_context no enriqueció el prompt"
    n_requests_before = len(transport.requests)
    r2 = _run(bare.execute(enhanced))
    assert json.loads(r2[0].text)["status"] == "continuation_available"
    assert len(transport.requests) == n_requests_before + 1

    prompt_r2 = json.loads(
        transport.requests[-1]["body"])["messages"][-1]["content"]
    if not isinstance(prompt_r2, str):
        prompt_r2 = json.dumps(prompt_r2)

    branch_preembedded = "=== CONVERSATION HISTORY ===" in prompt_r2
    branch_intool_marker = "=== CONVERSATION HISTORY (CONTINUATION) ===" \
        in prompt_r2
    # base.py:335 testa el substring '=== CONVERSATION HISTORY ==='; el
    # marcador real del fork lleva '(CONTINUATION)' y NO casa con él.
    branch = ("base.py:335 pre-embedded" if branch_preembedded
              else "reconstrucción in-tool (rama c)")
    file_b_arrived = "VALOR_UNICO_B" in prompt_r2
    # vía de llegada de B: delta (CONTEXT FILES) vs historia (re-lectura disco)
    history_section = "=== FILES REFERENCED IN THIS CONVERSATION ==="
    file_b_via_history = False
    if file_b_arrived and history_section in prompt_r2:
        file_b_via_history = "VALOR_UNICO_B" in prompt_r2.split(history_section, 1)[1]
    file_b_via_note = ("--- NOTE: Additional files referenced in conversation "
                       "history ---") in prompt_r2
    file_a_arrived = "VALOR_UNICO_A" in prompt_r2
    nesting = prompt_r2.count("=== CONVERSATION HISTORY (CONTINUATION) ===")

    from utils.conversation_memory import get_thread
    thread = get_thread(continuation_id)
    user_turns_r2 = [t for t in thread.turns
                     if t.role == "user"
                     and "Segunda ronda" in (t.content or "")]
    add_turn_double = len(user_turns_r2) > 1

    report = f"""
================ CONTINUATION ROUND — EMPIRICAL VERDICT (fork e4ffd36) ================
rama efectiva de SimpleTool.execute        : {branch}
  marcador base.py:335 casado               : {branch_preembedded}
  marcador real '(CONTINUATION)' presente   : {branch_intool_marker}
  anidamiento (veces marcador en prompt)    : {nesting}
fichero A (ronda 1) en prompt efectivo      : {file_a_arrived}
fichero B (NUEVO ronda 2) en prompt efectivo: {file_b_arrived}
  B vía sección FILES REFERENCED (disco)    : {file_b_via_history}
  nota 'already available' presente         : {file_b_via_note}
add_turn duplicado en el thread             : {add_turn_double} (turnos user ronda2: {len(user_turns_r2)}, total turnos: {len(thread.turns)})
========================================================================================"""
    print(report)

    # Documentado como hallazgo empírico — el test FIJA el comportamiento
    # observado para que una actualización del fork no lo cambie en silencio.
    assert branch_preembedded is False, \
        "el substring de base.py:335 casó: la rama (b) SÍ se dispara (el fork cambió)"
    assert branch_intool_marker is True
    assert add_turn_double is True, \
        "se esperaba el add_turn duplicado (server.py:1088 + base.py:353)"
    # Veredicto central del golden: el fichero nuevo SÍ llega, vía history
    # (re-lectura de disco in-tool), no vía delta. Si esto cambia (el fork
    # pasa a perderlo de verdad), la convención 'hilo fresco' se vuelve aún
    # más crítica y la fórmula F4 del guard seguirá detectándolo.
    assert file_b_arrived is True
    assert file_b_via_history is True
    assert file_b_via_note is True


def test_guarded_continuation_round_observed(fork_env, tmp_path, payload_dir,
                                              monkeypatch):
    """Fix 6 (H4): dos rondas GUARDED contra el fork real con transport
    mockeado. Ronda 1: hilo fresco con fichero A. Ronda 2: continuation_id +
    fichero B nuevo bajo PAL_GUARD_STRICT=0 (Fix 2 impone hilo fresco para
    continuación+ficheros bajo STRICT). Asevera contra la entrada de ledger
    de la ronda 2: el guard observó la continuación real, history_files
    contiene A y B (paths canónicos — el fork embebe /private/tmp para
    requests /tmp), y la fórmula de perdidos no reporta falsos positivos.
    Valida _parse_history_files (anclado al marcador CONVERSATION HISTORY),
    el END marker y la canonización contra el formato real de
    conversation_memory.py."""
    monkeypatch.setenv("PAL_GUARD_STRICT", "0")
    transport, state, _dump = fork_env
    fA = _payload_file(payload_dir, "guarded_A.py", "GUARDED_A = 111\n")
    fB = _payload_file(payload_dir, "guarded_B.py", "GUARDED_B = 222\n")

    guarded = guard.GuardedChatTool()
    args1 = {
        "prompt": "Primera ronda guarded: analiza el fichero A.",
        "absolute_file_paths": [str(fA)],
        "working_directory_absolute_path": payload_dir,
        "model": MODEL,
        "thinking_mode": "low",
    }
    r1 = _run(guarded.execute(dict(args1)))
    data1 = json.loads(r1[0].text)
    assert data1["status"] == "continuation_available"
    continuation_id = data1["continuation_offer"]["continuation_id"]

    args2 = {
        "prompt": "Segunda ronda guarded: ahora también el fichero B.",
        "absolute_file_paths": [str(fB)],
        "working_directory_absolute_path": payload_dir,
        "model": MODEL,
        "thinking_mode": "low",
        "continuation_id": continuation_id,
    }
    import asyncio
    enhanced = asyncio.new_event_loop().run_until_complete(
        pal_server.reconstruct_thread_context(dict(args2)))
    # forma real del fork: el historial va PRIMERO (server.py:1230)
    assert enhanced["prompt"].startswith(guard.CONVERSATION_HISTORY_MARKER)
    n_requests_before = len(transport.requests)
    r2 = _run(guarded.execute(enhanced))
    assert json.loads(r2[0].text)["status"] == "continuation_available"
    assert len(transport.requests) == n_requests_before + 1

    entries = [json.loads(l) for l in
               (state / "pal_send_ledger.jsonl").read_text().splitlines() if l]
    guarded_entries = [e for e in entries if e.get("guard")]
    assert len(guarded_entries) == 2, \
        f"el guard debió auditar ambas rondas: {entries!r}"
    e2 = guarded_entries[-1]
    history_canon = {os.path.realpath(p) for p in e2["history_files"]}
    assert os.path.realpath(fA) in history_canon
    assert os.path.realpath(fB) in history_canon
    assert e2["files_lost_to_dedup_bug"] == []
    assert e2["processed_files_delta"] == []  # B filtrado por el thread

    sidecar = [json.loads(l) for l in
               (state / "pal_guard_responses.jsonl").read_text().splitlines()
               if l]
    ok = [r for r in sidecar if r.get("phase") == "response_ok"]
    assert len(ok) == 2, f"se esperaban 2 response_ok: {sidecar!r}"

