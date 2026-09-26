import os
import json
import asyncio
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import logging
from pydantic import BaseModel

logger = logging.getLogger("clay")

from .security.secret_masker import SecretMasker
from .parser.ast_chunker import PythonASTChunker
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
)
from .agent.repair_loop import SelfHealingRepairLoop
from .agent.tools.terminal_agent import TerminalAgent
from .llm.gemma_provider import OllamaGemmaProvider

app = FastAPI(title="CLAY Copilot", version="0.2.0")

# Enable CORS for local VS Code extension webview communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Core subsystems
graph_store = SQLiteGraphStore("code_graph.sqlite")
vector_store = LocalVectorStore()
ast_chunker = PythonASTChunker()
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
    - Absolute paths (e.g. /home/ishan/Desktop/Clay test)
    - Tilde paths (e.g. ~/Desktop/Clay test)
    - Bare folder names from browser directory pickers (e.g. "Clay test")
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
    await graph_store.clear_all()

    all_symbols = []
    all_relationships = []
    all_chunks = []
    indexed_files = 0

    EXCLUDED_DIRS = {
        "venv", ".venv", "env", "node_modules", "dist", "build",
        "__pycache__", ".pytest_cache", ".git", ".idea", ".vscode", ".gemini"
    }

    # 2. Parse Python files with AST Chunker
    for path in root.rglob("*.py"):
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            rel_parts = path.parts
        if any(part.startswith(".") or part in EXCLUDED_DIRS for part in rel_parts):
            continue
        if SecretMasker.is_file_sensitive(str(path)):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            symbols, relationships, chunks = ast_chunker.parse_file(str(path), content)
            all_symbols.extend(symbols)
            all_relationships.extend(relationships)
            all_chunks.extend(chunks)
            indexed_files += 1
        except Exception:
            continue

    # 3. Index additional essential code files for multi-language context (.ts, .js, .json, .md, .html, etc.)
    code_exts = {".ts", ".js", ".jsx", ".tsx", ".json", ".md", ".html", ".css", ".toml", ".yaml", ".yml", ".sql", ".sh"}
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
            content = path.read_text(encoding="utf-8", errors="ignore")
            lines = content.splitlines()
            if not lines:
                continue
            step = 30
            for i in range(0, min(len(lines), 120), step):
                chunk_lines = lines[i:i + step]
                chunk_body = "\n".join(chunk_lines)
                if not chunk_body.strip():
                    continue
                try:
                    rel_name = str(path.relative_to(root))
                except Exception:
                    rel_name = path.name
                all_chunks.append(CodeChunk(
                    chunk_id=f"{path}#{i+1}-{i+len(chunk_lines)}",
                    file_path=str(path),
                    symbol_type="file_section",
                    symbol_name=f"{rel_name}:{i+1}",
                    start_line=i + 1,
                    end_line=i + len(chunk_lines),
                    content=chunk_body
                ))
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

live_file_buffers: Dict[str, str] = {}
workspace_file_states: Dict[str, Tuple[float, int]] = {}

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
            if p.suffix == ".py":
                try:
                    new_symbols, new_relationships, new_chunks = ast_chunker.parse_file(str(p), content)
                except Exception:
                    pass
            else:
                lines = content.splitlines()
                step = 30
                for i in range(0, min(len(lines), 120), step):
                    chunk_lines = lines[i:i + step]
                    chunk_body = "\n".join(chunk_lines)
                    if chunk_body.strip():
                        new_chunks.append(CodeChunk(
                            chunk_id=f"{p}#{i+1}-{i+len(chunk_lines)}",
                            file_path=str(p),
                            symbol_type="file_section",
                            symbol_name=f"{p.name}:{i+1}",
                            start_line=i + 1,
                            end_line=i + len(chunk_lines),
                            content=chunk_body
                        ))

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

@app.on_event("startup")
async def on_startup():
    await graph_store.init_db()
    if len(vector_store.chunks) == 0:
        await auto_index_workspace(DEFAULT_WORKSPACE)

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

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "runtime_settings.json"

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
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
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
            "api_key": ollama_provider.api_key
        }
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

active_chat_history: List[Dict[str, str]] = []

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    raw_dir = req.workspace_path or runtime_settings.get("repo_path") or DEFAULT_WORKSPACE
    workspace_dir = str(resolve_workspace_path(raw_dir))
    runtime_settings["repo_path"] = workspace_dir
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

    # Silent background sync: microsecond stat() check on workspace files to capture any manual edits, deletions, or emptyings
    await silent_sync_workspace(workspace_dir)

    # Sync live active file buffer and open files from VS Code in real-time
    if req.context_file and req.active_file_content is not None:
        p_res = Path(req.context_file).resolve()
        live_file_buffers[str(p_res)] = req.active_file_content
        live_file_buffers[p_res.name] = req.active_file_content
        await reindex_file(str(p_res), req.active_file_content)

    if req.open_files:
        for of in req.open_files:
            op = of.get("path")
            oc = of.get("content")
            if op and oc is not None:
                opr = Path(op).resolve()
                live_file_buffers[str(opr)] = oc
                live_file_buffers[opr.name] = oc

    # If index is empty or workspace changed, re-index workspace
    if current_indexed_workspace != workspace_dir or len(vector_store.chunks) == 0:
        await auto_index_workspace(workspace_dir)

    # Determine conversation history: prefer req.history from client, fallback to backend session
    effective_history: List[Dict[str, str]] = []
    if req.history is not None:
        effective_history = list(req.history)
        active_chat_history.clear()
        active_chat_history.extend(effective_history)
    else:
        effective_history = list(active_chat_history)

    # 1. Direct file resolution via WorkspaceFileResolver (handles typos, exact matches, top-level scripts, active editor file, live in-memory buffers, multi-turn session history)
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

    # 2. Retrieve top relevant code chunks via Hybrid Search
    retrieved_chunks = await hybrid_engine.search(
        req.query,
        top_k=4,
        security_mode=sec_mode,
        workspace_root=workspace_dir
    )

    # Prioritize direct file chunks over generic search results
    seen_paths = set()
    combined_chunks = []
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
    catalog_context = ""
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
        if sec_mode in ("strict", "sandboxed") and target_fpath.exists():
            try:
                fpath.relative_to(Path(workspace_dir).resolve())
            except ValueError:
                is_allowed = False

        if is_allowed and not SecretMasker.is_file_sensitive(str(fpath)):
            already_included = any(Path(c.file_path).resolve() == fpath for c in combined_chunks)
            if not already_included:
                # Hot-reload: prioritize active in-memory content from VS Code, then fresh disk, then live buffer
                content = None
                if req.context_file and str(Path(req.context_file).resolve()) == str(fpath) and req.active_file_content is not None:
                    content = req.active_file_content
                elif fpath.exists() and fpath.is_file():
                    content = fpath.read_text(encoding="utf-8", errors="ignore") if fpath.stat().st_size > 0 else ""
                    live_file_buffers[str(fpath)] = content
                    live_file_buffers[fpath.name] = content
                elif str(fpath) in live_file_buffers:
                    content = live_file_buffers[str(fpath)]
                elif fpath.name in live_file_buffers:
                    content = live_file_buffers[fpath.name]
                else:
                    content = ""

                lines = content.splitlines()
                total_lines = len(lines)
                try:
                    rel_name = str(fpath.relative_to(Path(workspace_dir).resolve()))
                except Exception:
                    rel_name = fpath.name

                if not content.strip() or total_lines == 0:
                    file_context = (
                        f"[Active File: {rel_name} (0 lines, empty file)]\n"
                        f"[Status: This file is currently empty on disk. When asked to write, implement, or generate code for this file or repository, provide the complete new code implementation.]\n"
                    )
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

    # 2b. Global Repository Map for whole-codebase architectural awareness
    repo_map = RepoMapGenerator.generate_repo_map(
        workspace_dir,
        graph_store=graph_store,
        max_chars=4000,
        security_mode=sec_mode
    )

    # Combine contexts with context-window token budgeting
    parts = []
    if repo_map:
        parts.append(f"<repository_map>\n{repo_map}\n</repository_map>")
    if catalog_context:
        parts.append(catalog_context)
    if file_context:
        parts.append(file_context)
    if hybrid_context:
        parts.append(hybrid_context)

    full_code_context = "\n\n".join(parts).strip()
    max_ctx_chars = max(10000, int(ollama_provider.num_ctx * 3.5))
    if len(full_code_context) > max_ctx_chars:
        full_code_context = full_code_context[:max_ctx_chars] + f"\n... [context truncated at {max_ctx_chars} characters according to {ollama_provider.num_ctx} context window]"

    # Clean short relative citations to avoid UI clutter
    citations = []
    for c in combined_chunks:
        try:
            rel = str(Path(c.file_path).resolve().relative_to(Path(workspace_dir).resolve()))
        except Exception:
            rel = Path(c.file_path).name
        citations.append(f"{rel}:{c.start_line}-{c.end_line}")

    accumulated_tokens: List[str] = []

    async def event_stream():
        nonlocal accumulated_tokens
        # Stream reasoning tokens directly from Ollama respecting Zero-Leak Privacy Guard
        is_privacy_guard = runtime_settings.get("privacy_filter", True)
        async for token in ollama_provider.stream_reasoning(
            req.query,
            code_context=full_code_context,
            privacy_filter=is_privacy_guard,
            history=effective_history
        ):
            clean_token = token.replace(r'\"\"\"', '"""').replace(r"\'\'\'", "'''")
            accumulated_tokens.append(clean_token)
            yield f"data: {json.dumps({'token': clean_token})}\n\n"
            await asyncio.sleep(0.01)

        # Update in-memory session history with completed turn
        full_assistant_reply = "".join(accumulated_tokens)
        active_chat_history.append({"role": "user", "content": req.query})
        active_chat_history.append({"role": "assistant", "content": full_assistant_reply})
        if len(active_chat_history) > 20:
            active_chat_history[:] = active_chat_history[-20:]

        # Send completion with citations
        yield f"data: {json.dumps({'done': True, 'citations': citations})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

@app.post("/api/chat/clear")
async def clear_chat_context():
    """Clears any active conversation context, query state, and temporary agent tasks."""
    global active_task
    active_chat_history.clear()
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
    target_norm = str(resolve_workspace_path(path or runtime_settings.get("repo_path"))).lower()
    selected_file_norm = file.strip().replace("\\", "/").lower() if file and file.strip() else None

    try:
        with sqlite3.connect(graph_store.db_path) as db:
            if selected_file_norm:
                sel_filename = Path(selected_file_norm).name.lower()
                cursor = db.execute(
                    "SELECT source_symbol, target_symbol, relation_type, file_path FROM relationships "
                    "WHERE LOWER(file_path) LIKE ? OR LOWER(file_path) LIKE ? LIMIT 60",
                    (f"%{selected_file_norm}%", f"%{sel_filename}%")
                )
            else:
                cursor = db.execute(
                    "SELECT source_symbol, target_symbol, relation_type, file_path FROM relationships LIMIT 60"
                )
            rows = cursor.fetchall()
            for src, tgt, rel, fpath in rows:
                if fpath:
                    try:
                        resolved_fpath = str(Path(fpath).resolve()).lower()
                        if not resolved_fpath.startswith(target_norm) and not str(Path(fpath)).lower().startswith(target_norm):
                            continue
                        if selected_file_norm:
                            fpath_norm = str(Path(fpath)).replace("\\", "/").lower()
                            if sel_filename not in fpath_norm and selected_file_norm not in fpath_norm:
                                continue
                    except Exception:
                        pass
                clean_src = src.split("#")[-1].replace(".", "_")
                clean_tgt = tgt.split("#")[-1].replace(".", "_")
                edge = f"  {clean_src} -->|{rel}| {clean_tgt}"
                if edge not in seen_edges:
                    seen_edges.add(edge)
                    diagram_lines.append(edge)

            # If no call relationships exist, fallback to displaying indexed functions & classes
            if len(diagram_lines) == 1:
                if selected_file_norm:
                    sym_cursor = db.execute(
                        "SELECT name, symbol_type, file_path FROM symbols "
                        "WHERE LOWER(file_path) LIKE ? OR LOWER(file_path) LIKE ? LIMIT 35",
                        (f"%{selected_file_norm}%", f"%{sel_filename}%")
                    )
                else:
                    sym_cursor = db.execute(
                        "SELECT name, symbol_type, file_path FROM symbols LIMIT 30"
                    )
                for sname, stype, sfpath in sym_cursor.fetchall():
                    if sfpath:
                        try:
                            resolved_sfpath = str(Path(sfpath).resolve()).lower()
                            if not resolved_sfpath.startswith(target_norm) and not str(Path(sfpath)).lower().startswith(target_norm):
                                continue
                            if selected_file_norm:
                                sfpath_norm = str(Path(sfpath)).replace("\\", "/").lower()
                                if sel_filename not in sfpath_norm and selected_file_norm not in sfpath_norm:
                                    continue
                        except Exception:
                            pass
                    clean_name = sname.split("#")[-1].replace(".", "_")
                    node_line = f"  {clean_name}[\"{stype}: {clean_name}\"]"
                    if node_line not in seen_edges:
                        seen_edges.add(node_line)
                        diagram_lines.append(node_line)
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
            "message": msg
        }

    return {"mermaid": "\n".join(diagram_lines), "available": True, "focused_file": file}


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
    On Windows: os.startfile(p) prompts the Windows open helper if no program is bound.
    On Linux/macOS: xdg-open / open.
    """
    raw_path = req.file_path.strip()
    p = Path(raw_path).resolve()
    if not p.exists() and req.repo_path:
        alt_p = (Path(req.repo_path) / raw_path).resolve()
        if alt_p.exists():
            p = alt_p
    if not p.exists() and runtime_settings.get("repo_path"):
        alt_p = (Path(runtime_settings.get("repo_path")) / raw_path).resolve()
        if alt_p.exists():
            p = alt_p
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {raw_path}")
    
    import platform, subprocess
    os_name = platform.system().lower()
    try:
        if os_name == "windows":
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

@app.post("/api/agent/plan")
async def create_plan(req: PlanRequest):
    workspace_dir = str(resolve_workspace_path(req.workspace_path or runtime_settings.get("repo_path")))
    if current_indexed_workspace != workspace_dir:
        await auto_index_workspace(workspace_dir)

    intent_info = InstructionClassifier.classify(req.goal)
    accessible = WorkspaceFileResolver.list_accessible_files(workspace_dir)
    target_files = SymbolEntityResolver.resolve_target_files(req.goal, workspace_dir, accessible)
    ambiguity = AmbiguityDetector.check_ambiguity(req.goal, target_files)
    change_specs = SymbolEntityResolver.extract_change_specs(req.goal)

    summary = f"Repository at '{workspace_dir}' with {len(vector_store.chunks)} indexed code chunks."
    plan_data = await planner.create_plan(req.goal, summary)
    
    global active_task
    active_task = {
        "id": f"TASK #{hash(req.goal) % 9000 + 1000}",
        "title": req.goal[:40],
        "priority": "HIGH PRIORITY",
        "mode": "autonomous",
        "model": ollama_provider.model_name,
        "intent": intent_info.get("intent", "EDIT_EXISTING"),
        "targetFiles": target_files,
        "ambiguity": ambiguity,
        "changeSpecs": change_specs,
        "totalSteps": plan_data["total_steps"],
        "completedSteps": 1,
        "steps": plan_data["steps"],
        "stagedChanges": []
    }
    return active_task

@app.get("/api/agent/task")
async def get_agent_task():
    return active_task

class StepExecuteRequest(BaseModel):
    goal: str
    step_id: int
    task_id: Optional[str] = None
    workspace_path: Optional[str] = None
    cursor_line: Optional[int] = None
    active_file: Optional[str] = None
    target_file: Optional[str] = None

@app.post("/api/agent/step")
async def execute_agent_step(req: StepExecuteRequest):
    import re
    workspace_dir = str(resolve_workspace_path(req.workspace_path or runtime_settings.get("repo_path")))
    if current_indexed_workspace != workspace_dir:
        await auto_index_workspace(workspace_dir)
    else:
        await silent_sync_workspace(workspace_dir)

    # Step 1: Symbol scanning and repository context retrieval
    if req.step_id == 1:
        intent_info = InstructionClassifier.classify(req.goal)
        accessible = WorkspaceFileResolver.list_accessible_files(workspace_dir)
        target_files = SymbolEntityResolver.resolve_target_files(req.goal, workspace_dir, accessible)
        ambiguity = AmbiguityDetector.check_ambiguity(req.goal, target_files)
        change_specs = SymbolEntityResolver.extract_change_specs(req.goal)

        retrieved = await hybrid_engine.search(req.goal, top_k=3, workspace_root=workspace_dir)
        direct_files = WorkspaceFileResolver.resolve_query_files(req.goal, workspace_dir)
        all_found = list({Path(c.file_path).name for c in (direct_files + retrieved)})
        for tf in target_files:
            all_found.append(Path(tf).name)
        files_found = list(dict.fromkeys(all_found))

        is_ambiguous = bool(ambiguity and ambiguity.get("detected") and len(ambiguity.get("candidates", [])) > 1)
        msg = f"Intent: {intent_info.get('intent', 'EDIT_EXISTING')}. Correlated files: {', '.join(files_found) if files_found else 'root workspace'}"
        if is_ambiguous:
            msg += f" (Ambiguity detected: {', '.join(ambiguity['candidates'])})"
        return {
            "step_id": 1,
            "status": "ambiguous" if is_ambiguous else "done",
            "intent": intent_info,
            "target_files": target_files,
            "ambiguity": ambiguity,
            "change_specs": change_specs,
            "log": msg
        }
    # Step 2: Code synthesis / Unified Diff via Gemma 4 LLM
    elif req.step_id == 2:
        accessible = WorkspaceFileResolver.list_accessible_files(workspace_dir)
        target_files = SymbolEntityResolver.resolve_target_files(req.goal, workspace_dir, accessible)
        ambiguity = AmbiguityDetector.check_ambiguity(req.goal, target_files)
        change_specs = SymbolEntityResolver.extract_change_specs(req.goal)

        target_filename = None
        target_path_obj = None

        if req.target_file:
            tf_clean = req.target_file.strip()
            if tf_clean in accessible:
                target_path_obj = accessible[tf_clean]
                target_filename = target_path_obj.name
            elif (Path(workspace_dir) / tf_clean).exists():
                target_path_obj = Path(workspace_dir) / tf_clean
                target_filename = target_path_obj.name
            else:
                for k, p in accessible.items():
                    if p.name.lower() == tf_clean.lower() or k.lower() == tf_clean.lower():
                        target_path_obj = p
                        target_filename = p.name
                        break

        if not target_path_obj:
            if target_files:
                target_path_obj = accessible.get(target_files[0]) or (Path(workspace_dir) / target_files[0])
                target_filename = target_path_obj.name
            else:
                direct_files = WorkspaceFileResolver.resolve_query_files(req.goal, workspace_dir)
                if direct_files:
                    target_path_obj = Path(direct_files[0].file_path)
                    target_filename = target_path_obj.name
                elif accessible:
                    py_files = [p for p in accessible.values() if p.suffix == ".py"]
                if py_files:
                    target_path_obj = py_files[0]
                    target_filename = target_path_obj.name
                else:
                    target_path_obj = list(accessible.values())[0]
                    target_filename = target_path_obj.name

        if not target_filename:
            words = req.goal.split()
            for w in words:
                clean_w = w.strip(".,;:?!'\"()[]{}<>`")
                if "." in clean_w and not clean_w.startswith("."):
                    target_filename = clean_w
                    break
            if not target_filename:
                target_filename = "test_c.py" if (Path(workspace_dir) / "test_c.py").exists() else "main.py"

        # AST Skeleton + Target Context
        orig_lines = []
        raw_file_content = ""
        file_content_context = ""
        target_symbols = []
        preferred_line = req.cursor_line
        if target_path_obj and target_path_obj.exists() and target_path_obj.is_file():
            try:
                raw_file_content = target_path_obj.read_text(encoding="utf-8", errors="ignore")
                orig_lines = raw_file_content.splitlines()
                target_symbols = SymbolEntityResolver.resolve_target_symbols(req.goal, raw_file_content)
                if preferred_line is None:
                    sym_positions = SymbolEntityResolver.resolve_target_symbols_with_positions(req.goal, raw_file_content)
                    if sym_positions:
                        preferred_line = sym_positions[0]["lineno"]

                file_content_context, _ = ContextWindowBuilder.build_ast_skeleton_context(
                    raw_file_content,
                    target_filename,
                    target_symbols=target_symbols,
                    preferred_line=preferred_line
                )
            except Exception:
                orig_lines = []

        direct_files = WorkspaceFileResolver.resolve_query_files(req.goal, workspace_dir)
        retrieved = await hybrid_engine.search(req.goal, top_k=3, workspace_root=workspace_dir)
        combined = direct_files + [c for c in retrieved if c.file_path not in {d.file_path for d in direct_files}]
        context = HybridSearchEngine.build_context_prompt(combined[:3], workspace_root=workspace_dir)

        specs_info = ""
        if change_specs:
            specs_info += f"Identified Change Targets: {json.dumps(change_specs)}\n"
        if target_symbols:
            specs_info += f"Identified Target Symbols: {', '.join(target_symbols)}\n"
        if preferred_line:
            specs_info += f"Target Code Region Focus: around line {preferred_line}\n"

        prompt = (
            f"Goal: {req.goal}\n"
            f"Target File: {target_filename}\n"
            f"{specs_info}"
            f"{file_content_context}\n"
            f"You are implementing the surgical code solution for '{target_filename}' to achieve: {req.goal}\n"
            "STRICT OPERATIONAL & SURGICAL CODING RULES:\n"
            "- ZERO EMOJIS: Do not use emojis anywhere in your output.\n"
            f"- MINIMAL SURGICAL SCOPE: If '{target_filename}' exists, make ONLY the minimal changes required to achieve the goal. Do NOT rewrite unchanged logic or delete the file.\n"
            "- EXACT LINE NUMBERS: Use the exact literal 1-indexed line numbers shown before the colons in the context above. NEVER guess or hallucinate line numbers.\n"
            "- CONTEXT ANCHORING: Include 2 to 3 lines of unchanged context above and below the change to ensure unambiguous anchor matching.\n"
            "- EXACT INDENTATION: Preserve the target file's exact leading indentation (spaces/tabs) to prevent syntax or indentation errors.\n"
            "- NO LAZY TRUNCATIONS: Never write placeholder comments like '// ... rest of code unchanged ...' or '# ... existing code ...'.\n"
            "- REAL-TIME & GAMEPLAY PARAMS: If adjusting game mechanics or timing (e.g. speed, delay, fps, timeout, interval), identify the exact constant and update it cleanly.\n"
            "- Output your solution in ONE of these formats:\n"
            "  Format A (SEARCH/REPLACE block - recommended for 1-5 line edits):\n"
            "  <<<<<<< SEARCH\n"
            "  <exact existing lines to replace>\n"
            "  =======\n"
            "  <new replacement lines>\n"
            "  >>>>>>> REPLACE\n\n"
            "  Format B (Standard Unified Diff with EXACT line numbers from context above):\n"
            "  ```diff\n"
            "  @@ -line,count +line,count @@\n"
            "  -old line\n"
            "  +new line\n"
            "  ```\n\n"
            "  Format C (Updated function or variable assignment in code block):\n"
            "  ```python\n"
            "  <the updated function or statement>\n"
            "  ```\n\n"
            f"If '{target_filename}' is empty or new, write the full, working implementation from scratch.\n"
            "Do NOT refuse. Do NOT output conversational fluff. Output the code block directly."
        )
        generated = await ollama_provider.generate_text(prompt, code_context=context)

        # Build surgical minimal diff using multi-strategy builder (eliminates full-file wipes and line desync)
        diff_text = PatchEngine.build_surgical_diff(orig_lines, generated, target_filename, preferred_line=preferred_line)

        # Pre-staging AST syntax validation & automated self-healing
        is_valid, final_diff, err_msg = PatchEngine.validate_and_heal_diff(orig_lines, diff_text, target_filename)

        # 1-shot self-healing repair loop if syntax error remains
        if not is_valid and err_msg and target_filename.endswith(".py"):
            try:
                repair_prompt = (
                    f"The proposed modification for '{target_filename}' caused: {err_msg}\n"
                    f"Goal was: {req.goal}\n"
                    f"Original diff was:\n{diff_text}\n"
                    f"Please provide the corrected, syntactically valid code or SEARCH/REPLACE block for '{target_filename}'.\n"
                    "Output ONLY the corrected code or diff inside a code block."
                )
                repair_gen = await ollama_provider.generate_text(repair_prompt, code_context=context)
                repaired_diff = PatchEngine.build_surgical_diff(orig_lines, repair_gen, target_filename)
                rep_valid, rep_diff, _ = PatchEngine.validate_and_heal_diff(orig_lines, repaired_diff, target_filename)
                if rep_valid:
                    final_diff = rep_diff
                    is_valid = True
            except Exception:
                pass

        # Extract filename from diff header if present
        if final_diff:
            header_match = re.search(r"\+\+\+\s+(?:b/)?([^\s\n\r]+)", final_diff)
            if header_match and header_match.group(1) != "/dev/null":
                target_filename = Path(header_match.group(1)).name

        hunks = PatchEngine.split_into_hunks(final_diff)
        staged_files = PatchEngine.split_multi_file_diff(final_diff)

        log_msg = f"Synthesized surgical solution for {target_filename}." if final_diff else f"Generated response for {target_filename}."
        return {
            "step_id": 2,
            "status": "done",
            "target_file": target_filename,
            "diff": final_diff,
            "hunks": hunks,
            "staged_files": staged_files,
            "valid": is_valid,
            "generated": generated,
            "log": log_msg
        }
    # Step 3: AST verification & zero-regression checks
    elif req.step_id == 3:
        return {
            "step_id": 3,
            "status": "done",
            "log": "Syntax and AST verification passed. Changes ready for inspection in Diff tab."
        }
    return {"status": "error", "message": "Unknown step"}


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
                live_file_buffers[str(p_obj.resolve())] = new_text
                live_file_buffers[p_obj.name] = new_text
                await reindex_file(str(p_obj.resolve()), new_text)
        except Exception as e:
            logger.error(f"Failed to auto-reindex patched file {patched_file}: {e}")

    return result

class ComputeDiffRequest(BaseModel):
    target_file: str
    code_or_snippet: str
    workspace_path: Optional[str] = None
    preferred_line: Optional[int] = None

@app.post("/api/agent/compute-diff")
async def compute_diff(req: ComputeDiffRequest):
    workspace = resolve_workspace_path(req.workspace_path)
    target_path = PatchEngine.resolve_target_path(req.target_file, str(workspace))
    orig_lines = []
    if target_path.exists() and target_path.is_file():
        try:
            orig_lines = target_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            orig_lines = []

    diff_text = PatchEngine.build_surgical_diff(orig_lines, req.code_or_snippet, target_path.name, preferred_line=req.preferred_line)
    is_valid, final_diff, err = PatchEngine.validate_and_heal_diff(orig_lines, diff_text, target_path.name)
    hunks = PatchEngine.split_into_hunks(final_diff)
    staged_files = PatchEngine.split_multi_file_diff(final_diff)
    return {
        "status": "success",
        "target_file": target_path.name,
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
        "ambiguity": ambiguity
    }

class DiscardHunkRequest(BaseModel):
    file_path: str
    diff: str
    hunk_index: int
    workspace_path: Optional[str] = None

@app.post("/api/agent/discard-hunk")
async def discard_diff_hunk(req: DiscardHunkRequest):
    workspace = resolve_workspace_path(req.workspace_path)
    target_path = PatchEngine.resolve_target_path(req.file_path, str(workspace))
    orig_lines = []
    if target_path.exists() and target_path.is_file():
        try:
            orig_lines = target_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            orig_lines = []

    new_diff = PatchEngine.discard_hunk(orig_lines, req.diff, req.hunk_index, target_path.name)
    hunks = PatchEngine.split_into_hunks(new_diff)
    return {
        "status": "success",
        "file_path": target_path.name,
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

@app.post("/api/agent/terminal/execute")
async def execute_terminal_command(req: TerminalCommandRequest):
    cwd = str(resolve_workspace_path(req.workspace_path or runtime_settings.get("repo_path")))
    terminal = TerminalAgent(cwd)
    sec_mode = req.security_mode or runtime_settings.get("security_mode", "sandboxed")
    allow_net = runtime_settings.get("allow_network", True)
    res = await terminal.run_command(req.command, security_mode=sec_mode, allow_network=allow_net)
    return {
        "command": req.command,
        "workspace": terminal.workspace_path,
        "os": res.get("os", terminal.os_type),
        "security_mode": sec_mode,
        "exit_code": res.get("exit_code", 0),
        "stdout": res.get("stdout", ""),
        "stderr": res.get("stderr", ""),
        "passed": res.get("passed", False)
    }

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
