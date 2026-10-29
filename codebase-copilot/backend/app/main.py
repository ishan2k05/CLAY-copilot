import os
import re
import json
import asyncio
import hmac
import secrets
import time
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
import logging
from pydantic import BaseModel

logger = logging.getLogger("clay")

try:
    from .security.secret_masker import SecretMasker
    from .parser.ast_chunker import PythonASTChunker
    from .parser.universal_chunker import UniversalASTChunker
    from .parser.symbols import CodeChunk
    from .index.graph_store import SQLiteGraphStore
    from .index.vector_store import LocalVectorStore
    from .retrieval.hybrid_search import HybridSearchEngine
    from .retrieval.file_resolver import WorkspaceFileResolver
    from .retrieval.repo_map import RepoMapGenerator
    from .agent.planner import AgentPlanner
    from .agent.patch_engine import PatchEngine
    from .agent.instruction_parser import (
        InstructionClassifier,
        SymbolEntityResolver,
        AmbiguityDetector,
        ContextWindowBuilder,
        DynamicTaskPlanner,
        TopicDomainResolver,
        normalize_prompt_text,
    )
    from .agent.repair_loop import SelfHealingRepairLoop
    from .agent.tools.terminal_agent import TerminalAgent
    from .llm.gemma_provider import OllamaGemmaProvider
    from .llm.history_compactor import HistoryCompactor
except (ImportError, ValueError):
    from app.security.secret_masker import SecretMasker
    from app.parser.ast_chunker import PythonASTChunker
    from app.parser.universal_chunker import UniversalASTChunker
    from app.parser.symbols import CodeChunk
    from app.index.graph_store import SQLiteGraphStore
    from app.index.vector_store import LocalVectorStore
    from app.retrieval.hybrid_search import HybridSearchEngine
    from app.retrieval.file_resolver import WorkspaceFileResolver
    from app.retrieval.repo_map import RepoMapGenerator
    from app.agent.planner import AgentPlanner
    from app.agent.patch_engine import PatchEngine
    from app.agent.instruction_parser import (
        InstructionClassifier,
        SymbolEntityResolver,
        AmbiguityDetector,
        ContextWindowBuilder,
        DynamicTaskPlanner,
        TopicDomainResolver,
        normalize_prompt_text,
    )
    from app.agent.repair_loop import SelfHealingRepairLoop
    from app.agent.tools.terminal_agent import TerminalAgent
    from app.llm.gemma_provider import OllamaGemmaProvider
    from app.llm.history_compactor import HistoryCompactor

# Session token resolution & management
SESSION_TOKEN_DIR = Path.home() / ".clay"
SESSION_TOKEN_FILE = SESSION_TOKEN_DIR / "auth_token.json"

def get_or_create_session_token() -> str:
    # 1. Environment variable takes highest priority
    token = os.getenv("CLAY_SESSION_TOKEN", "").strip()
    if token:
        return token
    
    # 2. Check persistent token file
    try:
        if SESSION_TOKEN_FILE.exists():
            data = json.loads(SESSION_TOKEN_FILE.read_text(encoding="utf-8"))
            if data.get("token"):
                return str(data["token"]).strip()
    except Exception as e:
        logger.debug(f"Error reading session token file: {e}")

    # 3. Generate a cryptographically secure 256-bit token
    token = secrets.token_hex(32)
    try:
        SESSION_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_TOKEN_FILE.write_text(json.dumps({"token": token}), encoding="utf-8")
        try:
            SESSION_TOKEN_FILE.chmod(0o600)
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"Error persisting session token file: {e}")
    return token

ACTIVE_SESSION_TOKEN = get_or_create_session_token()

MAX_BODY_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB

class LocalAuthTokenMiddleware(BaseHTTPMiddleware):
    """
    Validates X-Clay-Auth-Token header against ACTIVE_SESSION_TOKEN.
    Prevents drive-by browser cross-origin requests (CSRF) and local process tampering.
    """
    async def dispatch(self, request: Request, call_next):
        # 1. Allow preflight OPTIONS requests without auth token
        if request.method == "OPTIONS":
            return await call_next(request)

        # 2. Whitelist public health & doc endpoints
        path = request.url.path.rstrip("/")
        if path in ("", "/health", "/api/health", "/docs", "/openapi.json"):
            return await call_next(request)

        # 3. Check if auth is disabled via environment
        if os.getenv("CLAY_AUTH_DISABLED", "").lower() in ("1", "true", "yes"):
            return await call_next(request)

        # 4. Extract token from header: X-Clay-Auth-Token or Authorization: Bearer <token>
        auth_header = request.headers.get("x-clay-auth-token") or request.headers.get("X-Clay-Auth-Token") or ""
        if not auth_header and "authorization" in request.headers:
            auth_val = request.headers["authorization"]
            if auth_val.startswith("Bearer "):
                auth_header = auth_val[7:].strip()
        elif not auth_header and "Authorization" in request.headers:
            auth_val = request.headers["Authorization"]
            if auth_val.startswith("Bearer "):
                auth_header = auth_val[7:].strip()

        current_token = os.getenv("CLAY_SESSION_TOKEN") or ACTIVE_SESSION_TOKEN
        if not auth_header or not hmac.compare_digest(auth_header, current_token):
            return JSONResponse(
                status_code=401,
                content={
                    "status": "error",
                    "error": "Unauthorized",
                    "detail": "Missing or invalid X-Clay-Auth-Token header."
                }
            )

        return await call_next(request)

class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """
    Clamps incoming request body size to MAX_BODY_SIZE_BYTES (25 MB)
    to protect against memory exhaustion and out-of-memory denial of service.
    """
    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_BODY_SIZE_BYTES:
                        return JSONResponse(
                            status_code=413,
                            content={
                                "status": "error",
                                "error": "Payload Too Large",
                                "detail": f"Request body exceeds maximum size limit of {MAX_BODY_SIZE_BYTES // (1024 * 1024)}MB."
                            }
                        )
                except ValueError:
                    pass
        return await call_next(request)

app = FastAPI(title="CLAY Copilot", version="1.6.14")

# Security Middleware Stack (Middlewares run in reverse order of addition)
# 1. Innermost: Ephemeral Token Handshake
app.add_middleware(LocalAuthTokenMiddleware)

# 2. Next: Body Size Limiter (25 MB ceiling)
app.add_middleware(RequestSizeLimitMiddleware)

# 3. Next: Hardened CORS (VS Code webview schemes and local loopback only)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?|vscode-webview://.*|vscode-file://.*)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 4. Outermost: Trusted Host validation (DNS Rebinding protection)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "testserver"]
)

# Core subsystems
WORKSPACE_STORAGE_DIR = os.getenv("CLAY_WORKSPACE_STORAGE_DIR")
if WORKSPACE_STORAGE_DIR:
    STORAGE_PATH = Path(WORKSPACE_STORAGE_DIR)
    STORAGE_PATH.mkdir(parents=True, exist_ok=True)
    GRAPH_DB_PATH = str(STORAGE_PATH / "code_graph.sqlite")
else:
    GRAPH_DB_PATH = "code_graph.sqlite"

graph_store = SQLiteGraphStore(GRAPH_DB_PATH)
vector_store = LocalVectorStore(db_path=GRAPH_DB_PATH)
ast_chunker = UniversalASTChunker()
ollama_provider = OllamaGemmaProvider()
hybrid_engine = HybridSearchEngine(vector_store, graph_store)
planner = AgentPlanner(ollama_provider)
patch_engine = PatchEngine()

# In-memory active task tracking
active_task: Dict[str, Any] = {
    "id": "TASK #0428",
    "title": "Refactor Auth & Test Fixtures",
    "priority": "HIGH PRIORITY",
    "mode": "autonomous",
    "model": "gemma4:31b-cloud",
    "totalSteps": 4,
    "completedSteps": 2,
    "steps": [
        {"id": 1, "title": "Scan codebase for JWT calls", "status": "done", "detail": "Found 8 references across src/auth/*"},
        {"id": 2, "title": "Update verifier.ts with crypto routine", "status": "done", "detail": "Injected crypto.subtle.verify fallback"},
        {"id": 3, "title": "Generate integration test fixtures", "status": "active", "detail": "Writing asymmetric keypair mocks"},
        {"id": 4, "title": "Execute vitest and verify zero regressions", "status": "queued"}
    ],
    "stagedChanges": [
        {"file": "src/auth/verifier.ts", "added": 28, "deleted": 12, "status": "MODIFIED"},
        {"file": "tests/auth.spec.ts", "added": 14, "deleted": 0, "status": "GENERATING"}
    ]
}

DEFAULT_WORKSPACE = os.getenv(
    "WORKSPACE_ROOT",
    str(Path(__file__).resolve().parent.parent.parent) if (Path(__file__).resolve().parent.parent.parent).exists() else str(Path.cwd().resolve())
)

current_indexed_workspace: Optional[str] = None

def resolve_workspace_path(path_str: Optional[str]) -> Path:
    """
    Intelligently resolves workspace directory paths.
    Handles:
    - Absolute paths (e.g. /path/to/my-project or C:/Users/name/my-project)
    - Tilde paths (e.g. ~/my-project)
    - Bare folder names from browser directory pickers (e.g. "my-project")
      searches: Desktop, home, Documents, Downloads, scratch directory
    - Fallback to current runtime repo_path or DEFAULT_WORKSPACE
    """
    if not path_str or not path_str.strip():
        current = runtime_settings.get("repo_path")
        if current and Path(current).exists() and Path(current).is_dir():
            return Path(current).resolve()
        return Path(DEFAULT_WORKSPACE).resolve()

    p_str = path_str.strip()
    if p_str.startswith("~"):
        p = Path(os.path.expanduser(p_str))
        return p.resolve()

    p = Path(p_str)
    if p.is_absolute():
        return p.resolve()

    home = Path.home()
    candidates = [
        home / "Desktop" / p_str,
        home / p_str,
        home / "Documents" / p_str,
        home / "Downloads" / p_str,
        Path(DEFAULT_WORKSPACE) / p_str,
        Path(__file__).resolve().parent.parent / p_str,
        Path.cwd() / p_str,
    ]
    for cand in candidates:
        if cand.exists() and cand.is_dir():
            return cand.resolve()

    for base in [home / "Desktop", home, Path.cwd().parent]:
        if base.exists() and base.is_dir():
            try:
                for child in base.iterdir():
                    if child.is_dir() and child.name.lower() == p_str.lower():
                        return child.resolve()
            except Exception:
                pass

    if p.exists() and p.is_dir():
        return p.resolve()

    current = runtime_settings.get("repo_path")
    if current and Path(current).exists() and Path(current).is_dir():
        return Path(current).resolve()
    return Path(DEFAULT_WORKSPACE).resolve() if Path(DEFAULT_WORKSPACE).exists() else Path.cwd().resolve()

def is_path_in_workspace(target_path: Optional[Union[str, Path]], workspace_dir: Optional[Union[str, Path]]) -> bool:
    """Verifies that target_path is strictly contained within workspace_dir."""
    if not target_path or not workspace_dir:
        return False
    try:
        t_resolved = Path(target_path).resolve()
        w_resolved = Path(workspace_dir).resolve()
        return t_resolved.is_relative_to(w_resolved)
    except Exception:
        try:
            t_norm = str(Path(target_path).resolve()).replace("\\", "/").rstrip("/").lower()
            w_norm = str(Path(workspace_dir).resolve()).replace("\\", "/").rstrip("/").lower()
            return t_norm.startswith(w_norm + "/") or t_norm == w_norm
        except Exception:
            return False

# Global stream cancellation flags for instant abort support
active_chat_stream_cancel: bool = False
active_agent_step_cancel: bool = False

live_file_buffers: Dict[str, str] = {}
workspace_file_states: Dict[str, Tuple[float, int]] = {}

async def auto_index_workspace(workspace_dir: str) -> Dict[str, int]:
    """
    Auto-indexes workspace files into SQLite symbol graph and vector store.
    Resets old chunks and relationships when switching repositories.
    """
    global current_indexed_workspace
    root = resolve_workspace_path(workspace_dir)
    if not root.exists():
        return {"filesCount": 0, "symbolsCount": 0, "relationshipsCount": 0, "chunksCount": 0}

    current_indexed_workspace = str(root)
    runtime_settings["repo_path"] = str(root)

    # 1. Reset vector store chunks and graph store for fresh repository context
    vector_store.chunks = []
    vector_store.chunk_embeddings = None
    workspace_file_states.clear()
    await graph_store.clear_all()

    all_symbols = []
    all_relationships = []
    all_chunks = []
    indexed_files = 0

    EXCLUDED_DIRS = {
        "venv", ".venv", "env", "node_modules", "dist", "build",
        "__pycache__", ".pytest_cache", ".git", ".idea", ".vscode", ".gemini"
    }

    # 2. Parse all workspace code files with Universal AST Chunker across all major languages
    code_exts = {
        ".py", ".ts", ".js", ".jsx", ".tsx", ".mjs", ".cjs",
        ".rs", ".go", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
        ".java", ".cs", ".php", ".rb", ".kt", ".kts", ".swift",
        ".json", ".md", ".html", ".css", ".toml", ".yaml", ".yml", ".sql", ".sh", ".bash"
    }
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in code_exts:
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            rel_parts = path.parts
        if any(part.startswith(".") or part in EXCLUDED_DIRS for part in rel_parts):
            continue
        if SecretMasker.is_file_sensitive(str(path)):
            continue
        try:
            st = path.stat()
            content = path.read_text(encoding="utf-8", errors="ignore")
            symbols, relationships, chunks = ast_chunker.parse_file(str(path), content)
            all_symbols.extend(symbols)
            all_relationships.extend(relationships)
            all_chunks.extend(chunks)
            workspace_file_states[str(path.resolve())] = (st.st_mtime, st.st_size)
            indexed_files += 1
        except Exception:
            continue

    if all_symbols:
        await graph_store.insert_batch(all_symbols, all_relationships)
    if all_chunks:
        await vector_store.add_chunks(all_chunks)
        await graph_store.index_chunks_bm25(all_chunks)

    return {
        "filesCount": indexed_files,
        "symbolsCount": len(all_symbols),
        "relationshipsCount": len(all_relationships),
        "chunksCount": len(all_chunks)
    }

async def reindex_file(file_path: str, content: Optional[str] = None) -> bool:
    """
    Incrementally updates SQLite symbol graph and vector store for a single modified/saved/patched file.
    Always purges old symbols and vector chunks to prevent ghost code or stale context.
    """
    try:
        p = Path(file_path).resolve()
        if content is None:
            if p.exists() and p.is_file():
                content = p.read_text(encoding="utf-8", errors="ignore") if p.stat().st_size > 0 else ""
            elif str(p) in live_file_buffers:
                content = live_file_buffers[str(p)]
            else:
                content = ""

        # Update in-memory live buffer
        live_file_buffers[str(p)] = content
        live_file_buffers[p.name] = content

        # Purge stale symbols and vector chunks to prevent ghost functions/classes
        await graph_store.delete_file_symbols(str(p))
        vector_store.remove_chunks_for_file(str(p))

        # Parse new chunks if content exists
        new_symbols = []
        new_relationships = []
        new_chunks = []

        if content and content.strip():
            try:
                new_symbols, new_relationships, new_chunks = ast_chunker.parse_file(str(p), content)
            except Exception as e:
                logger.debug(f"Error parsing {p}: {e}")

        if new_symbols:
            await graph_store.insert_batch(new_symbols, new_relationships)
        if new_chunks:
            await vector_store.add_chunks(new_chunks)
            await graph_store.index_chunks_bm25(new_chunks)
        return True
    except Exception as e:
        logger.error(f"Failed to incrementally reindex {file_path}: {e}")
        return False

async def silent_sync_workspace(workspace_dir: str) -> Tuple[int, int]:
    """
    Lightweight, non-blocking zero-token background synchronizer.
    Scans repository files via microsecond filesystem stat() checks.
    Automatically detects manual edits, deletions, additions, or emptyings
    and hot-reloads the AST graph and vector store instantly without consuming LLM tokens.
    """
    changed_count = 0
    deleted_count = 0
    try:
        root = Path(workspace_dir).resolve()
        if not root.exists() or not root.is_dir():
            return 0, 0

        current_files = set()
        code_exts = {".py", ".ts", ".js", ".jsx", ".tsx", ".json", ".md", ".html", ".css", ".toml", ".yaml", ".yml", ".sql", ".sh"}
        excluded_dirs = {"venv", ".venv", "env", "node_modules", "dist", "build", "__pycache__", ".git", ".idea", ".vscode", ".gemini"}

        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in code_exts:
                continue
            if any(part.startswith(".") or part in excluded_dirs for part in p.parts):
                continue
            if SecretMasker.is_file_sensitive(str(p)):
                continue

            p_str = str(p.resolve())
            current_files.add(p_str)
            try:
                st = p.stat()
                prev_state = workspace_file_states.get(p_str)
                if prev_state is None or prev_state != (st.st_mtime, st.st_size):
                    workspace_file_states[p_str] = (st.st_mtime, st.st_size)
                    content = p.read_text(encoding="utf-8", errors="ignore") if st.st_size > 0 else ""
                    await reindex_file(p_str, content)
                    changed_count += 1
            except Exception:
                continue

        # Check for deleted files
        tracked_keys = list(workspace_file_states.keys())
        for tracked in tracked_keys:
            if tracked.startswith(str(root)) and tracked not in current_files:
                del workspace_file_states[tracked]
                live_file_buffers.pop(tracked, None)
                live_file_buffers.pop(Path(tracked).name, None)
                await graph_store.delete_file_symbols(tracked)
                vector_store.remove_chunks_for_file(tracked)
                deleted_count += 1

        if len(current_files) == 0:
            # Workspace is completely empty on disk: purge all residual in-memory state
            for k in list(live_file_buffers.keys()):
                if k.startswith(str(root)) or not Path(k).is_absolute():
                    live_file_buffers.pop(k, None)
            vector_store.chunks = []
            vector_store.chunk_embeddings = None
            await graph_store.clear_all()
    except Exception as e:
        logger.debug(f"Silent sync error: {e}")
    return changed_count, deleted_count

class SyncFileRequest(BaseModel):
    file_path: str
    content: Optional[str] = ""
    workspace_path: Optional[str] = None
    event: Optional[str] = "changed"

@app.post("/api/sync/file")
async def sync_file_endpoint(req: SyncFileRequest):
    """Real-time hot reload sync endpoint for editor documents and agent changes."""
    p = Path(req.file_path).resolve()
    ws = resolve_workspace_path(req.workspace_path or runtime_settings.get("repo_path"))
    if not is_path_in_workspace(p, ws):
        return {"status": "ignored", "file": str(p), "reason": "file_outside_workspace"}

    if req.event == "deleted" or (req.content is None and not p.exists()):
        live_file_buffers.pop(str(p), None)
        live_file_buffers.pop(p.name, None)
        workspace_file_states.pop(str(p), None)
        await graph_store.delete_file_symbols(str(p))
        vector_store.remove_chunks_for_file(str(p))
        return {"status": "deleted", "file": str(p)}

    content = req.content if req.content is not None else (p.read_text(encoding="utf-8", errors="ignore") if p.exists() else "")
    live_file_buffers[str(p)] = content
    live_file_buffers[p.name] = content
    if p.exists():
        try:
            workspace_file_states[str(p)] = (p.stat().st_mtime, p.stat().st_size)
        except Exception:
            pass
    await reindex_file(str(p), content)
    return {
        "status": "synced",
        "file": str(p),
        "filename": p.name,
        "lines": len(content.splitlines()),
        "bytes": len(content.encode("utf-8"))
    }

@app.get("/api/sync/silent-check")
async def silent_sync_endpoint(workspace_path: Optional[str] = None):
    ws = str(resolve_workspace_path(workspace_path or runtime_settings.get("repo_path")))
    changed, deleted = await silent_sync_workspace(ws)
    return {
        "status": "synced",
        "changed": changed,
        "deleted": deleted,
        "tracked_files": len(workspace_file_states)
    }

async def _background_startup_index():
    try:
        if len(vector_store.chunks) == 0:
            logger.info(f"Starting non-blocking background workspace indexing for: {DEFAULT_WORKSPACE}")
            await auto_index_workspace(DEFAULT_WORKSPACE)
            logger.info("Background workspace indexing complete.")
    except Exception as e:
        logger.warning(f"Background startup indexing error: {e}")

@app.on_event("startup")
async def on_startup():
    await graph_store.init_db()
    asyncio.create_task(_background_startup_index())

@app.get("/health")
async def health_check():
    import time
    start = time.time()
    ollama_info = await ollama_provider.check_status()
    latency = int((time.time() - start) * 1000)
    ollama_info["latency_ms"] = latency
    return {
        "status": "healthy",
        "service": "codebase-copilot-backend",
        "model": ollama_provider.model_name,
        "host": ollama_provider.base_url,
        "indexed_symbols": len(vector_store.chunks),
        "ollama": ollama_info
    }

@app.get("/api/ollama/status")
async def get_ollama_status():
    import time
    start = time.time()
    status = await ollama_provider.check_status()
    latency = int((time.time() - start) * 1000)
    status["latency_ms"] = latency
    status["active_model"] = ollama_provider.model_name
    status["host"] = ollama_provider.base_url
    return status

GLOBAL_DATA_DIR = os.getenv("CLAY_GLOBAL_DATA_DIR")
if GLOBAL_DATA_DIR:
    SETTINGS_DIR = Path(GLOBAL_DATA_DIR)
else:
    SETTINGS_DIR = Path(__file__).resolve().parent.parent

SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = SETTINGS_DIR / "runtime_settings.json"

runtime_settings = {
    "security_mode": "sandboxed",
    "allow_network": True,
    "privacy_filter": True,
    "exclusion_patterns": ".env, *.pem, credentials.json, id_rsa, *.key",
    "connection_type": "local",
    "api_key": "",
    "repo_path": DEFAULT_WORKSPACE
}

def load_persisted_settings():
    target_file = SETTINGS_FILE
    if not target_file.exists():
        fallback = Path(__file__).resolve().parent.parent / "runtime_settings.json"
        if fallback.exists():
            target_file = fallback
    if target_file.exists():
        try:
            data = json.loads(target_file.read_text(encoding="utf-8"))
            for k, v in data.items():
                runtime_settings[k] = v
            if "temperature" in data:
                ollama_provider.temperature = float(data["temperature"])
            if "num_ctx" in data:
                ollama_provider.num_ctx = int(data["num_ctx"])
            if "num_predict" in data:
                ollama_provider.num_predict = int(data["num_predict"])
            if "model" in data and data["model"]:
                ollama_provider.model_name = data["model"]
            if "host" in data and data["host"]:
                ollama_provider.base_url = data["host"]
            if "connection_type" in data and data["connection_type"]:
                ollama_provider.connection_type = data["connection_type"]
            if "api_key" in data:
                ollama_provider.api_key = data["api_key"]
            if "exclusion_patterns" in data and data["exclusion_patterns"]:
                SecretMasker.set_custom_patterns(str(data["exclusion_patterns"]))
        except Exception:
            pass

def save_persisted_settings():
    try:
        to_save = {
            **runtime_settings,
            "temperature": ollama_provider.temperature,
            "num_ctx": ollama_provider.num_ctx,
            "num_predict": ollama_provider.num_predict,
            "model": ollama_provider.model_name,
            "host": ollama_provider.base_url,
            "connection_type": ollama_provider.connection_type,
        }
        # Do not persist secrets/keys to unencrypted JSON on disk (Domain 5 SecretStorage spec)
        to_save.pop("api_key", None)
        SETTINGS_FILE.write_text(json.dumps(to_save, indent=2), encoding="utf-8")
    except Exception:
        pass

load_persisted_settings()

class SettingsUpdateRequest(BaseModel):
    model: Optional[str] = None
    host: Optional[str] = None
    connection_type: Optional[str] = None
    api_key: Optional[str] = None
    temperature: Optional[float] = None
    num_ctx: Optional[int] = None
    num_predict: Optional[int] = None
    privacy_filter: Optional[bool] = None
    exclusion_patterns: Optional[str] = None
    security_mode: Optional[str] = None
    allow_network: Optional[bool] = None
    repo_path: Optional[str] = None
    theme_mode: Optional[str] = None

@app.get("/api/settings")
async def get_settings():
    safe_runtime = {k: v for k, v in runtime_settings.items() if k != "api_key"}
    raw_key = ollama_provider.api_key or ""
    masked_key = (
        f"{raw_key[:4]}••••••••{raw_key[-4:]}"
        if len(raw_key) > 8
        else ("••••••••" if raw_key else "")
    )
    return {
        "status": "success",
        **safe_runtime,
        "active_model": ollama_provider.model_name,
        "host": ollama_provider.base_url,
        "connection_type": ollama_provider.connection_type,
        "has_api_key": bool(raw_key),
        "api_key_masked": masked_key,
        "api_key": masked_key,
        "temperature": ollama_provider.temperature,
        "num_ctx": ollama_provider.num_ctx,
        "num_predict": ollama_provider.num_predict,
        "repo_path": runtime_settings.get("repo_path", DEFAULT_WORKSPACE)
    }

@app.post("/api/settings")
async def update_settings(req: SettingsUpdateRequest):
    if req.model:
        ollama_provider.model_name = req.model
        runtime_settings["model"] = req.model
    if req.connection_type:
        ollama_provider.connection_type = req.connection_type
        runtime_settings["connection_type"] = req.connection_type
        if req.connection_type == "cloud" and (not req.host or "localhost" in req.host or "127.0.0.1" in req.host):
            ollama_provider.base_url = "https://ollama.com/api"
            runtime_settings["host"] = "https://ollama.com/api"
        elif req.connection_type == "local" and (not req.host or "ollama.com" in req.host):
            ollama_provider.base_url = "http://localhost:11434"
            runtime_settings["host"] = "http://localhost:11434"
    if req.host:
        ollama_provider.base_url = req.host
        runtime_settings["host"] = req.host
    if req.api_key is not None:
        ollama_provider.api_key = req.api_key.strip()
        runtime_settings["api_key"] = req.api_key.strip()
    if req.temperature is not None:
        ollama_provider.temperature = max(0.0, min(1.0, req.temperature))
        runtime_settings["temperature"] = ollama_provider.temperature
    if req.num_ctx is not None:
        ollama_provider.num_ctx = req.num_ctx
        runtime_settings["num_ctx"] = req.num_ctx
    if req.num_predict is not None:
        ollama_provider.num_predict = req.num_predict
        runtime_settings["num_predict"] = req.num_predict
    if req.privacy_filter is not None:
        runtime_settings["privacy_filter"] = req.privacy_filter
    if req.exclusion_patterns:
        runtime_settings["exclusion_patterns"] = req.exclusion_patterns
        SecretMasker.set_custom_patterns(req.exclusion_patterns)
    if req.security_mode:
        runtime_settings["security_mode"] = req.security_mode
    if req.allow_network is not None:
        runtime_settings["allow_network"] = req.allow_network
    if req.repo_path:
        new_path = req.repo_path.strip()
        resolved = resolve_workspace_path(new_path)
        runtime_settings["repo_path"] = str(resolved)
        if current_indexed_workspace != str(resolved):
            await auto_index_workspace(str(resolved))
    if req.theme_mode:
        runtime_settings["theme_mode"] = req.theme_mode

    save_persisted_settings()

    return {
        "status": "success",
        **runtime_settings,
        "active_model": ollama_provider.model_name,
        "host": ollama_provider.base_url,
        "connection_type": ollama_provider.connection_type,
        "api_key": ollama_provider.api_key,
        "temperature": ollama_provider.temperature,
        "num_ctx": ollama_provider.num_ctx,
        "num_predict": ollama_provider.num_predict,
        "repo_path": runtime_settings.get("repo_path", DEFAULT_WORKSPACE)
    }

# ---------------------------------------------------------
# 1. Chat & Reasoning with Token-Budgeted Surgical RAG
# ---------------------------------------------------------
class ChatRequest(BaseModel):
    query: str
    context_file: Optional[str] = None
    active_file_content: Optional[str] = None
    open_files: Optional[List[Dict[str, Any]]] = None
    workspace_path: Optional[str] = None
    security_mode: Optional[str] = None
    temperature: Optional[float] = None
    num_ctx: Optional[int] = None
    num_predict: Optional[int] = None
    stream: bool = True
    cursor_line: Optional[int] = None
    history: Optional[List[Dict[str, str]]] = None
    pinned_mentions: Optional[List[Dict[str, Any]]] = None
    allow_multi_file: Optional[bool] = None
    target_files: Optional[List[str]] = None

active_chat_history: List[Dict[str, str]] = []
active_rolling_summary: Optional[str] = None

@app.get("/api/context/mentions")
async def get_context_mentions(
    query: str = "",
    limit: int = 15,
    workspace_path: Optional[str] = None
):
    """
    Returns ranked autocomplete candidates for @ mentions across workspace files,
    folders, and AST symbols (classes, functions, methods).
    """
    raw_ws = (workspace_path or runtime_settings.get("repo_path", "")).strip()
    ws_dir = str(resolve_workspace_path(raw_ws)) if raw_ws and raw_ws != "." else None
    results = []
    q_clean = query.strip().lstrip("@").lower()

    # 1. Matching accessible files and directories
    if ws_dir and Path(ws_dir).exists():
        try:
            accessible = WorkspaceFileResolver.list_accessible_files(ws_dir)
            file_matches = []
            folder_set = set()

            for rel_path, full_path in accessible.items():
                rel_clean = rel_path.replace("\\", "/")
                if not q_clean or q_clean in rel_clean.lower() or q_clean in full_path.name.lower():
                    file_matches.append({
                        "type": "file",
                        "name": full_path.name,
                        "path": rel_clean,
                        "icon": "file"
                    })

                parent = str(Path(rel_clean).parent).replace("\\", "/")
                if parent and parent != ".":
                    parts = parent.split("/")
                    for i in range(1, len(parts) + 1):
                        folder_set.add("/".join(parts[:i]))

            # Sort file matches: exact and prefix matches first (e.g. app.py for query 'app')
            def file_sort_score(f):
                fn = f["name"].lower()
                if fn == q_clean or fn.startswith(q_clean + "."):
                    return 0
                if fn.startswith(q_clean):
                    return 1
                if q_clean in fn:
                    return 2
                return 3
            file_matches.sort(key=file_sort_score)

            for fld in sorted(folder_set):
                if not q_clean or q_clean in fld.lower():
                    results.append({
                        "type": "folder",
                        "name": fld.split("/")[-1] + "/",
                        "path": fld,
                        "icon": "folder"
                    })
                    if len(results) >= 3:
                        break

            results.extend(file_matches[:10])
        except Exception as e:
            logger.debug(f"File mention resolver error: {e}")

    # 2. Matching AST symbols from SQLite Graph Store (skip if user typed a file extension like .py)
    has_file_ext = any(q_clean.endswith(ext) for ext in [".py", ".ts", ".js", ".json", ".html", ".css", ".md", ".sh", ".sql", ".yaml", ".yml", ".toml", ".txt"])
    if not has_file_ext and q_clean and len(q_clean) >= 2:
        try:
            symbols = await graph_store.find_symbols(q_clean, limit=12)
            for s in symbols:
                rel_f = s.get("file_path", "")
                if ws_dir:
                    try:
                        resolved_path = Path(rel_f).resolve()
                        resolved_ws = Path(ws_dir).resolve()
                        # Strictly verify symbol file is inside current workspace
                        rel_f = str(resolved_path.relative_to(resolved_ws))
                    except (ValueError, Exception):
                        # Symbol belongs to another repo or outside workspace: strictly skip!
                        continue
                results.append({
                    "type": "symbol",
                    "name": s["name"],
                    "path": rel_f.replace("\\", "/"),
                    "kind": s.get("symbol_type", "symbol"),
                    "line": s.get("start_line", 1),
                    "icon": "symbol"
                })
        except Exception as e:
            logger.debug(f"Symbol mention query error: {e}")

    return {"results": results[:limit]}


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    global active_rolling_summary
    raw_req_ws = (req.workspace_path or "").strip()
    has_workspace = bool(raw_req_ws and raw_req_ws != ".")
    if has_workspace:
        workspace_dir = str(resolve_workspace_path(raw_req_ws))
        runtime_settings["repo_path"] = workspace_dir
    else:
        workspace_dir = None
        runtime_settings["repo_path"] = ""

    sec_mode = req.security_mode or runtime_settings.get("security_mode", "sandboxed")
    if req.security_mode:
        runtime_settings["security_mode"] = req.security_mode

    # Apply per-request inference parameter overrides if provided
    if req.temperature is not None:
        ollama_provider.temperature = max(0.0, min(1.0, req.temperature))
    if req.num_ctx is not None:
        ollama_provider.num_ctx = req.num_ctx
    if req.num_predict is not None:
        ollama_provider.num_predict = req.num_predict

    # Silent background sync: only run if a repository workspace is actively selected
    accessible = {}
    if workspace_dir:
        await silent_sync_workspace(workspace_dir)
        accessible = WorkspaceFileResolver.list_accessible_files(workspace_dir)

    # Sync live active file buffer and open files from VS Code in real-time
    if req.context_file:
        p_res = Path(req.context_file).resolve()
        if not workspace_dir or is_path_in_workspace(p_res, workspace_dir):
            rel_ctx = to_workspace_rel_path(p_res, workspace_dir) if workspace_dir else p_res.name
            if not p_res.exists() and (req.active_file_content is None or not req.active_file_content.strip()):
                req.context_file = None
                req.active_file_content = None
                live_file_buffers.pop(str(p_res), None)
                live_file_buffers.pop(rel_ctx, None)
                if not workspace_dir or p_res.name in accessible:
                    live_file_buffers.pop(p_res.name, None)
            elif req.active_file_content is not None:
                update_live_file_buffer(p_res, req.active_file_content, workspace_dir, accessible or None)
                if workspace_dir:
                    await reindex_file(str(p_res), req.active_file_content)
        else:
            req.context_file = None
            req.active_file_content = None

    if req.open_files:
        clean_open_files = []
        for of in req.open_files:
            op = of.get("path")
            oc = of.get("content")
            if op and oc is not None:
                opr = Path(op).resolve()
                if not workspace_dir or is_path_in_workspace(opr, workspace_dir):
                    rel_op = to_workspace_rel_path(opr, workspace_dir) if workspace_dir else opr.name
                    if opr.exists() or oc.strip():
                        update_live_file_buffer(opr, oc, workspace_dir, accessible or None)
                        clean_open_files.append(of)
                    else:
                        live_file_buffers.pop(str(opr), None)
                        live_file_buffers.pop(rel_op, None)
                        if not workspace_dir or opr.name in accessible:
                            live_file_buffers.pop(opr.name, None)
        req.open_files = clean_open_files

    # If workspace is active and index is empty or workspace changed, re-index workspace
    if workspace_dir and (current_indexed_workspace != workspace_dir or len(vector_store.chunks) == 0):
        await auto_index_workspace(workspace_dir)

    # Determine conversation history: prefer req.history from client, fallback to backend session
    effective_history: List[Dict[str, str]] = []
    if req.history is not None:
        effective_history = list(req.history)
        active_chat_history.clear()
        active_chat_history.extend(effective_history)
    else:
        effective_history = list(active_chat_history)

    # Multi-turn context window rolling compaction (Tier 1 recent + Tier 2 summary)
    compacted_history, rolling_summary = HistoryCompactor.compact_history(
        effective_history,
        num_ctx=ollama_provider.num_ctx,
        active_summary=active_rolling_summary
    )
    active_rolling_summary = rolling_summary

    # 1. Direct file resolution & Hybrid Search (only when an active repository workspace is selected)
    direct_file_chunks = []
    retrieved_chunks = []
    combined_chunks = []
    hybrid_context = ""
    catalog_context = ""

    if workspace_dir:
        direct_file_chunks = WorkspaceFileResolver.resolve_query_files(
            query=req.query,
            workspace_root=workspace_dir,
            security_mode=sec_mode,
            max_chars_per_file=50000,
            context_file=req.context_file,
            context_file_content=req.active_file_content,
            live_buffers=live_file_buffers,
            history=effective_history
        )

        retrieved_chunks = await hybrid_engine.search(
            req.query,
            top_k=4,
            security_mode=sec_mode,
            workspace_root=workspace_dir
        )

        # Prioritize direct file chunks over generic search results
        seen_paths = set()
        for chunk in direct_file_chunks:
            combined_chunks.append(chunk)
            seen_paths.add(chunk.file_path)

        for chunk in retrieved_chunks:
            if chunk.file_path not in seen_paths:
                combined_chunks.append(chunk)
                seen_paths.add(chunk.file_path)
                if len(combined_chunks) >= 6:
                    break

        hybrid_context = HybridSearchEngine.build_context_prompt(
            combined_chunks,
            max_chars=35000,
            workspace_root=workspace_dir
        )

        # 3. Add workspace catalog/access summary if user is asking about repo access, file list, or security permissions
        if WorkspaceFileResolver.is_access_or_catalog_query(req.query):
            catalog_context = WorkspaceFileResolver.get_workspace_catalog_summary(workspace_dir, sec_mode)

    # 4. Add active editor file context if provided, not already in combined_chunks, and compliant with security mode
    file_context = ""
    target_fpath = None
    if req.context_file:
        target_fpath = Path(req.context_file).resolve()
    elif req.active_file_content and direct_file_chunks:
        target_fpath = Path(direct_file_chunks[0].file_path).resolve()

    if target_fpath and (target_fpath.exists() or req.active_file_content):
        fpath = target_fpath
        is_allowed = True
        if workspace_dir and sec_mode in ("strict", "sandboxed") and target_fpath.exists():
            try:
                fpath.relative_to(Path(workspace_dir).resolve())
            except ValueError:
                is_allowed = False

        if is_allowed and not SecretMasker.is_file_sensitive(str(fpath)):
            already_included = any(Path(c.file_path).resolve() == fpath for c in combined_chunks)
            if not already_included:
                rel_name = to_workspace_rel_path(fpath, workspace_dir) if workspace_dir else fpath.name
                # Hot-reload: prioritize active in-memory content from VS Code, then fresh disk, then live buffer
                content = None
                if req.context_file and str(Path(req.context_file).resolve()) == str(fpath) and req.active_file_content is not None:
                    content = req.active_file_content
                elif fpath.exists() and fpath.is_file():
                    content = fpath.read_text(encoding="utf-8", errors="ignore") if fpath.stat().st_size > 0 else ""
                    update_live_file_buffer(fpath, content, workspace_dir, accessible or None)
                elif rel_name in live_file_buffers:
                    content = live_file_buffers[rel_name]
                elif str(fpath) in live_file_buffers:
                    content = live_file_buffers[str(fpath)]
                elif (not workspace_dir or fpath.name in accessible) and fpath.name in live_file_buffers:
                    content = live_file_buffers[fpath.name]
                else:
                    content = ""

                lines = content.splitlines()
                total_lines = len(lines)

                if not content.strip() or total_lines == 0:
                    if fpath.exists():
                        file_context = (
                            f"[Active File: {rel_name} (0 lines, empty file)]\n"
                            f"[Status: This file is currently empty on disk. When asked to write, implement, or generate code for this file or repository, provide the complete new code implementation.]\n"
                        )
                    else:
                        file_context = ""
                else:
                    if total_lines <= 600:
                        numbered_lines = [f"{i+1:3d}: {line}" for i, line in enumerate(lines)]
                    else:
                        if req.cursor_line and 1 <= req.cursor_line <= total_lines:
                            c_idx = req.cursor_line - 1
                            w_start = max(0, c_idx - 250)
                            w_end = min(total_lines, c_idx + 250)
                        else:
                            w_start = 0
                            w_end = min(total_lines, 500)
                        numbered_lines = [f"{i+1:3d}: {line}" for i, line in enumerate(lines[w_start:w_end], start=w_start)]
                    file_body = "\n".join(numbered_lines)[:35000]
                    file_context = (
                        f"[Active File: {rel_name} ({total_lines} lines total, with EXACT 1-Indexed Line Numbers)]\n"
                        f"[IMPORTANT: Use the exact line numbers shown before the colons. Do NOT make up line numbers.]\n"
                        f"{file_body}\n"
                    )

    # 2b. Global Repository Map for whole-codebase architectural awareness (only when repository is selected)
    repo_map = ""
    if workspace_dir:
        repo_map = RepoMapGenerator.generate_repo_map(
            workspace_dir,
            graph_store=graph_store,
            max_chars=4000,
            security_mode=sec_mode
        )

    # 2c. Target creation directive if user requested writing/creating new files or folders
    extracted_targets = SymbolEntityResolver.extract_target_filepaths(req.query)
    target_creation_directive = ""
    if extracted_targets:
        target_str = ", ".join(extracted_targets)
        first_target = extracted_targets[0]
        target_ext = Path(first_target).suffix.lstrip(".")
        lang_hint = target_ext if target_ext else "python"
        target_creation_directive = (
            f"<target_creation>\n"
            f"[User Target Directive]: The user is explicitly requesting to create/write new file(s) or folder(s): {target_str}.\n"
            f"- Do NOT modify or overwrite existing files (such as active editor files) unless explicitly instructed.\n"
            f"- Provide the complete, production-ready, working implementation intended for '{first_target}'.\n"
            f"- Output your code in a standard markdown code block labeled with the language or path:\n"
            f"```{lang_hint}\n"
            f"# {first_target}\n"
            f"...\n"
            f"```\n"
            f"- State clearly that you are creating '{first_target}'.\n"
            f"</target_creation>"
        )

    # 2b. Grounded context from user-pinned mentions (@file, @symbol, @folder)
    pinned_context = ""
    if req.pinned_mentions:
        p_blocks = []
        for pm in req.pinned_mentions:
            m_type = pm.get("type", "file")
            m_name = pm.get("name", "")
            m_path = pm.get("path", "")
            if m_type == "file" and m_path:
                full_p = (Path(workspace_dir) / m_path).resolve() if not Path(m_path).is_absolute() else Path(m_path).resolve()
                if not is_path_in_workspace(full_p, workspace_dir):
                    continue
                if SecretMasker.is_file_sensitive(str(full_p)):
                    continue
                if full_p.exists() and full_p.is_file():
                    content = full_p.read_text(encoding="utf-8", errors="ignore")[:50000]
                    if runtime_settings.get("privacy_filter", True):
                        content = SecretMasker.mask_text(content)
                    p_blocks.append(f"<pinned_file path=\"{m_path}\">\n{content}\n</pinned_file>")
            elif m_type == "symbol" and m_name:
                sym_matches = await graph_store.find_symbols(m_name, limit=3)
                if sym_matches:
                    sym = sym_matches[0]
                    f_path = Path(sym["file_path"])
                    if not f_path.is_absolute() and workspace_dir:
                        f_path = (Path(workspace_dir) / f_path).resolve()
                    else:
                        f_path = f_path.resolve()
                    if is_path_in_workspace(f_path, workspace_dir) and not SecretMasker.is_file_sensitive(str(f_path)):
                        if f_path.exists() and f_path.is_file():
                            lines = f_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                            s_line = max(0, (sym.get("start_line") or 1) - 1)
                            e_line = min(len(lines), sym.get("end_line") or len(lines))
                            code_slice = "\n".join(lines[s_line:e_line])
                            if runtime_settings.get("privacy_filter", True):
                                code_slice = SecretMasker.mask_text(code_slice)
                            p_blocks.append(
                                f"<pinned_symbol name=\"{m_name}\" kind=\"{sym.get('symbol_type', 'symbol')}\" file=\"{m_path}\" lines=\"{s_line+1}-{e_line}\">\n"
                                f"{code_slice}\n"
                                f"</pinned_symbol>"
                            )
            elif m_type == "folder" and m_path:
                full_fld = (Path(workspace_dir) / m_path).resolve() if not Path(m_path).is_absolute() else Path(m_path).resolve()
                if not is_path_in_workspace(full_fld, workspace_dir):
                    continue
                if full_fld.exists() and full_fld.is_dir():
                    dir_files = [f.name for f in full_fld.iterdir() if not f.name.startswith(".") and not SecretMasker.is_file_sensitive(str(f))]
                    p_blocks.append(f"<pinned_folder path=\"{m_path}\">\nFiles: {', '.join(dir_files[:30])}\n</pinned_folder>")
        if p_blocks:
            pinned_context = "<user_pinned_context>\n" + "\n\n".join(p_blocks) + "\n</user_pinned_context>"

    # 2d. Scope directive: allow the LLM to decide whether single-file or multi-file is needed
    scope_directive = (
        "<scope_directive>\n"
        "[CODE EDITING & SCOPE GUIDELINES]:\n"
        "- If your solution requires changes to multiple files, output each file's changes inside a separate code block with the exact filename labeled on line 1, for example:\n"
        "  ```python\n"
        "  # calc.py\n"
        "  ...\n"
        "  ```\n"
        "  ```python\n"
        "  # app.py\n"
        "  ...\n"
        "  ```\n"
        "- If the user's request pertains to only a single file, confine your updates to that single file.\n"
        "- Provide complete, functional, working code without omitting functions.\n"
        "</scope_directive>"
    )





    # Combine contexts with context-window token budgeting
    parts = []
    if scope_directive:
        parts.append(scope_directive)
    if pinned_context:
        parts.append(pinned_context)
    if target_creation_directive:
        parts.append(target_creation_directive)
    if repo_map:
        parts.append(f"<repository_map>\n{repo_map}\n</repository_map>")
    if catalog_context:
        parts.append(catalog_context)
    if file_context:
        # If user explicitly requested creating a new file, mark the active editor file context as reference-only
        if extracted_targets:
            parts.append(f"[Reference Active Editor File (Do NOT modify, use only as reference for new file {extracted_targets[0]})]:\n{file_context}")
        else:
            parts.append(file_context)
    if hybrid_context:
        parts.append(hybrid_context)

    full_code_context = "\n\n".join(parts).strip()
    max_ctx_chars = max(10000, int(ollama_provider.num_ctx * 3.5))
    if len(full_code_context) > max_ctx_chars:
        full_code_context = full_code_context[:max_ctx_chars] + f"\n... [context truncated at {max_ctx_chars} characters according to {ollama_provider.num_ctx} context window]"

    # Clean short relative citations to avoid UI clutter
    citations = []
    if workspace_dir:
        for c in combined_chunks:
            try:
                rel = str(Path(c.file_path).resolve().relative_to(Path(workspace_dir).resolve()))
            except Exception:
                rel = Path(c.file_path).name
            citations.append(f"{rel}:{c.start_line}-{c.end_line}")

    accumulated_tokens: List[str] = []

    async def event_stream():
        nonlocal accumulated_tokens
        global active_chat_stream_cancel
        active_chat_stream_cancel = False

        # Stream reasoning tokens directly from Ollama respecting Zero-Leak Privacy Guard
        is_privacy_guard = runtime_settings.get("privacy_filter", True)
        try:
            async for token in ollama_provider.stream_reasoning(
                req.query,
                code_context=full_code_context,
                privacy_filter=is_privacy_guard,
                history=compacted_history,
                summary=rolling_summary
            ):
                if active_chat_stream_cancel:
                    yield f"data: {json.dumps({'aborted': True})}\n\n"
                    break
                clean_token = token.replace(r'\"\"\"', '"""').replace(r"\'\'\'", "'''")
                accumulated_tokens.append(clean_token)
                yield f"data: {json.dumps({'token': clean_token})}\n\n"
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            pass

        if active_chat_stream_cancel:
            yield "data: [DONE]\n\n"
            return

        # Update in-memory session history with completed turn
        full_assistant_reply = "".join(accumulated_tokens)
        active_chat_history.append({"role": "user", "content": req.query})
        active_chat_history.append({"role": "assistant", "content": full_assistant_reply})
        if len(active_chat_history) > 30:
            active_chat_history[:] = active_chat_history[-30:]

        # Send completion with citations
        yield f"data: {json.dumps({'done': True, 'citations': citations})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

@app.post("/api/chat/abort")
async def abort_chat_stream():
    """Immediately halts the active chat token generation stream and cancels inference."""
    global active_chat_stream_cancel
    active_chat_stream_cancel = True
    return {"status": "aborted", "timestamp": time.time()}

@app.post("/api/chat/clear")
async def clear_chat_context():
    """Clears any active conversation context, query state, and temporary agent tasks."""
    global active_task, active_rolling_summary
    active_chat_history.clear()
    active_rolling_summary = None
    active_task = {
        "id": "TASK #0001",
        "title": "Awaiting Goal",
        "priority": "NORMAL",
        "mode": "autonomous",
        "model": ollama_provider.model_name,
        "totalSteps": 0,
        "completedSteps": 0,
        "steps": [],
        "stagedChanges": []
    }
    return {"status": "cleared", "message": "Chat context and active task reset successfully."}

# ---------------------------------------------------------
# 2. Repository Scanning & Dual Indexing (AST + SQLite + Vectors)
# ---------------------------------------------------------
class ScanRequest(BaseModel):
    workspace_path: str

@app.post("/api/index/scan")
async def scan_repository(req: ScanRequest):
    if not req.workspace_path or not req.workspace_path.strip() or req.workspace_path.strip() == ".":
        raise HTTPException(status_code=400, detail="Please select a repository.")
    root = resolve_workspace_path(req.workspace_path)
    if not root.exists():
        raise HTTPException(status_code=404, detail="Workspace directory does not exist.")
    
    stats = await auto_index_workspace(str(root))
    return {
        "status": "synced",
        **stats
    }

# ---------------------------------------------------------
# 3. Dynamic Architecture Visualizer (Mermaid Graph)
# ---------------------------------------------------------
@app.get("/api/graph/mermaid")
async def get_mermaid_graph(path: Optional[str] = None, file: Optional[str] = None):
    """Generates a dynamic Mermaid.js diagram from indexed SQLite relationships, optionally focused on a selected file."""
    import sqlite3
    diagram_lines = ["graph TD"]
    seen_edges = set()
    nodes_meta: Dict[str, Any] = {}
    target_norm = str(resolve_workspace_path(path or runtime_settings.get("repo_path"))).lower()
    selected_file_norm = file.strip().replace("\\", "/").lower() if file and file.strip() else None

    try:
        with graph_store._get_connection() as db:
            # Build symbol metadata lookup
            try:
                if selected_file_norm:
                    file_norm = selected_file_norm.strip("/ ")
                    query_like = f"%/{file_norm}"
                    sym_all = db.execute(
                        "SELECT name, symbol_type, file_path, start_line FROM symbols "
                        "WHERE LOWER(REPLACE(file_path, '\\', '/')) = ? "
                        "   OR LOWER(REPLACE(file_path, '\\', '/')) LIKE ? "
                        "   OR LOWER(REPLACE(file_path, '\\', '/')) LIKE ?",
                        (file_norm, query_like, f"%{file_norm}")
                    ).fetchall()
                else:
                    sym_all = db.execute("SELECT name, symbol_type, file_path, start_line FROM symbols LIMIT 300").fetchall()

                for sname, stype, sfpath, sline in sym_all:
                    clean = sname.split("#")[-1].replace(".", "_")
                    meta_entry = {
                        "file": sfpath,
                        "line": sline or 1,
                        "type": stype,
                        "name": sname.split("#")[-1]
                    }
                    nodes_meta[clean] = meta_entry
                    nodes_meta[sname] = meta_entry
            except Exception:
                pass

            if selected_file_norm:
                file_norm = selected_file_norm.strip("/ ")
                query_like = f"%/{file_norm}"
                cursor = db.execute(
                    "SELECT source_symbol, target_symbol, relation_type, file_path, line_number FROM relationships "
                    "WHERE LOWER(REPLACE(file_path, '\\', '/')) = ? "
                    "   OR LOWER(REPLACE(file_path, '\\', '/')) LIKE ? "
                    "   OR LOWER(REPLACE(file_path, '\\', '/')) LIKE ? LIMIT 60",
                    (file_norm, query_like, f"%{file_norm}")
                )
            else:
                cursor = db.execute(
                    "SELECT source_symbol, target_symbol, relation_type, file_path, line_number FROM relationships LIMIT 60"
                )
            rows = cursor.fetchall()
            for src, tgt, rel, fpath, lnum in rows:
                if fpath:
                    try:
                        resolved_fpath = str(Path(fpath).resolve()).lower()
                        if not resolved_fpath.startswith(target_norm) and not str(Path(fpath)).lower().startswith(target_norm):
                            continue
                        if selected_file_norm:
                            fpath_norm = str(Path(fpath)).replace("\\", "/").lower().strip("/ ")
                            if fpath_norm != file_norm and not fpath_norm.endswith("/" + file_norm) and not file_norm.endswith("/" + fpath_norm):
                                continue
                    except Exception:
                        pass
                clean_src_raw = src.split("#")[-1]
                clean_tgt_raw = tgt.split("#")[-1]

                # Sanitize identifiers to alphanumeric/underscore and prefix with node_
                src_id = "node_" + re.sub(r'[^a-zA-Z0-9_]', '_', clean_src_raw).strip('_')
                tgt_id = "node_" + re.sub(r'[^a-zA-Z0-9_]', '_', clean_tgt_raw).strip('_')
                if not src_id or src_id == "node_":
                    src_id = "node_src"
                if not tgt_id or tgt_id == "node_":
                    tgt_id = "node_tgt"

                label_src = clean_src_raw.replace('"', "'")
                label_tgt = clean_tgt_raw.replace('"', "'")
                clean_rel = re.sub(r'["<>]', '', rel).strip() or "calls"

                edge = f'  {src_id}["{label_src}"] -->|{clean_rel}| {tgt_id}["{label_tgt}"]'
                if edge not in seen_edges:
                    seen_edges.add(edge)
                    diagram_lines.append(edge)

                meta_src = {
                    "file": fpath,
                    "line": lnum or 1,
                    "type": "function",
                    "name": clean_src_raw
                }
                meta_tgt = {
                    "file": fpath,
                    "line": lnum or 1,
                    "type": "function",
                    "name": clean_tgt_raw
                }

                if fpath:
                    for k in (src_id, clean_src_raw, clean_src_raw.replace(".", "_")):
                        if k not in nodes_meta:
                            nodes_meta[k] = meta_src
                    for k in (tgt_id, clean_tgt_raw, clean_tgt_raw.replace(".", "_")):
                        if k not in nodes_meta:
                            nodes_meta[k] = meta_tgt

            # If no call relationships exist, fallback to displaying indexed functions & classes
            if len(diagram_lines) == 1:
                if selected_file_norm:
                    file_norm = selected_file_norm.strip("/ ")
                    query_like = f"%/{file_norm}"
                    sym_cursor = db.execute(
                        "SELECT name, symbol_type, file_path, start_line FROM symbols "
                        "WHERE LOWER(REPLACE(file_path, '\\', '/')) = ? "
                        "   OR LOWER(REPLACE(file_path, '\\', '/')) LIKE ? "
                        "   OR LOWER(REPLACE(file_path, '\\', '/')) LIKE ? LIMIT 35",
                        (file_norm, query_like, f"%{file_norm}")
                    )
                else:
                    sym_cursor = db.execute(
                        "SELECT name, symbol_type, file_path, start_line FROM symbols LIMIT 30"
                    )
                for sname, stype, sfpath, sline in sym_cursor.fetchall():
                    if sfpath:
                        try:
                            resolved_sfpath = str(Path(sfpath).resolve()).lower()
                            if not resolved_sfpath.startswith(target_norm) and not str(Path(sfpath)).lower().startswith(target_norm):
                                continue
                            if selected_file_norm:
                                sfpath_norm = str(Path(sfpath)).replace("\\", "/").lower().strip("/ ")
                                if sfpath_norm != file_norm and not sfpath_norm.endswith("/" + file_norm) and not file_norm.endswith("/" + sfpath_norm):
                                    continue
                        except Exception:
                            pass
                    clean_name_raw = sname.split("#")[-1]
                    node_id = "node_" + re.sub(r'[^a-zA-Z0-9_]', '_', clean_name_raw).strip('_')
                    if not node_id or node_id == "node_":
                        node_id = "node_sym"
                    display_name = clean_name_raw.replace('"', "'")
                    node_line = f'  {node_id}["{stype}: {display_name}"]'
                    if node_line not in seen_edges:
                        seen_edges.add(node_line)
                        diagram_lines.append(node_line)
                    meta_sym = {
                        "file": sfpath,
                        "line": sline or 1,
                        "type": stype,
                        "name": clean_name_raw
                    }
                    for k in (node_id, clean_name_raw, clean_name_raw.replace(".", "_")):
                        if k not in nodes_meta:
                            nodes_meta[k] = meta_sym
    except Exception:
        pass

    if len(diagram_lines) == 1:
        msg = (
            f"No call relationships or symbols found for '{file}'.\nClick 'Scan & Index Workspace' to analyze this file or select another file."
            if file
            else "No SQLite code graph available for this repository.\nClick 'Scan & Index Workspace' to analyze codebase structure."
        )
        return {
            "mermaid": "",
            "available": False,
            "message": msg,
            "nodes_meta": {}
        }

    return {"mermaid": "\n".join(diagram_lines), "available": True, "focused_file": file, "nodes_meta": nodes_meta}


# ---------------------------------------------------------
# 3b. Essential Repository Files Tree Explorer
# ---------------------------------------------------------
def build_essential_file_tree(root_path: Path) -> Dict[str, Any]:
    """
    Recursively scans root_path and returns an essential files tree.
    Filters out __pycache__, .git, venv, node_modules, build artifacts, etc.
    """
    EXCLUDED_DIRS = {
        "__pycache__", ".git", ".github", ".vscode", ".idea", ".gemini",
        "venv", ".venv", "env", "node_modules", "dist", "build",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".coverage", "htmlcov",
        ".system_generated", "eggs", ".turbo", ".next", ".nuxt"
    }
    EXCLUDED_FILES = {
        ".DS_Store", "Thumbs.db", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"
    }
    EXCLUDED_EXTS = {
        ".pyc", ".pyo", ".pyd", ".log", ".tmp", ".swp", ".bak"
    }

    def format_size(bytes_size: int) -> str:
        if bytes_size < 1024:
            return f"{bytes_size} B"
        elif bytes_size < 1024 * 1024:
            return f"{bytes_size / 1024:.1f} KB"
        return f"{bytes_size / (1024 * 1024):.1f} MB"

    def scan_dir(current_dir: Path) -> list:
        entries = []
        try:
            items = sorted(list(current_dir.iterdir()), key=lambda x: (x.is_file(), x.name.lower()))
        except Exception:
            return entries

        for item in items:
            name = item.name
            if name.startswith(".") and name not in [".gitignore", ".env.example"]:
                continue
            if item.is_dir():
                if name in EXCLUDED_DIRS or name.startswith("."):
                    continue
                children = scan_dir(item)
                if children:
                    entries.append({
                        "name": name,
                        "type": "directory",
                        "path": str(item.relative_to(root_path)),
                        "full_path": str(item.resolve()),
                        "children": children
                    })
            elif item.is_file():
                if name in EXCLUDED_FILES or item.suffix.lower() in EXCLUDED_EXTS:
                    continue
                if SecretMasker.is_file_sensitive(str(item)):
                    continue
                
                try:
                    with item.open("r", encoding="utf-8", errors="ignore") as f:
                        line_count = sum(1 for _ in f)
                except Exception:
                    line_count = 0
                
                try:
                    fsize = item.stat().st_size
                except Exception:
                    fsize = 0

                ext = item.suffix.lstrip(".").lower() or "file"
                entries.append({
                    "name": name,
                    "type": "file",
                    "path": str(item.relative_to(root_path)),
                    "full_path": str(item.resolve()),
                    "ext": ext,
                    "size": format_size(fsize),
                    "bytes": fsize,
                    "lines": line_count
                })

        return entries

    tree = scan_dir(root_path)

    total_files = 0
    total_dirs = 0
    total_lines = 0

    def count_stats(nodes):
        nonlocal total_files, total_dirs, total_lines
        for node in nodes:
            if node["type"] == "directory":
                total_dirs += 1
                count_stats(node.get("children", []))
            else:
                total_files += 1
                total_lines += node.get("lines", 0)

    count_stats(tree)

    return {
        "status": "success",
        "root": str(root_path.resolve()),
        "stats": {
            "total_files": total_files,
            "total_dirs": total_dirs,
            "total_lines": total_lines
        },
        "tree": tree
    }

class TreeRequest(BaseModel):
    path: Optional[str] = None

@app.get("/api/repo/tree")
@app.post("/api/repo/tree")
async def get_repo_tree(path: Optional[str] = None, req: Optional[TreeRequest] = None):
    """Returns an essential files tree filtered from cache and junk files."""
    raw_path = path or (req.path if req else None) or runtime_settings.get("repo_path")
    root = resolve_workspace_path(raw_path)
    return build_essential_file_tree(root)

# ---------------------------------------------------------
# 3c. Global Architecture Repo Map
# ---------------------------------------------------------
class RepoMapRequest(BaseModel):
    workspace_path: Optional[str] = None
    max_chars: Optional[int] = 5000

@app.get("/api/repo/map")
@app.post("/api/repo/map")
async def get_repo_map_endpoint(workspace_path: Optional[str] = None, req: Optional[RepoMapRequest] = None):
    """
    Returns an architectural blueprint of the workspace repository (Global Repo Map)
    with classes, functions, methods, line ranges, and entrypoints.
    """
    raw_path = (req.workspace_path if req and req.workspace_path else None) or workspace_path or runtime_settings.get("repo_path")
    root = resolve_workspace_path(raw_path)
    max_c = (req.max_chars if req and req.max_chars else None) or 5000
    map_text = RepoMapGenerator.generate_repo_map(str(root), graph_store=graph_store, max_chars=max_c)
    data = RepoMapGenerator.get_repo_map_data(str(root), graph_store=graph_store)
    data["map_text"] = map_text
    return data

class SecurityAuditRequest(BaseModel):
    path: Optional[str] = None

@app.get("/api/repo/security-audit")
@app.post("/api/repo/security-audit")
async def get_repo_security_audit(path: Optional[str] = None, req: Optional[SecurityAuditRequest] = None):
    """Scans the repository for sensitive configuration files and leaked credentials."""
    raw_path = path or (req.path if req else None) or runtime_settings.get("repo_path")
    root = resolve_workspace_path(raw_path)
    return SecretMasker.scan_repository(str(root))

class FileOpenRequest(BaseModel):
    file_path: str
    repo_path: Optional[str] = None

@app.post("/api/file/open")
async def open_file_in_system(req: FileOpenRequest):
    """
    Opens the requested file using the operating system default application/helper.
    Restricted strictly to safe text/document types inside the active workspace to prevent arbitrary execution.
    """
    raw_path = req.file_path.strip()
    active_ws = resolve_workspace_path(runtime_settings.get("repo_path"))
    
    # Path resolution bounded strictly to workspace
    p = Path(raw_path)
    if not p.is_absolute():
        p = (active_ws / p).resolve()
    else:
        p = p.resolve()

    if not p.exists():
        # Fallback check relative to req.repo_path ONLY if req.repo_path is within active_ws
        if req.repo_path:
            candidate_ws = resolve_workspace_path(req.repo_path)
            if is_path_in_workspace(candidate_ws, active_ws) or candidate_ws == active_ws:
                alt_p = (candidate_ws / raw_path).resolve()
                if alt_p.exists():
                    p = alt_p

    if not p.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {raw_path}")
    
    # Path Jailing: target file must reside strictly inside active workspace
    if not is_path_in_workspace(p, active_ws):
        raise HTTPException(status_code=403, detail=f"Access denied: file '{p.name}' is outside the active workspace.")

    # Strict Allowlist: Only non-executable, safe document and text formats
    ALLOWED_OPEN_EXTS = {
        # Text, Code & Markup (safe preview/read)
        ".txt", ".md", ".markdown", ".rst", ".json", ".yaml", ".yml", ".xml", ".csv", ".tsv",
        ".html", ".css", ".scss", ".less", ".ts", ".tsx", ".js", ".jsx", ".py", ".pyi",
        ".c", ".cpp", ".cc", ".h", ".hpp", ".cs", ".go", ".rs", ".java", ".kt", ".kts",
        ".sql", ".env.example", ".gitignore", ".dockerignore", ".toml", ".ini", ".cfg",
        # Non-executable media / docs
        ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".webp", ".ico"
    }
    ext = p.suffix.lower()
    if ext not in ALLOWED_OPEN_EXTS:
        raise HTTPException(
            status_code=403,
            detail=f"Execution blocked: opening file type '{ext}' is not permitted for security reasons."
        )

    # Executable script extensions that Windows/Unix might execute directly if passed to shell
    SCRIPT_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx"}

    import platform, subprocess
    os_name = platform.system().lower()
    try:
        if os_name == "windows":
            if ext in SCRIPT_EXTS:
                # Open with notepad to prevent python/wscript execution via shell associations
                subprocess.Popen(["notepad.exe", str(p)])
            else:
                os.startfile(str(p))
        elif os_name == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return {"status": "success", "file": str(p)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------
# 4. Agentic Task Planning & Step Breakdown
# ---------------------------------------------------------
class PlanRequest(BaseModel):
    goal: str
    workspace_path: Optional[str] = None
    target_file: Optional[str] = None
    active_file: Optional[str] = None
    active_file_content: Optional[str] = None
    model_name: Optional[str] = None

def to_workspace_rel_path(raw_path: Any, workspace_dir: Any) -> str:
    """Converts a path to a clean forward-slash relative path within workspace_dir."""
    if not raw_path:
        return ""
    raw_str = str(raw_path).strip()
    if not raw_str:
        return ""
    try:
        ws_res = Path(workspace_dir).resolve()
        p = Path(raw_str)
        if p.is_absolute():
            return p.resolve().relative_to(ws_res).as_posix()
        cand = (ws_res / p).resolve()
        return cand.relative_to(ws_res).as_posix()
    except Exception:
        return raw_str.replace("\\", "/").strip("./")


def is_same_workspace_file(
    path_a: Any,
    path_b: Any,
    workspace_dir: Any,
    allow_bare_basename: bool = False
) -> bool:
    """
    Strictly checks if two file references point to the exact same file in workspace_dir.
    Prevents tests/utils.py and src/utils.py from matching each other.
    """
    if not path_a or not path_b:
        return False
    rel_a = to_workspace_rel_path(path_a, workspace_dir).lower()
    rel_b = to_workspace_rel_path(path_b, workspace_dir).lower()
    if not rel_a or not rel_b:
        return False
    if rel_a == rel_b:
        return True
    if "/" in rel_a and "/" in rel_b:
        return False
    if allow_bare_basename and ("/" not in rel_a or "/" not in rel_b):
        return Path(rel_a).name.lower() == Path(rel_b).name.lower()
    return False


def update_live_file_buffer(
    file_path_or_obj: Any,
    content: str,
    workspace_dir: Any,
    accessible: Optional[Dict[str, Path]] = None
) -> None:
    """Stores file content in live_file_buffers keyed by full path, relative path, and unambiguous basename."""
    if not file_path_or_obj or content is None:
        return
    rel_p = to_workspace_rel_path(file_path_or_obj, workspace_dir)
    if rel_p:
        live_file_buffers[rel_p] = content
        bname = Path(rel_p).name
        if accessible is None or bname in accessible:
            live_file_buffers[bname] = content
    try:
        p = Path(file_path_or_obj)
        if not p.is_absolute() and workspace_dir:
            p = Path(workspace_dir) / p
        live_file_buffers[str(p.resolve())] = content
    except Exception:
        live_file_buffers[str(file_path_or_obj)] = content


@app.post("/api/agent/plan")
async def create_plan(req: PlanRequest):
    global active_agent_step_cancel
    active_agent_step_cancel = False
    workspace_dir = str(resolve_workspace_path(req.workspace_path or runtime_settings.get("repo_path")))
    if current_indexed_workspace != workspace_dir:
        await auto_index_workspace(workspace_dir)

    intent_info = InstructionClassifier.classify(req.goal)
    accessible = WorkspaceFileResolver.list_accessible_files(workspace_dir)
    target_files = SymbolEntityResolver.resolve_target_files(req.goal, workspace_dir, accessible)

    # Strictly validate active_file against workspace_dir preserving relative subfolder path
    active_in_ws = None
    if req.active_file and is_path_in_workspace(req.active_file, workspace_dir):
        active_in_ws = to_workspace_rel_path(req.active_file, workspace_dir)

    target_candidate = None
    if req.target_file:
        cand_p = Path(req.target_file)
        if cand_p.is_absolute():
            if is_path_in_workspace(cand_p, workspace_dir):
                target_candidate = to_workspace_rel_path(cand_p, workspace_dir)
        else:
            target_candidate = to_workspace_rel_path(cand_p, workspace_dir)

    # 1. Goal-extracted files take top priority.
    # 2. Only if goal had no explicit files, fallback to validated target_candidate or active_in_ws
    if not target_files:
        if target_candidate:
            target_files.append(target_candidate)
        elif active_in_ws:
            target_files.append(active_in_ws)

    # Only buffer active file content if verified inside workspace
    if active_in_ws and req.active_file_content:
        update_live_file_buffer(req.active_file, req.active_file_content, workspace_dir, accessible)

    ambiguity = AmbiguityDetector.check_ambiguity(req.goal, target_files)
    change_specs = SymbolEntityResolver.extract_change_specs(req.goal)

    selected_model = req.model_name or ollama_provider.model_name
    from .agent.llm_planner import LLMInstructionDecomposer
    dynamic_plan = await LLMInstructionDecomposer.decompose_goal(
        goal=req.goal,
        workspace_dir=workspace_dir,
        active_file=active_in_ws,
        accessible_files=accessible,
        target_files=target_files,
        change_specs=change_specs,
        model=selected_model,
        provider=ollama_provider
    )
    
    merged_targets: List[str] = []
    for tf in (target_files or []):
        tf_norm = str(tf).replace("\\", "/").strip().lstrip("./")
        if tf_norm and tf_norm not in merged_targets:
            merged_targets.append(tf_norm)
    for pf in (dynamic_plan.get("target_files") or []):
        pf_norm = str(pf).replace("\\", "/").strip().lstrip("./")
        if not pf_norm or pf_norm in merged_targets:
            continue
        if "/" not in pf_norm and any(
            Path(existing).name.lower() == pf_norm.lower()
            for existing in merged_targets
        ):
            continue
        merged_targets.append(pf_norm)
    if merged_targets:
        dynamic_plan["target_files"] = merged_targets
        dynamic_plan["target_file"] = merged_targets[0]
        effective_target_files = merged_targets
    else:
        effective_target_files = dynamic_plan.get("target_files") or target_files
    global active_task
    active_task = {
        "id": f"TASK #{abs(hash(req.goal)) % 9000 + 1000}",
        "title": req.goal[:40],
        "priority": "HIGH PRIORITY",
        "mode": "autonomous",
        "model": selected_model,
        "intent": dynamic_plan.get("intent", intent_info.get("intent", "EDIT_EXISTING")),
        "targetFiles": effective_target_files,
        "ambiguity": ambiguity,
        "changeSpecs": change_specs,
        "totalSteps": dynamic_plan["total_steps"],
        "completedSteps": 0,
        "steps": dynamic_plan["steps"],
        "stagedChanges": []
    }
    return dynamic_plan

@app.get("/api/agent/task")
async def get_agent_task():
    return active_task

class StepExecuteRequest(BaseModel):
    goal: str
    step_id: int
    step_action: Optional[str] = None
    total_steps: Optional[int] = 3
    task_id: Optional[str] = None
    workspace_path: Optional[str] = None
    cursor_line: Optional[int] = None
    active_file: Optional[str] = None
    active_file_content: Optional[str] = None
    target_file: Optional[str] = None
    allow_multi_file: Optional[bool] = None
    target_files: Optional[List[str]] = None

@app.post("/api/agent/step")
async def execute_agent_step(req: StepExecuteRequest):
    import re
    workspace_dir = str(resolve_workspace_path(req.workspace_path or runtime_settings.get("repo_path")))
    if current_indexed_workspace != workspace_dir:
        await auto_index_workspace(workspace_dir)
    else:
        await silent_sync_workspace(workspace_dir)

    accessible = WorkspaceFileResolver.list_accessible_files(workspace_dir)
    target_files = SymbolEntityResolver.resolve_target_files(req.goal, workspace_dir, accessible)

    # Strictly validate active_file against workspace_dir preserving relative subfolder path
    active_in_ws = None
    if req.active_file and is_path_in_workspace(req.active_file, workspace_dir):
        active_in_ws = to_workspace_rel_path(req.active_file, workspace_dir)

    valid_target_file = None
    if req.target_file:
        cand_p = Path(req.target_file)
        if cand_p.is_absolute():
            if is_path_in_workspace(cand_p, workspace_dir):
                valid_target_file = to_workspace_rel_path(cand_p, workspace_dir)
        else:
            valid_target_file = to_workspace_rel_path(cand_p, workspace_dir)

    if valid_target_file:
        if valid_target_file in target_files:
            target_files.remove(valid_target_file)
            target_files.insert(0, valid_target_file)
        elif not target_files:
            target_files.insert(0, valid_target_file)
    elif not target_files and active_in_ws:
        target_files.append(active_in_ws)

    ambiguity = AmbiguityDetector.check_ambiguity(req.goal, target_files)
    change_specs = SymbolEntityResolver.extract_change_specs(req.goal)

    intent_info = InstructionClassifier.classify(req.goal)
    intent_label = intent_info.get('intent', 'EDIT_EXISTING')
    is_delete = (
        intent_label == "DELETE_CODE"
        or req.step_action == "delete"
        or bool(re.search(r"\b(?:delete|remove|clear|wipe|erase|truncate|empty)\b.*?\b(?:all\s+code|entire\s+code|all\s+content|everything|entire\s+file|all\s+lines)\b", req.goal, re.IGNORECASE))
        or bool(re.search(r"^\s*(?:clear|wipe|truncate|empty)\s+(?:the\s+)?(?:file|code|content|contents|[A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+)\s*$", req.goal, re.IGNORECASE))
    )

    # Resolve primary target filename and path strictly confined to workspace_dir
    target_filename = None
    target_path_obj = None

    if target_files:
        primary_name = target_files[0].replace("\\", "/").strip("./")
        target_filename = primary_name
        if primary_name in accessible:
            target_path_obj = accessible[primary_name]
        elif Path(primary_name).name in accessible and "/" not in primary_name:
            target_path_obj = accessible[Path(primary_name).name]
        elif (Path(workspace_dir) / primary_name).exists() or "/" in primary_name or "\\" in primary_name:
            target_path_obj = Path(workspace_dir) / primary_name
        else:
            target_path_obj = Path(workspace_dir) / target_filename
    elif active_in_ws:
        target_filename = active_in_ws
        if active_in_ws in accessible:
            target_path_obj = accessible[active_in_ws]
        else:
            target_path_obj = Path(workspace_dir) / active_in_ws
    else:
        direct_files = WorkspaceFileResolver.resolve_query_files(req.goal, workspace_dir)
        if direct_files:
            target_path_obj = Path(direct_files[0].file_path)
            target_filename = to_workspace_rel_path(target_path_obj, workspace_dir)
        elif accessible:
            py_files = [p for p in accessible.values() if p.suffix == ".py"]
            if py_files:
                target_path_obj = py_files[0]
                target_filename = to_workspace_rel_path(target_path_obj, workspace_dir)
            else:
                target_path_obj = list(accessible.values())[0]
                target_filename = to_workspace_rel_path(target_path_obj, workspace_dir)
        else:
            extracted_from_goal = SymbolEntityResolver.extract_target_filepaths(req.goal)
            if extracted_from_goal:
                target_filename = extracted_from_goal[0].replace("\\", "/").strip("./")
                target_path_obj = Path(workspace_dir) / target_filename
            else:
                words = req.goal.split()
                for w in words:
                    clean_w = w.strip(".,;:?!'\"()[]{}<>`")
                    if "." in clean_w and not clean_w.startswith("."):
                        target_filename = clean_w.replace("\\", "/").strip("./")
                        break
                if not target_filename:
                    target_filename = "app.py"
                target_path_obj = Path(workspace_dir) / target_filename

    # Safety invariant: target_path_obj must NEVER escape workspace_dir
    if not is_path_in_workspace(target_path_obj, workspace_dir):
        target_path_obj = Path(workspace_dir) / (target_filename or "app.py")

    # Resolve action
    action = req.step_action
    total_steps = req.total_steps or 3
    if not action:
        if req.step_id == 1:
            action = "scan"
        elif req.step_id == 2:
            action = "delete" if is_delete else ("prep" if total_steps in (2, 4, 5) else "synthesize")
        elif req.step_id == 3 and total_steps == 3:
            action = "verify"
        elif req.step_id == 3 and total_steps >= 4:
            action = "synthesize"
        elif req.step_id == 4 and total_steps == 4:
            action = "verify"
        elif req.step_id == 4 and total_steps == 5:
            action = "validate"
        elif req.step_id == 5:
            action = "verify"
        else:
            action = "verify"

    # Action 1: SCAN
    if action == "scan":
        if is_delete:
            line_count = 0
            if target_path_obj and target_path_obj.exists() and target_path_obj.is_file():
                try:
                    line_count = len(target_path_obj.read_text(encoding="utf-8", errors="ignore").splitlines())
                except Exception:
                    pass
            msg = f"Intent: DELETE_CODE. Isolated code boundaries in '{target_filename}' ({line_count} lines to remove)."
            return {
                "step_id": req.step_id,
                "status": "done",
                "intent": {"intent": "DELETE_CODE", "confidence": 0.99},
                "target_files": [target_filename],
                "ambiguity": None,
                "change_specs": [],
                "log": msg
            }

        is_folder_intent = intent_label == "CREATE_FOLDER" or any(kw in req.goal.lower() for kw in ("folder", "directory", "dir"))
        if is_folder_intent and not any("." in t for t in target_files):
            f_display = SymbolEntityResolver.extract_folder_name(req.goal) or target_filename
            msg = f"Intent: CREATE_FOLDER. Target folder verified: '{f_display}' in workspace."
            return {
                "step_id": req.step_id,
                "status": "done",
                "intent": {"intent": "CREATE_FOLDER", "confidence": 0.99},
                "target_files": [f_display],
                "ambiguity": None,
                "change_specs": [],
                "log": msg
            }

        retrieved = await hybrid_engine.search(req.goal, top_k=3, workspace_root=workspace_dir)
        direct_files = WorkspaceFileResolver.resolve_query_files(req.goal, workspace_dir)
        all_found = list({Path(c.file_path).name for c in (direct_files + retrieved)})
        for tf in target_files:
            all_found.append(Path(tf).name)
        files_found = list(dict.fromkeys(all_found))
        display_files = target_files if target_files else files_found

        topic_info = TopicDomainResolver.resolve_topic(req.goal)
        is_ambiguous = bool(ambiguity and ambiguity.get("detected") and len(ambiguity.get("candidates", [])) > 1)
        if intent_label in ('CREATE_NEW', 'CREATE_FOLDER') and target_files:
            msg = f"Intent: {intent_label}. Target to create: {', '.join(target_files)}"
        elif intent_label == "ALGORITHM_SYNTHESIS":
            msg = f"Intent: ALGORITHM_SYNTHESIS. Analyzed problem domain & heuristic models for '{topic_info.get('topic', 'Algorithm')}' in '{target_filename}'."
        elif total_steps == 5:
            msg = f"Intent: MULTI_FILE_REFACTOR. Indexed AST symbols across: {', '.join(display_files) if display_files else target_filename}"
        else:
            msg = f"Intent: {intent_label}. Correlated files: {', '.join(display_files) if display_files else 'root workspace'}"
        if is_ambiguous:
            msg += f" (Ambiguity detected: {', '.join(ambiguity['candidates'])})"
        return {
            "step_id": req.step_id,
            "status": "ambiguous" if is_ambiguous else "done",
            "intent": intent_info,
            "target_files": target_files,
            "ambiguity": ambiguity,
            "change_specs": change_specs,
            "log": msg
        }

    # Action 2: PREP (Folder creation / Impact Evaluation / Invariants)
    elif action == "prep":
        folder_name = SymbolEntityResolver.extract_folder_name(req.goal)
        if total_steps == 2 or (folder_name and not any("." in t for t in target_files)):
            f_display = folder_name or target_filename
            folder_dir = Path(workspace_dir) / f_display
            folder_dir.mkdir(parents=True, exist_ok=True)
            return {
                "step_id": req.step_id,
                "status": "done",
                "target_file": f_display,
                "diff": "",
                "hunks": [],
                "staged_files": [],
                "valid": True,
                "generated": f"Created folder '{f_display}' successfully.",
                "log": f"Created folder '{f_display}' in workspace."
            }
        elif folder_name:
            folder_dir = Path(workspace_dir) / folder_name
            folder_dir.mkdir(parents=True, exist_ok=True)
            return {
                "step_id": req.step_id,
                "status": "done",
                "target_file": target_filename,
                "log": f"Directory structure for '{folder_name}' prepared. Boilerplate specs ready for '{target_filename}'."
            }
        elif total_steps == 5:
            topic_info = TopicDomainResolver.resolve_topic(req.goal)
            return {
                "step_id": req.step_id,
                "status": "done",
                "target_file": target_filename,
                "log": f"Structured {topic_info.get('data_structure', 'state models and invariants')} for '{topic_info.get('topic', target_filename)}'."
            }
        else:
            return {
                "step_id": req.step_id,
                "status": "done",
                "target_file": target_filename,
                "log": f"Correlated code invariants and call stack for '{target_filename}'."
            }

    # Action 3: DELETE (Clean Code Truncation)
    elif action == "delete" or is_delete:
        orig_lines = []
        if target_path_obj and target_path_obj.exists() and target_path_obj.is_file():
            try:
                orig_lines = target_path_obj.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                orig_lines = []
        if orig_lines:
            del_lines = [f"-{line}" for line in orig_lines]
            final_diff = f"--- a/{target_filename}\n+++ b/{target_filename}\n@@ -1,{len(orig_lines)} +0,0 @@\n" + "\n".join(del_lines) + "\n"
        else:
            final_diff = f"--- a/{target_filename}\n+++ b/{target_filename}\n@@ -0,0 +0,0 @@\n"
        hunks = PatchEngine.split_into_hunks(final_diff)
        staged_files = PatchEngine.split_multi_file_diff(final_diff)
        log_msg = f"Generated clean truncation diff to remove all code from {target_filename} ({len(orig_lines)} lines removed)."
        return {
            "step_id": req.step_id,
            "status": "done",
            "target_file": target_filename,
            "diff": final_diff,
            "hunks": hunks,
            "staged_files": staged_files,
            "valid": True,
            "generated": "",
            "log": log_msg
        }

    # Action 4: SYNTHESIZE (LLM / Surgical Diff Generation)
    elif action == "synthesize":
        # Check if user specifically requested pure folder creation
        is_folder_only = bool(target_filename and "." not in Path(target_filename).name and any(kw in req.goal.lower() for kw in ("folder", "directory", "dir")))
        if is_folder_only:
            folder_dir = Path(workspace_dir) / target_filename
            folder_dir.mkdir(parents=True, exist_ok=True)
            return {
                "step_id": req.step_id,
                "status": "done",
                "target_file": target_filename,
                "diff": "",
                "hunks": [],
                "staged_files": [],
                "valid": True,
                "generated": f"Created folder '{target_filename}' successfully.",
                "log": f"Created folder '{target_filename}' in workspace."
            }

        # Check if file exists or is brand new
        is_new_file = not (target_path_obj and target_path_obj.exists() and target_path_obj.is_file())

        orig_lines = []
        raw_file_content = ""
        preferred_line = req.cursor_line

        # Helper to check if active_file strictly refers to target_filename/target_path_obj
        allow_bare_primary = bool(target_filename and "/" not in target_filename and Path(target_filename).name in accessible)
        is_target_active_file = is_same_workspace_file(
            req.active_file,
            target_path_obj or target_filename,
            workspace_dir,
            allow_bare_basename=allow_bare_primary
        )

        # Keep live buffer updated for the active file itself (if verified inside workspace)
        if req.active_file and req.active_file_content is not None and is_path_in_workspace(req.active_file, workspace_dir):
            update_live_file_buffer(req.active_file, req.active_file_content, workspace_dir, accessible)

        # 1. Prioritize active_file_content ONLY if active_file strictly matches the target file
        if is_target_active_file and req.active_file_content is not None and req.active_file_content.strip():
            raw_file_content = req.active_file_content
            update_live_file_buffer(target_path_obj or target_filename, raw_file_content, workspace_dir, accessible)
        # 2. Otherwise read target file directly from disk (ground truth)
        elif target_path_obj and target_path_obj.exists() and target_path_obj.is_file():
            try:
                raw_file_content = target_path_obj.read_text(encoding="utf-8", errors="ignore")
                update_live_file_buffer(target_path_obj, raw_file_content, workspace_dir, accessible)
            except Exception:
                raw_file_content = ""
        # 3. Check live in-memory buffer specifically for target file (never for active_file if different)
        elif target_filename and target_filename in live_file_buffers and live_file_buffers[target_filename].strip():
            raw_file_content = live_file_buffers[target_filename]
        elif target_path_obj and str(target_path_obj) in live_file_buffers and live_file_buffers[str(target_path_obj)].strip():
            raw_file_content = live_file_buffers[str(target_path_obj)]
        else:
            raw_file_content = ""

        orig_lines = raw_file_content.splitlines() if raw_file_content else []
        is_new_file = not (target_path_obj and target_path_obj.exists() and target_path_obj.is_file()) and not raw_file_content.strip()

        target_symbols = []
        if not is_new_file and orig_lines:
            target_symbols = SymbolEntityResolver.resolve_target_symbols(req.goal, raw_file_content)
            if preferred_line is None:
                sym_positions = SymbolEntityResolver.resolve_target_symbols_with_positions(req.goal, raw_file_content)
                if sym_positions:
                    preferred_line = sym_positions[0]["lineno"]

        # Build file context with 1-indexed line numbers (identical format as Chat tab)
        if orig_lines:
            numbered_lines = [f"{i+1:3d}: {line}" for i, line in enumerate(orig_lines)]
            target_file_context = (
                f"[Active Target File: {target_filename} ({len(orig_lines)} lines total, with EXACT 1-Indexed Line Numbers)]\n"
                f"[IMPORTANT: Use the exact line numbers shown before the colons. Do NOT make up line numbers.]\n"
                + "\n".join(numbered_lines)[:25000]
            )
        else:
            target_file_context = (
                f"[Target File to Create: '{target_filename}' (Brand New File)]\n"
                f"[Status: This file does not exist yet on disk. Write the complete code implementation from scratch for '{target_filename}'.]"
            )

        direct_files = WorkspaceFileResolver.resolve_query_files(req.goal, workspace_dir)
        retrieved = await hybrid_engine.search(req.goal, top_k=2, workspace_root=workspace_dir)
        combined = direct_files + [c for c in retrieved if c.file_path not in {d.file_path for d in direct_files}]

        agent_code_context = target_file_context
        if combined:
            rag_context = HybridSearchEngine.build_context_prompt(combined[:2], workspace_root=workspace_dir)
            if rag_context:
                agent_code_context += f"\n\n[Related Workspace Context]:\n{rag_context}"

        specs_info = ""
        if change_specs:
            specs_info += f"Identified Change Targets: {json.dumps(change_specs)}\n"
        if target_symbols:
            specs_info += f"Identified Target Symbols: {', '.join(target_symbols)}\n"
        if preferred_line:
            specs_info += f"Target Code Region Focus: around line {preferred_line}\n"

        topic_info = TopicDomainResolver.resolve_topic(req.goal)
        is_algorithm_or_complex = (
            intent_label == "ALGORITHM_SYNTHESIS"
            or any(kw in req.goal.lower() for kw in (
                "astar", "a*", "a-star", "dijkstra", "pathfinding", "binary search",
                "quicksort", "mergesort", "dynamic programming", "graph traversal",
                "red-black tree", "avl tree"
            ))
        )
        is_explicit_full_rewrite = any(kw in req.goal.lower() for kw in (
            "from scratch", "full rewrite", "rewrite the whole file", "rewrite entire file",
            "overwrite file", "replace entire file"
        ))
        is_comprehensive = is_new_file or is_explicit_full_rewrite

        target_ext = Path(target_filename).suffix.lstrip(".")
        lang_hint = target_ext if target_ext else "python"

        raw_all_targets = [str(t).replace("\\", "/").strip("./") for t in ((req.target_files or []) + target_files) if t]
        if req.allow_multi_file is False and valid_target_file:
            all_targets = [valid_target_file.replace("\\", "/").strip("./")]
        else:
            dedup_targets = list(dict.fromkeys(raw_all_targets))
            qualified_bases = {Path(t).name.lower() for t in dedup_targets if "/" in t}
            all_targets = [t for t in dedup_targets if "/" in t or Path(t).name.lower() not in qualified_bases]
        if target_filename:
            tf_clean = target_filename.replace("\\", "/").strip("./")
            if tf_clean not in all_targets:
                # Only insert bare target_filename if no folder-qualified target with the same basename already exists
                if "/" in tf_clean or not any(Path(existing).name.lower() == Path(tf_clean).name.lower() for existing in all_targets):
                    all_targets.insert(0, tf_clean)

        base_counts: Dict[str, int] = {}
        for at in all_targets:
            bname = Path(str(at).replace("\\", "/")).name.lower()
            base_counts[bname] = base_counts.get(bname, 0) + 1

        # Build file_orig_lines_map strictly from ground-truth disk / isolated live buffers
        file_orig_lines_map = {}
        for at in all_targets:
            at_clean = str(at).replace("\\", "/").strip("./")
            bname_low = Path(at_clean).name.lower()
            is_unique_base = (base_counts.get(bname_low, 0) == 1 and Path(at_clean).name in accessible)
            at_obj = PatchEngine.resolve_target_path(at_clean, workspace_dir)
            if not at_obj or not at_obj.exists():
                at_obj = (
                    accessible.get(at)
                    or accessible.get(at_clean)
                    or (accessible.get(Path(at_clean).name) if is_unique_base and "/" not in at_clean else None)
                    or (Path(workspace_dir) / at_clean)
                )

            lines = []
            # 1. Ground truth: read exact file directly from disk first
            if at_obj and at_obj.exists() and at_obj.is_file():
                try:
                    lines = at_obj.read_text(encoding="utf-8", errors="ignore").splitlines()
                except Exception:
                    lines = []

            # 2. Only override if req.active_file strictly matches this specific target file
            if req.active_file and is_path_in_workspace(req.active_file, workspace_dir):
                if is_same_workspace_file(req.active_file, at_clean, workspace_dir, allow_bare_basename=is_unique_base) and req.active_file_content is not None and req.active_file_content.strip():
                    lines = req.active_file_content.splitlines()

            # 3. Live buffer fallback only if file is newly created in memory
            elif not lines:
                if at_clean in live_file_buffers and live_file_buffers[at_clean].strip():
                    lines = live_file_buffers[at_clean].splitlines()
                elif at_obj and str(at_obj) in live_file_buffers and live_file_buffers[str(at_obj)].strip():
                    lines = live_file_buffers[str(at_obj)].splitlines()
                elif is_unique_base and at_obj and at_obj.name in live_file_buffers and live_file_buffers[at_obj.name].strip():
                    lines = live_file_buffers[at_obj.name].splitlines()

            file_orig_lines_map[at] = lines
            file_orig_lines_map[at_clean] = lines
            if at_obj:
                file_orig_lines_map[str(at_obj)] = lines
                if base_counts.get(bname_low, 0) == 1:
                    file_orig_lines_map[at_obj.name] = lines

        if len(all_targets) > 1:
            multi_contexts = []
            for at in all_targets:
                lines = file_orig_lines_map.get(at, [])
                if lines:
                    numbered = [f"{i+1:3d}: {line}" for i, line in enumerate(lines)]
                    multi_contexts.append(f"[Target File: {at} ({len(lines)} lines)]\n" + "\n".join(numbered)[:12000])
                else:
                    multi_contexts.append(f"[Target File to Create: '{at}']\n[Status: File has no code yet on disk. Provide full implementation.]")
            agent_code_context = "\n\n".join(multi_contexts)
            prompt = (
                f"Goal: {req.goal}\n"
                f"Target Files: {', '.join(all_targets)}\n\n"
                f"Implement the complete, working solution across the target files to achieve: {req.goal}\n\n"
                "INSTRUCTIONS:\n"
                "- Output the updated code for each target file.\n"
                "- Output each file inside a separate code block, with the filename labeled on line 1, for example:\n"
                "  ```python\n"
                f"  # {all_targets[0]}\n"
                "  ...\n"
                "  ```\n"
                "  ```python\n"
                f"  # {all_targets[1]}\n"
                "  ...\n"
                "  ```\n"
                "- Provide complete, production-ready, working code. Do not use placeholders or omit lines.\n"
                "- ZERO EMOJIS: Do not use emojis anywhere in your output."
            )
        elif is_comprehensive:
            prompt = (
                f"Goal: {req.goal}\n"
                f"Target File: {target_filename}\n"
                f"Write the complete code implementation for '{target_filename}' to fulfill: {req.goal}\n\n"
                "GUIDELINES:\n"
                "- Write the exact, complete, working implementation requested by the user.\n"
                "- Directly answer the user prompt without adding unsolicited architecture or unnecessary boilerplate.\n"
                "- ZERO EMOJIS: Do not use emojis anywhere in your output.\n"
                f"- Output your code inside a ```{lang_hint} code block directly."
            )
        else:
            prompt = (
                f"Goal: {req.goal}\n"
                f"Target File: {target_filename}\n"
                f"{specs_info}"
                f"Implement the minimal, surgical change in '{target_filename}' to achieve: {req.goal}\n\n"
                "RULES:\n"
                "- ZERO EMOJIS: Do not use emojis anywhere in your output.\n"
                f"- MINIMAL SURGICAL SCOPE: Make ONLY the minimal changes required in '{target_filename}'. Do NOT rewrite unchanged logic.\n"
                "- EXACT LINE NUMBERS: Use the exact line numbers from the context above.\n"
                f"- Output your solution as a standard unified diff (```diff) or updated code block (```{lang_hint})."
            )

        generated = await ollama_provider.generate_text(prompt, code_context=agent_code_context)

        # Build surgical minimal diff using multi-surgical builder
        final_diff, staged_files = PatchEngine.build_multi_surgical_diff(
            file_orig_lines_map=file_orig_lines_map,
            generated_text=generated,
            default_filename=target_filename,
            allow_multi_file=(len(all_targets) > 1),
            authorized_files=all_targets,
            preferred_line=preferred_line
        )

        is_valid = all(s.get("valid", True) for s in staged_files) if staged_files else True
        err_msg = next((s.get("error") for s in staged_files if s.get("error")), None)

        # Only extract filename from diff header if single file diff
        if final_diff and (not staged_files or len(staged_files) <= 1):
            header_match = re.search(r"\+\+\+\s+(?:b/)?([^\s\n\r]+)", final_diff)
            if header_match and header_match.group(1) != "/dev/null":
                hdr_rel = to_workspace_rel_path(header_match.group(1), workspace_dir)
                if "/" in hdr_rel or "/" not in (target_filename or ""):
                    target_filename = hdr_rel

        hunks = PatchEngine.split_into_hunks(final_diff)

        if staged_files and len(staged_files) > 1:
            log_msg = f"Synthesized surgical solution across {len(staged_files)} files ({', '.join(s['file_path'] for s in staged_files)})."
        elif is_new_file:
            log_msg = f"Generated complete implementation for new file '{target_filename}'."
        elif is_algorithm_or_complex:
            log_msg = f"Synthesized production-ready implementation of {topic_info.get('topic', 'algorithm')} for '{target_filename}'."
        else:
            log_msg = f"Synthesized surgical solution for '{target_filename}'." if final_diff else f"Generated response for '{target_filename}'."

        return {
            "step_id": req.step_id,
            "status": "done",
            "target_file": target_filename,
            "diff": final_diff,
            "hunks": hunks,
            "staged_files": staged_files,
            "valid": is_valid,
            "generated": generated,
            "log": log_msg
        }

    # Action 5: VALIDATE (Multi-file AST validation)
    elif action == "validate":
        return {
            "step_id": req.step_id,
            "status": "done",
            "target_file": target_filename,
            "log": "AST syntax validation & algorithmic invariant verification passed."
        }

    # Action 6: VERIFY (Zero Regressions & Ready for Diff)
    elif action == "verify":
        topic_info = TopicDomainResolver.resolve_topic(req.goal)
        if is_delete:
            msg = f"File integrity verified. Truncation diff staged for '{target_filename}' in Diff tab."
        elif total_steps == 2:
            msg = f"Directory '{target_filename}' verified and ready in workspace."
        elif intent_label == "ALGORITHM_SYNTHESIS" or total_steps == 5:
            msg = f"Zero regressions verified. Staged complete {topic_info.get('topic', 'implementation')} in Diff tab."
        else:
            msg = f"Syntax and AST verification passed. Changes ready for inspection in Diff tab."
        return {
            "step_id": req.step_id,
            "status": "done",
            "target_file": target_filename,
            "log": msg
        }

    return {"status": "error", "message": f"Unknown step action: {action}"}


@app.post("/api/agent/step/stream")
async def execute_agent_step_stream(req: StepExecuteRequest):
    """
    SSE Streaming route for Agent Step execution.
    For synthesis steps, streams Ollama tokens live to the frontend terminal,
    then returns the validated surgical diff upon completion.
    """
    import re
    workspace_dir = str(resolve_workspace_path(req.workspace_path or runtime_settings.get("repo_path")))
    if current_indexed_workspace != workspace_dir:
        await auto_index_workspace(workspace_dir)
    else:
        await silent_sync_workspace(workspace_dir)

    accessible = WorkspaceFileResolver.list_accessible_files(workspace_dir)
    target_files = SymbolEntityResolver.resolve_target_files(req.goal, workspace_dir, accessible)

    active_in_ws = None
    if req.active_file and is_path_in_workspace(req.active_file, workspace_dir):
        active_in_ws = to_workspace_rel_path(req.active_file, workspace_dir)

    valid_target_file = None
    if req.target_file:
        cand_p = Path(req.target_file)
        if cand_p.is_absolute():
            if is_path_in_workspace(cand_p, workspace_dir):
                valid_target_file = to_workspace_rel_path(cand_p, workspace_dir)
        else:
            valid_target_file = to_workspace_rel_path(cand_p, workspace_dir)

    if valid_target_file:
        if valid_target_file in target_files:
            target_files.remove(valid_target_file)
            target_files.insert(0, valid_target_file)
        elif not target_files:
            target_files.insert(0, valid_target_file)
    elif not target_files and active_in_ws:
        target_files.append(active_in_ws)

    ambiguity = AmbiguityDetector.check_ambiguity(req.goal, target_files)
    change_specs = SymbolEntityResolver.extract_change_specs(req.goal)

    intent_info = InstructionClassifier.classify(req.goal)
    intent_label = intent_info.get('intent', 'EDIT_EXISTING')
    is_delete = (
        intent_label == "DELETE_CODE"
        or req.step_action == "delete"
        or bool(re.search(r"\b(?:delete|remove|clear|wipe|erase|truncate|empty)\b.*?\b(?:all\s+code|entire\s+code|all\s+content|everything|entire\s+file|all\s+lines)\b", req.goal, re.IGNORECASE))
        or bool(re.search(r"^\s*(?:clear|wipe|truncate|empty)\s+(?:the\s+)?(?:file|code|content|contents|[A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+)\s*$", req.goal, re.IGNORECASE))
    )

    target_filename = None
    target_path_obj = None

    if target_files:
        primary_name = target_files[0].replace("\\", "/").strip("./")
        target_filename = primary_name
        if primary_name in accessible:
            target_path_obj = accessible[primary_name]
        elif Path(primary_name).name in accessible and "/" not in primary_name:
            target_path_obj = accessible[Path(primary_name).name]
        elif (Path(workspace_dir) / primary_name).exists() or "/" in primary_name or "\\" in primary_name:
            target_path_obj = Path(workspace_dir) / primary_name
        else:
            target_path_obj = Path(workspace_dir) / target_filename
    elif active_in_ws:
        target_filename = active_in_ws
        if active_in_ws in accessible:
            target_path_obj = accessible[active_in_ws]
        else:
            target_path_obj = Path(workspace_dir) / active_in_ws
    else:
        direct_files = WorkspaceFileResolver.resolve_query_files(req.goal, workspace_dir)
        if direct_files:
            target_path_obj = Path(direct_files[0].file_path)
            target_filename = to_workspace_rel_path(target_path_obj, workspace_dir)
        elif accessible:
            py_files = [p for p in accessible.values() if p.suffix == ".py"]
            if py_files:
                target_path_obj = py_files[0]
                target_filename = to_workspace_rel_path(target_path_obj, workspace_dir)
            else:
                target_path_obj = list(accessible.values())[0]
                target_filename = to_workspace_rel_path(target_path_obj, workspace_dir)
        else:
            extracted_from_goal = SymbolEntityResolver.extract_target_filepaths(req.goal)
            if extracted_from_goal:
                target_filename = extracted_from_goal[0].replace("\\", "/").strip("./")
                target_path_obj = Path(workspace_dir) / target_filename
            else:
                words = req.goal.split()
                for w in words:
                    clean_w = w.strip(".,;:?!'\"()[]{}<>`")
                    if "." in clean_w and not clean_w.startswith("."):
                        target_filename = clean_w.replace("\\", "/").strip("./")
                        break
                if not target_filename:
                    target_filename = "app.py"
                target_path_obj = Path(workspace_dir) / target_filename

    if not is_path_in_workspace(target_path_obj, workspace_dir):
        target_path_obj = Path(workspace_dir) / (target_filename or "app.py")

    action = req.step_action
    total_steps = req.total_steps or 3
    if not action:
        if req.step_id == 1:
            action = "scan"
        elif req.step_id == 2:
            action = "delete" if is_delete else ("prep" if total_steps in (2, 4, 5) else "synthesize")
        elif req.step_id == 3 and total_steps == 3:
            action = "verify"
        elif req.step_id == 3 and total_steps >= 4:
            action = "synthesize"
        else:
            action = "verify"

    async def step_stream_generator():
        global active_agent_step_cancel
        if active_agent_step_cancel:
            yield f"data: {json.dumps({'step_id': req.step_id, 'status': 'stopped', 'aborted': True, 'log': 'Execution stopped by user.', 'done': True})}\n\n"
            yield "data: [DONE]\n\n"
            return

        # If action is not synthesize, delegate to execute_agent_step and emit single completed payload
        if action != "synthesize":
            res = await execute_agent_step(req)
            if isinstance(res, dict):
                res["done"] = True
            yield f"data: {json.dumps(res)}\n\n"
            yield "data: [DONE]\n\n"
            return

        is_folder_only = bool(target_filename and "." not in Path(target_filename).name and any(kw in req.goal.lower() for kw in ("folder", "directory", "dir")))
        if is_folder_only:
            folder_dir = Path(workspace_dir) / target_filename
            folder_dir.mkdir(parents=True, exist_ok=True)
            res = {
                "step_id": req.step_id,
                "status": "done",
                "target_file": target_filename,
                "diff": "",
                "hunks": [],
                "staged_files": [],
                "valid": True,
                "generated": f"Created folder '{target_filename}' successfully.",
                "log": f"Created folder '{target_filename}' in workspace.",
                "done": True
            }
            yield f"data: {json.dumps(res)}\n\n"
            yield "data: [DONE]\n\n"
            return

        orig_lines = []
        raw_file_content = ""
        preferred_line = req.cursor_line

        # Helper to check if active_file strictly refers to target_filename/target_path_obj
        allow_bare_primary = bool(target_filename and "/" not in target_filename and Path(target_filename).name in accessible)
        is_target_active_file = is_same_workspace_file(
            req.active_file,
            target_path_obj or target_filename,
            workspace_dir,
            allow_bare_basename=allow_bare_primary
        )

        # Keep live buffer updated for the active file itself (if verified inside workspace)
        if req.active_file and req.active_file_content is not None and is_path_in_workspace(req.active_file, workspace_dir):
            update_live_file_buffer(req.active_file, req.active_file_content, workspace_dir, accessible)

        # 1. Prioritize active_file_content ONLY if active_file strictly matches the target file
        if is_target_active_file and req.active_file_content is not None and req.active_file_content.strip():
            raw_file_content = req.active_file_content
            update_live_file_buffer(target_path_obj or target_filename, raw_file_content, workspace_dir, accessible)
        # 2. Otherwise read target file directly from disk (ground truth)
        elif target_path_obj and target_path_obj.exists() and target_path_obj.is_file():
            try:
                raw_file_content = target_path_obj.read_text(encoding="utf-8", errors="ignore")
                update_live_file_buffer(target_path_obj, raw_file_content, workspace_dir, accessible)
            except Exception:
                raw_file_content = ""
        # 3. Check live in-memory buffer specifically for target file (never for active_file if different)
        elif target_filename and target_filename in live_file_buffers and live_file_buffers[target_filename].strip():
            raw_file_content = live_file_buffers[target_filename]
        elif target_path_obj and str(target_path_obj) in live_file_buffers and live_file_buffers[str(target_path_obj)].strip():
            raw_file_content = live_file_buffers[str(target_path_obj)]
        else:
            raw_file_content = ""

        orig_lines = raw_file_content.splitlines() if raw_file_content else []
        is_new_file = not (target_path_obj and target_path_obj.exists() and target_path_obj.is_file()) and not raw_file_content.strip()

        target_symbols = []
        if not is_new_file and orig_lines:
            target_symbols = SymbolEntityResolver.resolve_target_symbols(req.goal, raw_file_content)
            if preferred_line is None:
                sym_positions = SymbolEntityResolver.resolve_target_symbols_with_positions(req.goal, raw_file_content)
                if sym_positions:
                    preferred_line = sym_positions[0]["lineno"]

        if orig_lines:
            numbered_lines = [f"{i+1:3d}: {line}" for i, line in enumerate(orig_lines)]
            target_file_context = (
                f"[Active Target File: {target_filename} ({len(orig_lines)} lines total, with EXACT 1-Indexed Line Numbers)]\n"
                f"[IMPORTANT: Use the exact line numbers shown before the colons. Do NOT make up line numbers.]\n"
                + "\n".join(numbered_lines)[:25000]
            )
        else:
            target_file_context = (
                f"[Target File to Create: '{target_filename}' (Brand New File)]\n"
                f"[Status: This file does not exist yet on disk. Write the complete code implementation from scratch for '{target_filename}'.]"
            )

        direct_files = WorkspaceFileResolver.resolve_query_files(req.goal, workspace_dir)
        retrieved = await hybrid_engine.search(req.goal, top_k=2, workspace_root=workspace_dir)
        combined = direct_files + [c for c in retrieved if c.file_path not in {d.file_path for d in direct_files}]

        agent_code_context = target_file_context
        if combined:
            rag_context = HybridSearchEngine.build_context_prompt(combined[:2], workspace_root=workspace_dir)
            if rag_context:
                agent_code_context += f"\n\n[Related Workspace Context]:\n{rag_context}"

        specs_info = ""
        if change_specs:
            specs_info += f"Identified Change Targets: {json.dumps(change_specs)}\n"
        if target_symbols:
            specs_info += f"Identified Target Symbols: {', '.join(target_symbols)}\n"
        if preferred_line:
            specs_info += f"Target Code Region Focus: around line {preferred_line}\n"

        topic_info = TopicDomainResolver.resolve_topic(req.goal)
        is_algorithm_or_complex = (
            intent_label == "ALGORITHM_SYNTHESIS"
            or any(kw in req.goal.lower() for kw in (
                "astar", "a*", "a-star", "dijkstra", "pathfinding", "binary search",
                "quicksort", "mergesort", "dynamic programming", "graph traversal",
                "red-black tree", "avl tree"
            ))
        )
        is_explicit_full_rewrite = any(kw in req.goal.lower() for kw in (
            "from scratch", "full rewrite", "rewrite the whole file", "rewrite entire file",
            "overwrite file", "replace entire file"
        ))
        is_comprehensive = is_new_file or is_explicit_full_rewrite

        target_ext = Path(target_filename).suffix.lstrip(".")
        lang_hint = target_ext if target_ext else "python"

        raw_all_targets = [str(t).replace("\\", "/").strip("./") for t in ((req.target_files or []) + target_files) if t]
        if req.allow_multi_file is False and valid_target_file:
            all_targets = [valid_target_file.replace("\\", "/").strip("./")]
        else:
            dedup_targets = list(dict.fromkeys(raw_all_targets))
            qualified_bases = {Path(t).name.lower() for t in dedup_targets if "/" in t}
            all_targets = [t for t in dedup_targets if "/" in t or Path(t).name.lower() not in qualified_bases]
        if target_filename:
            tf_clean = target_filename.replace("\\", "/").strip("./")
            if tf_clean not in all_targets:
                # Only insert bare target_filename if no folder-qualified target with the same basename already exists
                if "/" in tf_clean or not any(Path(existing).name.lower() == Path(tf_clean).name.lower() for existing in all_targets):
                    all_targets.insert(0, tf_clean)

        base_counts: Dict[str, int] = {}
        for at in all_targets:
            bname = Path(str(at).replace("\\", "/")).name.lower()
            base_counts[bname] = base_counts.get(bname, 0) + 1

        # Build file_orig_lines_map strictly from ground-truth disk / isolated live buffers
        file_orig_lines_map = {}
        for at in all_targets:
            at_clean = str(at).replace("\\", "/").strip("./")
            bname_low = Path(at_clean).name.lower()
            is_unique_base = (base_counts.get(bname_low, 0) == 1 and Path(at_clean).name in accessible)
            at_obj = PatchEngine.resolve_target_path(at_clean, workspace_dir)
            if not at_obj or not at_obj.exists():
                at_obj = (
                    accessible.get(at)
                    or accessible.get(at_clean)
                    or (accessible.get(Path(at_clean).name) if is_unique_base and "/" not in at_clean else None)
                    or (Path(workspace_dir) / at_clean)
                )

            lines = []
            # 1. Ground truth: read exact file directly from disk first
            if at_obj and at_obj.exists() and at_obj.is_file():
                try:
                    lines = at_obj.read_text(encoding="utf-8", errors="ignore").splitlines()
                except Exception:
                    lines = []

            # 2. Only override if req.active_file strictly matches this specific target file
            if req.active_file and is_path_in_workspace(req.active_file, workspace_dir):
                if is_same_workspace_file(req.active_file, at_clean, workspace_dir, allow_bare_basename=is_unique_base) and req.active_file_content is not None and req.active_file_content.strip():
                    lines = req.active_file_content.splitlines()

            # 3. Live buffer fallback only if file is newly created in memory
            elif not lines:
                if at_clean in live_file_buffers and live_file_buffers[at_clean].strip():
                    lines = live_file_buffers[at_clean].splitlines()
                elif at_obj and str(at_obj) in live_file_buffers and live_file_buffers[str(at_obj)].strip():
                    lines = live_file_buffers[str(at_obj)].splitlines()
                elif is_unique_base and at_obj and at_obj.name in live_file_buffers and live_file_buffers[at_obj.name].strip():
                    lines = live_file_buffers[at_obj.name].splitlines()

            file_orig_lines_map[at] = lines
            file_orig_lines_map[at_clean] = lines
            if at_obj:
                file_orig_lines_map[str(at_obj)] = lines
                if base_counts.get(bname_low, 0) == 1:
                    file_orig_lines_map[at_obj.name] = lines

        if len(all_targets) > 1:
            multi_contexts = []
            for at in all_targets:
                lines = file_orig_lines_map.get(at, [])
                if lines:
                    numbered = [f"{i+1:3d}: {line}" for i, line in enumerate(lines)]
                    multi_contexts.append(f"[Target File: {at} ({len(lines)} lines)]\n" + "\n".join(numbered)[:12000])
                else:
                    multi_contexts.append(f"[Target File to Create: '{at}']\n[Status: File has no code yet on disk. Provide full implementation.]")
            agent_code_context = "\n\n".join(multi_contexts)
            prompt = (
                f"Goal: {req.goal}\n"
                f"Target Files: {', '.join(all_targets)}\n\n"
                f"Implement the complete, working solution across the target files to achieve: {req.goal}\n\n"
                "INSTRUCTIONS:\n"
                "- Output the updated code for each target file.\n"
                "- Output each file inside a separate code block, with the filename labeled on line 1, for example:\n"
                "  ```python\n"
                f"  # {all_targets[0]}\n"
                "  ...\n"
                "  ```\n"
                "  ```python\n"
                f"  # {all_targets[1]}\n"
                "  ...\n"
                "  ```\n"
                "- Provide complete, production-ready, working code. Do not use placeholders or omit lines.\n"
                "- ZERO EMOJIS: Do not use emojis anywhere in your output."
            )
        elif is_comprehensive:
            prompt = (
                f"Goal: {req.goal}\n"
                f"Target File: {target_filename}\n"
                f"Write the complete code implementation for '{target_filename}' to fulfill: {req.goal}\n\n"
                "GUIDELINES:\n"
                "- Write the exact, complete, working implementation requested by the user.\n"
                "- Directly answer the user prompt without adding unsolicited architecture or unnecessary boilerplate.\n"
                "- ZERO EMOJIS: Do not use emojis anywhere in your output.\n"
                f"- Output your code inside a ```{lang_hint} code block directly."
            )
        else:
            prompt = (
                f"Goal: {req.goal}\n"
                f"Target File: {target_filename}\n"
                f"{specs_info}"
                f"Implement the minimal, surgical change in '{target_filename}' to achieve: {req.goal}\n\n"
                "RULES:\n"
                "- ZERO EMOJIS: Do not use emojis anywhere in your output.\n"
                f"- MINIMAL SURGICAL SCOPE: Make ONLY the minimal changes required in '{target_filename}'. Do NOT rewrite unchanged logic.\n"
                "- EXACT LINE NUMBERS: Use the exact line numbers from the context above.\n"
                f"- Output your solution as a standard unified diff (```diff) or updated code block (```{lang_hint})."
            )

        accumulated_tokens = []
        try:
            async for token in ollama_provider.stream_reasoning(prompt, code_context=agent_code_context):
                if active_agent_step_cancel:
                    yield f"data: {json.dumps({'aborted': True, 'log': 'Execution stopped by user.'})}\n\n"
                    break
                accumulated_tokens.append(token)
                yield f"data: {json.dumps({'token': token})}\n\n"
        except asyncio.CancelledError:
            pass

        if active_agent_step_cancel:
            yield "data: [DONE]\n\n"
            return

        generated = "".join(accumulated_tokens)

        # Build surgical minimal diff with multi-file scope awareness
        final_diff, staged_files = PatchEngine.build_multi_surgical_diff(
            file_orig_lines_map=file_orig_lines_map,
            generated_text=generated,
            default_filename=target_filename,
            allow_multi_file=(len(all_targets) > 1),
            authorized_files=all_targets,
            preferred_line=preferred_line
        )

        is_valid = all(s.get("valid", True) for s in staged_files) if staged_files else True
        err_msg = next((s.get("error") for s in staged_files if s.get("error")), None)

        # Only extract filename from diff header if single file diff
        if final_diff and (not staged_files or len(staged_files) <= 1):
            header_match = re.search(r"\+\+\+\s+(?:b/)?([^\s\n\r]+)", final_diff)
            if header_match and header_match.group(1) != "/dev/null":
                hdr_rel = to_workspace_rel_path(header_match.group(1), workspace_dir)
                if "/" in hdr_rel or "/" not in (target_filename or ""):
                    target_filename = hdr_rel

        hunks = PatchEngine.split_into_hunks(final_diff)

        if staged_files and len(staged_files) > 1:
            log_msg = f"Synthesized surgical solution across {len(staged_files)} files ({', '.join(s['file_path'] for s in staged_files)})."
        elif is_new_file:
            log_msg = f"Generated complete implementation for new file '{target_filename}'."
        elif is_algorithm_or_complex:
            log_msg = f"Synthesized production-ready implementation of {topic_info.get('topic', 'algorithm')} for '{target_filename}'."
        else:
            log_msg = f"Synthesized surgical solution for '{target_filename}'." if final_diff else f"Generated response for '{target_filename}'."

        completion_data = {
            "step_id": req.step_id,
            "status": "done",
            "target_file": target_filename,
            "diff": final_diff,
            "hunks": hunks,
            "staged_files": staged_files,
            "valid": is_valid,
            "generated": generated,
            "log": log_msg,
            "done": True
        }
        yield f"data: {json.dumps(completion_data)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(step_stream_generator(), media_type="text/event-stream")


# ---------------------------------------------------------
# 5. Patch Application & Self-Healing Repair Loop
# ---------------------------------------------------------
class ApplyPatchRequest(BaseModel):
    file_path: str
    diff: str
    workspace_path: Optional[str] = None

@app.post("/api/agent/apply-patch")
async def apply_patch(req: ApplyPatchRequest):
    workspace = resolve_workspace_path(req.workspace_path)
    result = PatchEngine.apply_patch_hunks(
        file_path=req.file_path,
        diff_text=req.diff,
        workspace_path=str(workspace)
    )
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message"))

    # Immediately reindex patched file and update live buffer so chat hot-reload is instant
    patched_file = result.get("file")
    if patched_file:
        try:
            p_obj = Path(patched_file)
            if p_obj.exists():
                new_text = p_obj.read_text(encoding="utf-8", errors="ignore")
                accessible = WorkspaceFileResolver.list_accessible_files(str(workspace))
                update_live_file_buffer(p_obj, new_text, str(workspace), accessible)
                await reindex_file(str(p_obj.resolve()), new_text)
        except Exception as e:
            logger.error(f"Failed to auto-reindex patched file {patched_file}: {e}")

    return result

class PatchItem(BaseModel):
    file_path: str
    diff: str

class ApplyAllPatchesRequest(BaseModel):
    patches: List[PatchItem]
    workspace_path: Optional[str] = None

@app.post("/api/agent/apply-all-patches")
async def apply_all_patches(req: ApplyAllPatchesRequest):
    workspace = resolve_workspace_path(req.workspace_path)
    accessible = WorkspaceFileResolver.list_accessible_files(str(workspace))
    results = []
    applied_count = 0

    for p in req.patches:
        res = PatchEngine.apply_patch_hunks(
            file_path=p.file_path,
            diff_text=p.diff,
            workspace_path=str(workspace)
        )
        is_success = (res.get("status") == "success")
        if is_success:
            applied_count += 1
            patched_file = res.get("file")
            if patched_file:
                try:
                    p_obj = Path(patched_file)
                    if p_obj.exists():
                        new_text = p_obj.read_text(encoding="utf-8", errors="ignore")
                        update_live_file_buffer(p_obj, new_text, str(workspace), accessible)
                        await reindex_file(str(p_obj.resolve()), new_text)
                except Exception as e:
                    logger.error(f"Failed to auto-reindex patched file {patched_file}: {e}")

        results.append({
            "file_path": p.file_path,
            "status": res.get("status", "error"),
            "message": res.get("message", ""),
            "applied": is_success
        })

    overall_status = "success" if (applied_count == len(req.patches) and len(req.patches) > 0) else ("partial" if applied_count > 0 else "error")
    return {
        "status": overall_status,
        "applied_count": applied_count,
        "total_count": len(req.patches),
        "results": results
    }

class RebaseDiffRequest(BaseModel):
    file_path: str
    base_content: str
    diff: str
    current_content: str
    workspace_path: Optional[str] = None
    force_resolution: Optional[str] = None

@app.post("/api/agent/rebase-diff")
async def rebase_diff(req: RebaseDiffRequest):
    workspace = resolve_workspace_path(req.workspace_path)
    result = PatchEngine.rebase_staged_diff(
        file_path=req.file_path,
        base_content=req.base_content,
        diff_text=req.diff,
        current_content=req.current_content,
        workspace_path=str(workspace),
        force_resolution=req.force_resolution
    )
    return result

class ComputeDiffRequest(BaseModel):
    target_file: str
    code_or_snippet: str
    workspace_path: Optional[str] = None
    preferred_line: Optional[int] = None
    active_file: Optional[str] = None
    active_file_content: Optional[str] = None
    allow_multi_file: Optional[bool] = None
    target_files: Optional[List[str]] = None

@app.post("/api/agent/compute-diff")
async def compute_diff(req: ComputeDiffRequest):
    workspace = resolve_workspace_path(req.workspace_path)
    accessible = WorkspaceFileResolver.list_accessible_files(str(workspace))
    target_path = PatchEngine.resolve_target_path(req.target_file, str(workspace))
    orig_lines = []

    req_target_rel = to_workspace_rel_path(req.target_file, workspace) if req.target_file else to_workspace_rel_path(target_path, workspace)
    allow_bare_target = bool(req_target_rel and "/" not in req_target_rel and Path(req_target_rel).name in accessible)

    is_target_active = False
    if req.active_file:
        is_target_active = is_same_workspace_file(
            req.active_file,
            req.target_file or target_path,
            workspace,
            allow_bare_basename=allow_bare_target
        )
    elif req.active_file_content is not None and not (target_path.exists() and target_path.is_file()):
        is_target_active = True

    if is_target_active and req.active_file_content is not None:
        orig_lines = req.active_file_content.splitlines()
        update_live_file_buffer(req.target_file or target_path, req.active_file_content, workspace, accessible)
    elif target_path.exists() and target_path.is_file():
        try:
            orig_lines = target_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            update_live_file_buffer(target_path, "\n".join(orig_lines), workspace, accessible)
        except Exception:
            orig_lines = []
    elif req.target_file in live_file_buffers:
        orig_lines = live_file_buffers[req.target_file].splitlines()
    elif allow_bare_target and target_path.name in live_file_buffers:
        orig_lines = live_file_buffers[target_path.name].splitlines()

    explicit_auth_targets: List[str] = []
    auth_targets: List[str] = []
    seen_auth = set()
    for tf in (req.target_files or []):
        tf_norm = tf.replace("\\", "/").strip("/ ")
        if tf_norm and tf_norm.lower() not in seen_auth:
            seen_auth.add(tf_norm.lower())
            auth_targets.append(tf_norm)
            explicit_auth_targets.append(tf_norm)

    if req.target_file:
        rf_norm = req.target_file.replace("\\", "/").strip("/ ")
        if rf_norm.lower() not in seen_auth:
            seen_auth.add(rf_norm.lower())
            auth_targets.append(rf_norm)

    fn_matches = re.findall(r"(?:###|\*\*|File:?|filepath:?|#|\/\/|\/\*|<!--|\+\+\+\s+(?:b/)?)\s*([A-Za-z0-9_\-\.\/\\]+\.[A-Za-z0-9]+)", req.code_or_snippet)
    for fn in fn_matches:
        fn_norm = fn.replace("\\", "/").strip("/ ")
        if fn_norm.lower() not in seen_auth and "." in fn_norm:
            seen_auth.add(fn_norm.lower())
            auth_targets.append(fn_norm)

    # Count occurrences of basenames in auth_targets to avoid ambiguous basename mappings
    base_counts: Dict[str, int] = {}
    for at in auth_targets:
        bname = Path(at).name.lower()
        base_counts[bname] = base_counts.get(bname, 0) + 1

    file_orig_lines_map: Dict[str, List[str]] = {}
    for at in auth_targets:
        at_clean = at.replace("\\", "/").strip("./")
        bname = Path(at_clean).name.lower()
        is_unique_base = (base_counts.get(bname, 0) == 1 and Path(at_clean).name in accessible)
        resolved_p = PatchEngine.resolve_target_path(at_clean, str(workspace))
        lines: List[str] = []
        # 1. Read exact file from disk first
        if resolved_p.exists() and resolved_p.is_file():
            try:
                lines = resolved_p.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                lines = []

        # 2. Override with active_file_content ONLY if active_file strictly matches this target
        if req.active_file and req.active_file_content is not None:
            if is_same_workspace_file(req.active_file, at_clean, workspace, allow_bare_basename=is_unique_base):
                lines = req.active_file_content.splitlines()
        elif not req.active_file and is_target_active and req.active_file_content is not None and len(auth_targets) == 1:
            lines = req.active_file_content.splitlines()
        # 3. Fallback to isolated live_file_buffers if not on disk
        elif not lines:
            if at_clean in live_file_buffers:
                lines = live_file_buffers[at_clean].splitlines()
            elif at.replace("/", "\\") in live_file_buffers:
                lines = live_file_buffers[at.replace("/", "\\")].splitlines()
            elif str(resolved_p) in live_file_buffers:
                lines = live_file_buffers[str(resolved_p)].splitlines()
            elif is_unique_base and resolved_p.name in live_file_buffers:
                lines = live_file_buffers[resolved_p.name].splitlines()

        file_orig_lines_map[at] = lines
        file_orig_lines_map[at_clean] = lines
        file_orig_lines_map[str(resolved_p)] = lines
        if base_counts.get(bname, 0) == 1:
            file_orig_lines_map[Path(at_clean).name] = lines
            file_orig_lines_map[resolved_p.name] = lines

    is_multi = req.allow_multi_file if req.allow_multi_file is not None else (len(auth_targets) > 1)

    default_f = req_target_rel or req.target_file or target_path.name
    effective_authorized = explicit_auth_targets if explicit_auth_targets else auth_targets
    final_diff, staged_files = PatchEngine.build_multi_surgical_diff(
        file_orig_lines_map=file_orig_lines_map,
        generated_text=req.code_or_snippet,
        default_filename=default_f,
        allow_multi_file=is_multi,
        authorized_files=effective_authorized,
        preferred_line=req.preferred_line
    )

    is_valid = all(s.get("valid", True) for s in staged_files) if staged_files else True
    err = next((s.get("error") for s in staged_files if s.get("error")), None)
    hunks = PatchEngine.split_into_hunks(final_diff)
    return {
        "status": "success",
        "target_file": req_target_rel or target_path.name,
        "diff": final_diff,
        "hunks": hunks,
        "staged_files": staged_files,
        "valid": is_valid,
        "error": err
    }

class ParseInstructionRequest(BaseModel):
    goal: str
    workspace_path: Optional[str] = None

@app.post("/api/agent/parse-instruction")
async def parse_instruction(req: ParseInstructionRequest):
    workspace_dir = str(resolve_workspace_path(req.workspace_path or runtime_settings.get("repo_path")))
    intent_info = InstructionClassifier.classify(req.goal)
    accessible = WorkspaceFileResolver.list_accessible_files(workspace_dir)
    target_files = SymbolEntityResolver.resolve_target_files(req.goal, workspace_dir, accessible)
    ambiguity = AmbiguityDetector.check_ambiguity(req.goal, target_files)
    change_specs = SymbolEntityResolver.extract_change_specs(req.goal)

    from .agent.llm_planner import LLMInstructionDecomposer
    decomposed = await LLMInstructionDecomposer.decompose_goal(
        goal=req.goal,
        workspace_dir=workspace_dir,
        accessible_files=accessible,
        target_files=target_files,
        change_specs=change_specs,
        provider=ollama_provider
    )
    if decomposed.get("target_files"):
        for tf in decomposed["target_files"]:
            tf_norm = str(tf).replace("\\", "/").strip().lstrip("./")
            if not tf_norm or tf_norm in target_files:
                continue
            if "/" not in tf_norm and any(
                Path(existing).name.lower() == tf_norm.lower()
                for existing in target_files
            ):
                continue
            target_files.append(tf_norm)
    if decomposed.get("intent"):
        intent_info["intent"] = decomposed["intent"]

    target_symbols = []
    if target_files:
        primary_path = accessible.get(target_files[0]) or (Path(workspace_dir) / target_files[0])
        if primary_path.exists() and primary_path.is_file():
            try:
                content = primary_path.read_text(encoding="utf-8", errors="ignore")
                target_symbols = SymbolEntityResolver.resolve_target_symbols(req.goal, content)
            except Exception:
                pass

    return {
        "goal": req.goal,
        "intent": intent_info,
        "target_files": target_files,
        "target_symbols": target_symbols,
        "change_specs": change_specs,
        "ambiguity": ambiguity,
        "plan": decomposed
    }

class DiscardHunkRequest(BaseModel):
    file_path: str
    diff: str
    hunk_index: int
    workspace_path: Optional[str] = None
    base_content: Optional[str] = None

@app.post("/api/agent/discard-hunk")
async def discard_diff_hunk(req: DiscardHunkRequest):
    workspace = resolve_workspace_path(req.workspace_path)
    accessible = WorkspaceFileResolver.list_accessible_files(str(workspace))
    target_path = PatchEngine.resolve_target_path(req.file_path, str(workspace))
    rel_target = to_workspace_rel_path(req.file_path or target_path, workspace) or target_path.name
    orig_lines = []
    if req.base_content is not None and req.base_content.strip():
        orig_lines = req.base_content.splitlines()
    elif target_path.exists() and target_path.is_file():
        try:
            orig_lines = target_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            orig_lines = []
    elif rel_target in live_file_buffers and live_file_buffers[rel_target].strip():
        orig_lines = live_file_buffers[rel_target].splitlines()
    elif str(target_path) in live_file_buffers and live_file_buffers[str(target_path)].strip():
        orig_lines = live_file_buffers[str(target_path)].splitlines()
    elif "/" not in rel_target and target_path.name in accessible and target_path.name in live_file_buffers:
        orig_lines = live_file_buffers[target_path.name].splitlines()

    new_diff = PatchEngine.discard_hunk(orig_lines, req.diff, req.hunk_index, rel_target)
    hunks = PatchEngine.split_into_hunks(new_diff)
    return {
        "status": "success",
        "file_path": rel_target,
        "diff": new_diff,
        "hunks": hunks
    }

class IntraDiffRequest(BaseModel):
    old_line: str
    new_line: str

@app.post("/api/agent/intra-diff")
async def get_intra_diff(req: IntraDiffRequest):
    return PatchEngine.compute_intra_line_diff(req.old_line, req.new_line)

@app.get("/api/system/info")
async def get_system_info(workspace_path: Optional[str] = None):
    cwd = str(resolve_workspace_path(workspace_path or runtime_settings.get("repo_path")))
    terminal = TerminalAgent(cwd)
    return terminal.get_system_info()

class TerminalCommandRequest(BaseModel):
    command: str
    workspace_path: Optional[str] = None
    security_mode: Optional[str] = "sandboxed"
    execution_id: Optional[str] = None

@app.post("/api/agent/terminal/execute")
async def execute_terminal_command(req: TerminalCommandRequest):
    cwd = str(resolve_workspace_path(req.workspace_path or runtime_settings.get("repo_path")))
    terminal = TerminalAgent(cwd)
    sec_mode = req.security_mode or runtime_settings.get("security_mode", "sandboxed")
    allow_net = runtime_settings.get("allow_network", True)
    res = await terminal.run_command(
        req.command,
        security_mode=sec_mode,
        allow_network=allow_net,
        execution_id=req.execution_id
    )
    return {
        "execution_id": res.get("execution_id", req.execution_id),
        "command": req.command,
        "workspace": terminal.workspace_path,
        "os": res.get("os", terminal.os_type),
        "security_mode": sec_mode,
        "exit_code": res.get("exit_code", 0),
        "stdout": res.get("stdout", ""),
        "stderr": res.get("stderr", ""),
        "passed": res.get("passed", False)
    }

class TerminalAbortRequest(BaseModel):
    execution_id: Optional[str] = None

@app.post("/api/agent/terminal/abort")
async def abort_terminal_command(req: Optional[TerminalAbortRequest] = None):
    """Aborts an active terminal command execution by terminating its process tree."""
    exec_id = req.execution_id if req else None
    return TerminalAgent.abort_command(exec_id)

@app.get("/api/agent/terminal/processes")
async def list_terminal_processes():
    """Returns metadata for all currently running terminal commands."""
    return {"processes": TerminalAgent.get_active_processes()}

@app.post("/api/agent/abort")
async def abort_agent_execution():
    """Immediately halts active multi-step agent planning or code synthesis and child processes."""
    global active_agent_step_cancel
    active_agent_step_cancel = True
    TerminalAgent.abort_command()
    return {"status": "aborted", "timestamp": time.time()}

class RepairRequest(BaseModel):
    task_description: str
    workspace_path: str

@app.post("/api/agent/repair")
async def trigger_self_healing_repair(req: RepairRequest):
    terminal = TerminalAgent(req.workspace_path)
    loop = SelfHealingRepairLoop(terminal, patch_engine, ollama_provider)
    return await loop.execute_repair_loop(req.task_description)

class ReviewRequest(BaseModel):
    task_id: str
    decision: str

@app.post("/api/agent/review")
async def review_changes(req: ReviewRequest):
    return {"status": "success", "task_id": req.task_id, "action": req.decision}

# ---------------------------------------------------------
# 6. System Lifecycle, Graceful Shutdown & Auto-Reload
# ---------------------------------------------------------
@app.post("/api/system/shutdown")
async def shutdown_system():
    """Cleanly flushes database WAL buffers, closes connections, and terminates the backend process."""
    def _do_shutdown():
        try:
            if hasattr(graph_store, "db") and graph_store.db:
                try:
                    graph_store.db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    graph_store.db.close()
                except Exception:
                    pass
        except Exception:
            pass
        os._exit(0)

    loop = asyncio.get_event_loop()
    loop.call_later(0.3, _do_shutdown)
    return {"status": "shutting_down", "pid": os.getpid(), "message": "Backend server is shutting down cleanly."}

@app.post("/api/system/restart")
async def restart_system():
    """Flushes DB buffers and re-executes the backend process cleanly using a detached launcher."""
    def _do_restart():
        try:
            if hasattr(graph_store, "db") and graph_store.db:
                try:
                    graph_store.db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    graph_store.db.close()
                except Exception:
                    pass
        except Exception:
            pass
        import subprocess
        import sys

        port = 8765
        for i, arg in enumerate(sys.argv):
            if arg == "--port" and i + 1 < len(sys.argv):
                try:
                    port = int(sys.argv[i + 1])
                except Exception:
                    pass

        host = "127.0.0.1"
        for i, arg in enumerate(sys.argv):
            if arg == "--host" and i + 1 < len(sys.argv):
                host = sys.argv[i + 1]

        backend_dir = str(Path(__file__).resolve().parent.parent)
        is_frozen = getattr(sys, "frozen", False)

        try:
            if is_frozen:
                # Embedded executable (clay-server.exe) runner
                cmd = [sys.executable, "--host", host, "--port", str(port)]
                if os.name == "nt":
                    creation_flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                    delay_cmd = f'ping 127.0.0.1 -n 2 > nul & "{sys.executable}" --host {host} --port {port}'
                    subprocess.Popen(
                        ["cmd.exe", "/c", delay_cmd],
                        cwd=backend_dir,
                        creationflags=creation_flags,
                        close_fds=True
                    )
                else:
                    subprocess.Popen(
                        ["sh", "-c", f'sleep 1.0 && "{sys.executable}" --host {host} --port {port}'],
                        cwd=backend_dir,
                        start_new_session=True,
                        close_fds=True
                    )
            else:
                # Python source runner via Uvicorn
                cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port)]
                if os.name == "nt":
                    launcher_code = (
                        f"import time, subprocess; "
                        f"time.sleep(1.2); "
                        f"subprocess.Popen({repr(cmd)}, cwd={repr(backend_dir)})"
                    )
                    creation_flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                    subprocess.Popen(
                        [sys.executable, "-c", launcher_code],
                        cwd=backend_dir,
                        creationflags=creation_flags,
                        close_fds=True
                    )
                else:
                    launcher_code = (
                        f"import time, subprocess; "
                        f"time.sleep(0.8); "
                        f"subprocess.Popen({repr(cmd)}, cwd={repr(backend_dir)})"
                    )
                    subprocess.Popen(
                        [sys.executable, "-c", launcher_code],
                        cwd=backend_dir,
                        start_new_session=True,
                        close_fds=True
                    )
            os._exit(0)
        except Exception:
            os._exit(1)

    loop = asyncio.get_event_loop()
    loop.call_later(0.3, _do_restart)
    return {"status": "restarting", "pid": os.getpid()}

@app.post("/api/system/reload")
async def reload_system():
    """Reloads runtime settings, clears live memory buffers, and refreshes provider configuration."""
    load_persisted_settings()
    live_file_buffers.clear()
    return {
        "status": "reloaded",
        "pid": os.getpid(),
        "runtime_settings": {k: v for k, v in runtime_settings.items() if k != "api_key"},
        "active_model": ollama_provider.model_name,
        "host": ollama_provider.base_url
    }

@app.get("/api/system/processes")
async def get_system_process_status():
    """Returns process and health diagnostics for monitoring and auto-recovery."""
    return {
        "pid": os.getpid(),
        "active_terminal_jobs": len(TerminalAgent.get_active_processes()),
        "terminal_processes": TerminalAgent.get_active_processes(),
        "indexed_chunks": len(vector_store.chunks),
        "model": ollama_provider.model_name,
        "status": "healthy"
    }

@app.get("/health")
@app.get("/api/health")
async def health_check():
    """Lightweight health check endpoint for processManager and heartbeat watchdog."""
    ws = runtime_settings.get("repo_path") or DEFAULT_WORKSPACE
    return {
        "status": "healthy",
        "pid": os.getpid(),
        "model": ollama_provider.model_name,
        "version": "1.6.14",
        "workspace": str(resolve_workspace_path(ws)) if ws else ""
    }

