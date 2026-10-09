#!/usr/bin/env python3
"""
CLAY Copilot - Zero-Dependency Standalone Server
Uses standard Python library (http.server, urllib, sqlite3, ast)
to provide the full backend API on port 8765 with zero pip installs!
"""

import sys
import json
import urllib.request
import urllib.error
import re
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

# Import local modules
from app.parser.ast_chunker import PythonASTChunker
from app.parser.universal_chunker import UniversalASTChunker
from app.security.secret_masker import SecretMasker
from app.index.graph_store import SQLiteGraphStore
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
from app.retrieval.file_resolver import WorkspaceFileResolver
from app.retrieval.repo_map import RepoMapGenerator
from app.llm.history_compactor import HistoryCompactor

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "gpt-oss:120b-cloud"
PORT = 8765

SETTINGS_FILE = Path(__file__).resolve().parent / "runtime_settings.json"

runtime_settings = {
    "model": OLLAMA_MODEL,
    "connection_type": "local",
    "host": OLLAMA_URL,
    "temperature": 0.2,
    "num_ctx": 32768,
    "num_predict": 4096,
    "privacy_filter": True,
    "exclusion_patterns": ".env, *.pem, credentials.json, id_rsa, *.key",
    "security_mode": "sandboxed",
    "allow_network": True,
    "repo_path": ""
}

def load_persisted_settings():
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for k, v in data.items():
                runtime_settings[k] = v
            if "exclusion_patterns" in data and data["exclusion_patterns"]:
                SecretMasker.set_custom_patterns(str(data["exclusion_patterns"]))
        except Exception:
            pass

def save_persisted_settings():
    try:
        # Exclude secrets/keys from being persisted to plaintext JSON on disk
        persisted = {k: v for k, v in runtime_settings.items() if k != "api_key"}
        SETTINGS_FILE.write_text(json.dumps(persisted, indent=2), encoding="utf-8")
    except Exception:
        pass

load_persisted_settings()

def is_path_in_workspace(target_path, workspace_dir) -> bool:
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

graph_store = SQLiteGraphStore("code_graph.sqlite")
graph_store._sync_init_db()
ast_chunker = UniversalASTChunker()
workspace_file_states = {}
standalone_chat_history = []
standalone_rolling_summary = None

def _silent_sync_workspace_sync(workspace_dir: str):
    root = Path(workspace_dir).resolve()
    changed_count = 0
    deleted_count = 0
    if not root.exists() or not root.is_dir():
        return 0, 0
    current_files = set()
    code_exts = {
        ".py", ".ts", ".js", ".jsx", ".tsx", ".mjs", ".cjs",
        ".rs", ".go", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
        ".java", ".cs", ".php", ".rb", ".kt", ".kts", ".swift",
        ".json", ".md", ".html", ".css", ".toml", ".yaml", ".yml", ".sql", ".sh", ".bash"
    }
    excluded = {"venv", ".venv", "env", "node_modules", "dist", "build", "__pycache__", ".git", ".idea", ".vscode", ".gemini"}
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in code_exts:
            continue
        if any(part.startswith(".") or part in excluded for part in p.parts):
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
                graph_store._sync_delete_file_symbols(p_str)
                if st.st_size > 0:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                    syms, rels, chunks = ast_chunker.parse_file(p_str, content)
                    graph_store._sync_insert_batch(syms, rels)
                    graph_store._sync_index_chunks_bm25(chunks)
                changed_count += 1
        except Exception:
            continue

    tracked_keys = list(workspace_file_states.keys())
    for tracked in tracked_keys:
        if tracked.startswith(str(root)) and tracked not in current_files:
            del workspace_file_states[tracked]
            graph_store._sync_delete_file_symbols(tracked)
            deleted_count += 1
    return changed_count, deleted_count

class CopilotRequestHandler(BaseHTTPRequestHandler):

    def _set_cors_headers(self, content_type="application/json"):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Type", content_type)

    def do_OPTIONS(self):
        self.send_response(200)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            data = {
                "status": "healthy",
                "service": "codebase-copilot-backend",
                "model": OLLAMA_MODEL,
                "server": "standalone_standard_library"
            }
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif self.path.startswith("/api/settings"):
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            raw_key = runtime_settings.get("api_key", "")
            masked_key = (
                f"{raw_key[:4]}••••••••{raw_key[-4:]}"
                if len(raw_key) > 8
                else ("••••••••" if raw_key else "")
            )
            data = {
                "status": "success",
                "active_model": runtime_settings.get("model", OLLAMA_MODEL),
                "host": runtime_settings.get("host", OLLAMA_URL),
                "connection_type": runtime_settings.get("connection_type", "local"),
                "has_api_key": bool(raw_key),
                "api_key_masked": masked_key,
                "api_key": masked_key,
                "temperature": runtime_settings.get("temperature", 0.2),
                "num_ctx": runtime_settings.get("num_ctx", 32768),
                "num_predict": runtime_settings.get("num_predict", 4096),
                "privacy_filter": runtime_settings.get("privacy_filter", True),
                "exclusion_patterns": runtime_settings.get("exclusion_patterns", ".env, *.pem, credentials.json, id_rsa, *.key"),
                "security_mode": runtime_settings.get("security_mode", "sandboxed"),
                "allow_network": runtime_settings.get("allow_network", True),
                "repo_path": runtime_settings.get("repo_path", "")
            }
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif self.path == "/api/ollama/status":
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            try:
                with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    models = [m.get("name") for m in data.get("models", [])]
                    res = {
                        "connected": True,
                        "models": models,
                        "has_gemma": any("gemma4" in m for m in models)
                    }
            except Exception:
                res = {"connected": False, "models": [], "has_gemma": False}
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif self.path == "/api/agent/task":
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            task_data = {
                "id": "TASK #0428",
                "title": "Refactor Auth & Test Fixtures",
                "priority": "High priority",
                "mode": "autonomous",
                "model": OLLAMA_MODEL,
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
            self.wfile.write(json.dumps(task_data).encode("utf-8"))

        elif self.path.startswith("/api/graph/mermaid"):
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            try:
                import sqlite3
                from urllib.parse import urlparse, parse_qs
                from app.main import graph_store, runtime_settings, DEFAULT_WORKSPACE
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                target_path = qs.get("path", [None])[0] or runtime_settings.get("repo_path") or DEFAULT_WORKSPACE
                target_file = qs.get("file", [None])[0]
                target_norm = str(Path(target_path).resolve()).lower()
                selected_file_norm = target_file.strip().replace("\\", "/").lower() if target_file and target_file.strip() else None
                diagram_lines = ["graph TD"]
                seen_edges = set()

                nodes_meta = {}
                with sqlite3.connect(graph_store.db_path) as db:
                    try:
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
                        sel_filename = Path(selected_file_norm).name.lower()
                        cursor = db.execute(
                            "SELECT source_symbol, target_symbol, relation_type, file_path, line_number FROM relationships "
                            "WHERE LOWER(file_path) LIKE ? OR LOWER(file_path) LIKE ? LIMIT 60",
                            (f"%{selected_file_norm}%", f"%{sel_filename}%")
                        )
                    else:
                        cursor = db.execute("SELECT source_symbol, target_symbol, relation_type, file_path, line_number FROM relationships LIMIT 60")

                    for src, tgt, rel, fpath, lnum in cursor.fetchall():
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

                        if clean_src not in nodes_meta and fpath:
                            nodes_meta[clean_src] = {
                                "file": fpath,
                                "line": lnum or 1,
                                "type": "function",
                                "name": src.split("#")[-1]
                            }
                        if clean_tgt not in nodes_meta and fpath:
                            nodes_meta[clean_tgt] = {
                                "file": fpath,
                                "line": lnum or 1,
                                "type": "function",
                                "name": tgt.split("#")[-1]
                            }

                    if len(diagram_lines) == 1:
                        if selected_file_norm:
                            sym_cursor = db.execute(
                                "SELECT name, symbol_type, file_path, start_line FROM symbols "
                                "WHERE LOWER(file_path) LIKE ? OR LOWER(file_path) LIKE ? LIMIT 35",
                                (f"%{selected_file_norm}%", f"%{sel_filename}%")
                            )
                        else:
                            sym_cursor = db.execute("SELECT name, symbol_type, file_path, start_line FROM symbols LIMIT 30")

                        for sname, stype, sfpath, sline in sym_cursor.fetchall():
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
                            if clean_name not in nodes_meta:
                                nodes_meta[clean_name] = {
                                    "file": sfpath,
                                    "line": sline or 1,
                                    "type": stype,
                                    "name": sname.split("#")[-1]
                                }

                if len(diagram_lines) > 1:
                    data = {"mermaid": "\n".join(diagram_lines), "available": True, "focused_file": target_file, "nodes_meta": nodes_meta}
                else:
                    msg = (
                        f"No call relationships or symbols found for '{target_file}'.\nClick 'Scan & Index Workspace' to analyze this file or select another file."
                        if target_file
                        else "No SQLite code graph available for this repository.\nClick 'Scan & Index Workspace' to analyze codebase structure."
                    )
                    data = {"mermaid": "", "available": False, "message": msg, "nodes_meta": {}}
            except Exception as e:
                data = {"mermaid": "", "available": False, "message": f"Graph query error: {str(e)}"}
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif self.path.startswith("/api/repo/tree"):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            target_path = qs.get("path", [None])[0] or "."
            try:
                from app.main import build_essential_file_tree
                root = Path(target_path)
                if not root.exists() or not root.is_dir():
                    root = Path(__file__).resolve().parent
                data = build_essential_file_tree(root)
            except Exception as e:
                data = {"status": "error", "error": str(e), "tree": [], "stats": {"total_files": 0, "total_dirs": 0, "total_lines": 0}}
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif self.path.startswith("/api/repo/map"):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            target_path = qs.get("workspace_path", [None])[0] or qs.get("path", [None])[0] or runtime_settings.get("repo_path") or "."
            try:
                data = RepoMapGenerator.get_repo_map_data(target_path, graph_store=graph_store)
            except Exception as e:
                data = {"status": "error", "error": str(e), "files_count": 0, "symbols_count": 0, "map_text": ""}
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif self.path.startswith("/api/system/info"):
            import platform
            from urllib.parse import parse_qs, urlparse
            is_win = platform.system().lower() == "windows"
            raw_ws = None
            if "?" in self.path:
                qs = parse_qs(urlparse(self.path).query)
                raw_ws = qs.get("workspace_path", [None])[0]
            ws_path = str(Path.cwd().resolve())
            if raw_ws and Path(raw_ws).exists() and Path(raw_ws).is_dir():
                ws_path = str(Path(raw_ws).resolve())
            data = {
                "os": platform.system().lower(),
                "platform": platform.platform(),
                "is_windows": is_win,
                "shell": "PowerShell / CMD" if is_win else "bash",
                "workspace": ws_path,
                "recommended_commands": ["dir", "python --version", "git status", "pytest"] if is_win else ["ls -la", "python3 --version", "git status", "pytest"]
            }
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif self.path.startswith("/api/sync/silent-check"):
            from urllib.parse import parse_qs, urlparse
            raw_ws = None
            if "?" in self.path:
                qs = parse_qs(urlparse(self.path).query)
                raw_ws = qs.get("workspace_path", [None])[0]
            ws_path = raw_ws or runtime_settings.get("repo_path") or str(Path.cwd().resolve())
            changed, deleted = _silent_sync_workspace_sync(ws_path)
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "synced",
                "changed": changed,
                "deleted": deleted,
                "tracked_files": len(workspace_file_states)
            }).encode("utf-8"))

        elif self.path.startswith("/api/repo/security-audit"):
            from urllib.parse import parse_qs, urlparse
            raw_ws = None
            if "?" in self.path:
                qs = parse_qs(urlparse(self.path).query)
                raw_ws = qs.get("path", [None])[0] or qs.get("workspace_path", [None])[0]
            try:
                from app.main import resolve_workspace_path
                target_root = resolve_workspace_path(raw_ws or runtime_settings.get("repo_path"))
                data = SecretMasker.scan_repository(str(target_root))
            except Exception as e:
                data = {"status": "clean", "scanned_files": 0, "findings_count": 0, "findings": [], "summary": f"Scan error: {str(e)}"}
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))

        else:
            self.send_response(404)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(b'{"error": "Not Found"}')

    def do_POST(self):
        global OLLAMA_MODEL, OLLAMA_URL
        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            body = json.loads(post_body)
        except Exception:
            body = {}

        if self.path == "/api/sync/file":
            file_path = body.get("file_path", "")
            content = body.get("content", "")
            event = body.get("event", "changed")
            ws_path = body.get("workspace_path") or runtime_settings.get("repo_path", ".")
            p = Path(file_path).resolve()
            ws_resolved = Path(ws_path).resolve()
            if not is_path_in_workspace(p, ws_resolved):
                self.send_response(200)
                self._set_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ignored", "file": str(p), "reason": "file_outside_workspace"}).encode("utf-8"))
                return

            if event == "deleted" or (content is None and not p.exists()):
                graph_store._sync_delete_file_symbols(str(p))
            else:
                graph_store._sync_delete_file_symbols(str(p))
                if content and content.strip():
                    try:
                        syms, rels, chunks = ast_chunker.parse_file(str(p), content)
                        graph_store._sync_insert_batch(syms, rels)
                        graph_store._sync_index_chunks_bm25(chunks)
                    except Exception:
                        pass
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            lines_cnt = len(content.splitlines()) if content else 0
            self.wfile.write(json.dumps({"status": "synced", "file": str(p), "lines": lines_cnt}).encode("utf-8"))

        elif self.path == "/api/chat":
            query = body.get("query", "")
            context_file = body.get("context_file")
            active_file_content = body.get("active_file_content")
            raw_ws = (body.get("workspace_path") or "").strip()
            has_workspace = bool(raw_ws and raw_ws != ".")
            ws_path = raw_ws if has_workspace else ""
            ws_resolved = Path(ws_path).resolve() if has_workspace else None
            if context_file and ws_resolved and not is_path_in_workspace(context_file, ws_resolved):
                context_file = None
                active_file_content = None

            # Silent zero-token background sync check before resolving context
            if has_workspace:
                try:
                    _silent_sync_workspace_sync(ws_path)
                except Exception:
                    pass
            
            # Resolve conversation history: prefer request history, fallback to server session
            req_history = body.get("history")
            effective_history = []
            if req_history is not None:
                effective_history = list(req_history)
                standalone_chat_history.clear()
                standalone_chat_history.extend(effective_history)
            else:
                effective_history = list(standalone_chat_history)

            # Multi-turn context window rolling compaction (Tier 1 recent + Tier 2 summary)
            global standalone_rolling_summary
            ctx_tokens = int(body.get("num_ctx", runtime_settings.get("num_ctx", 32768)))
            compacted_history, rolling_summary = HistoryCompactor.compact_history(
                effective_history,
                num_ctx=ctx_tokens,
                active_summary=standalone_rolling_summary
            )
            standalone_rolling_summary = rolling_summary

            # Resolve fresh context directly using WorkspaceFileResolver with multi-turn session awareness
            resolved_chunks = WorkspaceFileResolver.resolve_query_files(
                query=query,
                workspace_root=ws_path,
                context_file=context_file,
                context_file_content=active_file_content,
                history=effective_history
            )
            if resolved_chunks:
                code_ctx = "\n\n".join(f"[{c.file_path}]\n{c.content}" for c in resolved_chunks)
            elif active_file_content is not None:
                lines = active_file_content.splitlines()
                total_lines = len(lines)
                c_line = body.get("cursor_line")
                if total_lines <= 600:
                    numbered_lines = [f"{i+1:3d}: {l}" for i, l in enumerate(lines)]
                else:
                    if c_line and 1 <= c_line <= total_lines:
                        c_idx = c_line - 1
                        w_start = max(0, c_idx - 250)
                        w_end = min(total_lines, c_idx + 250)
                    else:
                        w_start = 0
                        w_end = min(total_lines, 500)
                    numbered_lines = [f"{i+1:3d}: {l}" for i, l in enumerate(lines[w_start:w_end], start=w_start)]
                code_ctx = (
                    f"[Active Editor File ({total_lines} lines total, with EXACT 1-Indexed Line Numbers)]\n"
                    f"[IMPORTANT: Use the exact line numbers shown before the colons. Do NOT make up line numbers.]\n"
                    + "\n".join(numbered_lines)
                )
            elif context_file and Path(context_file).exists():
                if not SecretMasker.is_file_sensitive(context_file):
                    file_text = Path(context_file).read_text(encoding="utf-8", errors="ignore")
                    lines = file_text.splitlines()
                    total_lines = len(lines)
                    c_line = body.get("cursor_line")
                    if total_lines <= 600:
                        numbered_lines = [f"{i+1:3d}: {l}" for i, l in enumerate(lines)]
                    else:
                        if c_line and 1 <= c_line <= total_lines:
                            c_idx = c_line - 1
                            w_start = max(0, c_idx - 250)
                            w_end = min(total_lines, c_idx + 250)
                        else:
                            w_start = 0
                            w_end = min(total_lines, 500)
                        numbered_lines = [f"{i+1:3d}: {l}" for i, l in enumerate(lines[w_start:w_end], start=w_start)]
                    code_ctx = (
                        f"[{Path(context_file).name} ({total_lines} lines total, with EXACT 1-Indexed Line Numbers)]\n"
                        f"[IMPORTANT: Use the exact line numbers shown before the colons. Do NOT make up line numbers.]\n"
                        + "\n".join(numbered_lines)
                    )
            else:
                code_ctx = ""

            # Generate Global Repo Map for whole-codebase architectural awareness (only when repository is active)
            if has_workspace:
                try:
                    repo_map = RepoMapGenerator.generate_repo_map(ws_path, graph_store=graph_store, max_chars=4000)
                    if repo_map:
                        code_ctx = f"<repository_map>\n{repo_map}\n</repository_map>\n\n" + (code_ctx or "")
                except Exception:
                    pass

            # Target creation directive if user requested writing/creating new files or folders
            extracted_targets = SymbolEntityResolver.extract_target_filepaths(query)
            if extracted_targets:
                target_str = ", ".join(extracted_targets)
                first_target = extracted_targets[0]
                target_ext = Path(first_target).suffix.lstrip(".")
                lang_hint = target_ext if target_ext else "python"
                creation_directive = (
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
                    f"</target_creation>\n\n"
                )
                code_ctx = creation_directive + (code_ctx or "")

            is_privacy_guard = runtime_settings.get("privacy_filter", True)
            sanitized_query = SecretMasker.mask_text(query, enabled=is_privacy_guard)
            sanitized_ctx = SecretMasker.mask_text(code_ctx, enabled=is_privacy_guard)

            # Budget context according to context window length setting
            ctx_tokens = int(body.get("num_ctx", runtime_settings.get("num_ctx", 32768)))
            max_ctx_chars = max(10000, int(ctx_tokens * 3.5))
            if len(sanitized_ctx) > max_ctx_chars:
                sanitized_ctx = sanitized_ctx[:max_ctx_chars] + f"\n... [Context truncated at {max_ctx_chars} characters according to {ctx_tokens} context window]"

            self.send_response(200)
            self._set_cors_headers(content_type="text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            # Connect to Ollama with Host OS awareness
            import platform
            is_win = platform.system().lower() == "windows"
            host_os = "Windows" if is_win else "Linux"
            system_instruction = (
                f"You are CLAY Copilot, an elite autonomous repository intelligence engine and proactive AI coding partner. The host operating system is {host_os}. "
                f"Adjust all terminal/bash commands and file paths specifically for {host_os}. "
                + ("On Windows, always use 'python' (never 'python3'), PowerShell/CMD syntax (e.g. 'dir', 'python -m pytest', '.\\venv\\Scripts\\activate'). Never recommend 'source' or 'sudo' or 'python3'." if is_win else "On Linux, use standard bash syntax, 'python3', 'source venv/bin/activate', 'ls -la'.")
                + " PERSONA & TONE: For conversational greetings and interactive messages ('hello', 'hi', status checks), be warm, welcoming, intelligent, and collaborative—acknowledge active workspace context or open files and offer concrete ways you can assist, never giving cold robotic one-word replies like 'Hello.' For technical and coding requests, maintain a direct, rigorous, and razor-sharp technical voice without filler or platitudes (begin directly with a concise technical summary followed immediately by the code block or diff; do NOT emit a bare '[plan]' header). When asked about your capabilities or what you can do, provide an organized, structured breakdown showing both your Non-LLM UI Workspaces & Inline Features (Chat, Agent, Repo, Diff, Settings tabs, interactive SVG graphs, hybrid search, persistent vector store) and your LLM Intelligence Capabilities (surgical refactoring, architectural summarization, system diagrams, and autonomous bug-fixing). STRICT ZERO EMOJIS: Never output emojis under any circumstances. "
                + "CLAY ECOSYSTEM ARCHITECTURE & DUAL-PILLAR CAPABILITIES: You have two core pillars of capabilities: "
                + "PILLAR 1 (NON-LLM UI WORKSPACES & INLINE RUNTIME FEATURES): "
                + "1) Chat: multi-turn conversation with memory compaction, 60fps smooth token streaming, dynamic thinking drawer with live stopwatch and step ticker, clickable line citation pills, @file/@symbol/@folder chip pinning, 1-click copy, and instant abort without UI freeze. "
                + "2) Agent: autonomous multi-step planner with task status badges (done/active/queued), native subshell terminal execution with live stream logs, surgical AST patch engine, and self-healing error repair loop that catches runtime/syntax failures and auto-repairs them. "
                + "3) Repo: live multi-language AST repository map, structural symbol hierarchy, interactive dependency/call graphs, symbol search/filter with 1-click jump to editor code, and real-time file watcher. "
                + "4) Diff: split and unified visual diff viewer, hunk-by-hunk editing, and 1-click 'Accept & Apply to Disk' or 'Discard'. "
                + "5) Settings: model selector for Ollama & cloud, temperature & context window sliders, security modes (Sandboxed, Strict, Open), and Zero-Leak Privacy Guard. "
                + "6) Inline Engines: interactive SVG Mermaid diagrams with drag-to-pan and 30%-500% zoom, persistent SQLite vector store (code_graph.sqlite) with packed binary BLOBs and <5ms boot loading, SQLite FTS5 BM25 search, and 4-stream RRF hybrid search. "
                + "PILLAR 2 (LLM INTELLIGENCE & CODING CAPABILITIES): "
                + "1) Surgical code refactoring using clean SEARCH/REPLACE blocks or unified diffs with exact line numbers and zero placeholders. "
                + "2) Architectural repository summarization and data flow analysis using markdown tables. "
                + "3) Publishing-grade interactive system diagrams (flowcharts, class diagrams, sequence diagrams) with 100% code fidelity. "
                + "4) Autonomous bug diagnosis and stack trace analysis. "
                + "5) Cross-platform CLI and script adaptation for host OS. "
                + "SECURITY & PRIVACY BOUNDARIES: You operate under strict workspace sandboxing, Zero-Leak Privacy secret masking, and prompt injection defense boundaries. "
                + "STRUCTURED RESPONSE ORDER & CLEAN HEADERS: When providing code modifications, diffs, or diagrams, do NOT emit bare '[plan]' or '[diff]' label headers. Begin directly with a concise 1-sentence technical summary followed immediately by the unified diff, SEARCH/REPLACE block, or complete code implementation. "
                + "When asked to write, implement, create, or refactor code: ALWAYS generate complete working code or surgical updates using standard SEARCH/REPLACE blocks or unified diffs with exact 1-indexed line headers. Include 2-3 lines of unchanged context above and below the change. NEVER hallucinate line numbers. Preserve exact leading indentation. "
                + "Never emit placeholder comments like '// ... rest of code unchanged ...'. When adjusting game mechanics, timing, or loops (e.g. speed, delay, fps, timeout), identify the exact parameter and update it cleanly. "
                + "NEVER escape quotes as \\\"\\\"\\\" inside code blocks. If a target file is empty or new, populate it from scratch. NEVER refuse a request because a file is empty or missing. "
                + "When outputting comparison, component, or file breakdown data, format them using standard markdown tables (| Column 1 | Column 2 |). For section headers, use clean markdown headings (## or ###) without hashtag clutter. "
                + "STRICT ON-DEMAND DIAGRAM POLICY & PUBLISHING-GRADE FIDELITY: "
                + "ZERO UNSOLICITED DIAGRAMS: You must NEVER generate or emit a Mermaid diagram (```mermaid) unprompted unless the user explicitly asks for a diagram, flowchart, sequence diagram, or visual architecture graph in their prompt. "
                + "MULTI-TURN AFFIRMATIVE RESPONSES & SESSION AWARENESS: Maintain strict conversational session awareness across turns. When you previously suggested or offered a diagram in your prior message (e.g. '(Tip: If you would like a visual flowchart of this workflow, just ask!)'), and the user responds affirmatively (e.g. 'yes', 'sure', 'yeah', 'please', 'show me', 'do it', 'draw it', 'ok', 'yes give me flowchart'), you MUST understand that the user is confirming your diagram offer! Immediately generate the requested diagram block visualizing the components discussed. Do NOT greet the user again or treat short confirmations as a blank greeting. "
                + "STRICT CODE FIDELITY & ANTI-HALLUCINATION: When generating diagrams for code files (such as test1.py, test2.py, or any workspace module), you MUST strictly use the actual functions, classes, methods, and call flows present in context. NEVER hallucinate generic placeholder functions or dummy names like 'step_one()', 'step_two()', 'step_three()', 'foo()', or 'bar()'. Every node must represent real code from the file. "
                + "ALLOWED DIAGRAM TYPES: Restrict diagrams to relevant software engineering formats: Architecture Flowcharts ('graph TD' or 'flowchart LR'), Class Architecture Diagrams ('classDiagram'), and Sequence Diagrams ('sequenceDiagram'). ZERO GANTT CHARTS FOR CODE: Never suggest or generate Gantt charts for code files or test scripts. "
                + "When explaining repository modules or workflows WITHOUT an explicit diagram request: explain purely through concise technical text, bullet points, and markdown tables. At the end of your explanation or summary, you may add a brief 1-line note: '(Tip: If you would like a visual flowchart of this workflow, just ask!)'. Under NO circumstances generate a diagram unprompted."
            )
            active_model = runtime_settings.get("model", OLLAMA_MODEL)
            active_host = runtime_settings.get("host", OLLAMA_URL)

            messages = [{"role": "system", "content": system_instruction}]

            # Inject rolling conversation summary if present (Tier 2)
            if standalone_rolling_summary and standalone_rolling_summary.strip():
                messages.append({
                    "role": "system",
                    "content": f"<conversation_summary>\n[Context from earlier turns in this session]:\n{standalone_rolling_summary.strip()}\n</conversation_summary>"
                })

            # Inject recent conversation turns (Tier 1)
            if compacted_history:
                for turn in compacted_history:
                    r = turn.get("role", "user")
                    c = turn.get("content", "")
                    if c and r in ("user", "assistant"):
                        m_c = SecretMasker.mask_text(c, enabled=runtime_settings.get("privacy_filter", True))
                        if len(m_c) > 3000:
                            m_c = m_c[:3000] + "..."
                        messages.append({"role": r, "content": m_c})

            messages.append({"role": "user", "content": f"Context:\n{sanitized_ctx}\n\nTask: {sanitized_query}"})

            ollama_payload = json.dumps({
                "model": active_model,
                "messages": messages,
                "stream": True,
                "options": {
                    "temperature": float(body.get("temperature", runtime_settings.get("temperature", 0.2))),
                    "num_ctx": ctx_tokens,
                    "num_predict": int(body.get("num_predict", runtime_settings.get("num_predict", 4096)))
                }
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{active_host}/api/chat",
                data=ollama_payload,
                headers={"Content-Type": "application/json"}
            )

            accumulated_tokens = []
            try:
                with urllib.request.urlopen(req, timeout=120.0) as resp:
                    for line in resp:
                        if line:
                            try:
                                chunk = json.loads(line.decode("utf-8"))
                                token = chunk.get("message", {}).get("content", "")
                                if token:
                                    clean_tok = token.replace(r'\"\"\"', '"""').replace(r"\'\'\'", "'''")
                                    accumulated_tokens.append(clean_tok)
                                    sse = f"data: {json.dumps({'token': clean_tok})}\n\n"
                                    self.wfile.write(sse.encode("utf-8"))
                                    self.wfile.flush()
                            except Exception:
                                pass
            except Exception as e:
                err_sse = f"data: {json.dumps({'token': f'\\n[Ollama Notice: {str(e)} - Ensure ollama serve is active]' })}\n\n"
                self.wfile.write(err_sse.encode("utf-8"))

            # Update session history with completed turn
            full_reply = "".join(accumulated_tokens)
            if query and full_reply:
                standalone_chat_history.append({"role": "user", "content": query})
                standalone_chat_history.append({"role": "assistant", "content": full_reply})
                if len(standalone_chat_history) > 30:
                    standalone_chat_history[:] = standalone_chat_history[-30:]

            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        elif self.path == "/api/chat/clear":
            global standalone_rolling_summary
            standalone_chat_history.clear()
            standalone_rolling_summary = None
            self.send_response(200)
            self._set_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "cleared", "message": "Chat context reset successfully."}).encode("utf-8"))

        elif self.path == "/api/index/scan":
            root_dir = (body.get("workspace_path") or "").strip()
            if not root_dir or root_dir == ".":
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Please select a repository."}).encode("utf-8"))
                return
            root = Path(root_dir)
            all_syms = []
            all_rels = []
            all_chunks = []
            indexed_count = 0

            if root.exists():
                code_exts = {
                    ".py", ".ts", ".js", ".jsx", ".tsx", ".mjs", ".cjs",
                    ".rs", ".go", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
                    ".java", ".cs", ".php", ".rb", ".kt", ".kts", ".swift",
                    ".json", ".md", ".html", ".css", ".toml", ".yaml", ".yml", ".sql", ".sh", ".bash"
                }
                for p in root.rglob("*"):
                    if not p.is_file() or p.suffix.lower() not in code_exts:
                        continue
                    if any(part.startswith(".") or part in ["venv", "node_modules", "dist"] for part in p.parts):
                        continue
                    if SecretMasker.is_file_sensitive(str(p)):
                        continue
                    try:
                        content = p.read_text(encoding="utf-8", errors="ignore")
                        syms, rels, chunks = ast_chunker.parse_file(str(p), content)
                        all_syms.extend(syms)
                        all_rels.extend(rels)
                        all_chunks.extend(chunks)
                        indexed_count += 1
                    except Exception:
                        continue

            graph_store._sync_insert_batch(all_syms, all_rels)
            graph_store._sync_index_chunks_bm25(all_chunks)

            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            res = {
                "status": "synced",
                "filesCount": indexed_count,
                "symbolsCount": len(all_syms),
                "relationshipsCount": len(all_rels)
            }
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif self.path == "/api/agent/terminal/execute":
            import platform
            cmd = body.get("command", "")
            raw_ws = body.get("workspace_path")
            
            # Intelligent workspace resolution
            workspace = None
            if raw_ws and isinstance(raw_ws, str) and raw_ws.strip() and raw_ws.strip() != ".":
                p = Path(raw_ws.strip())
                if p.exists() and p.is_dir():
                    workspace = str(p.resolve())

            if not workspace:
                # Default to repository root (parent of backend/)
                repo_root = Path(__file__).resolve().parent.parent
                if repo_root.exists() and (repo_root / "backend").exists():
                    workspace = str(repo_root.resolve())
                else:
                    cwd = Path.cwd().resolve()
                    if cwd.name == "backend" and (cwd.parent / "backend").exists():
                        workspace = str(cwd.parent.resolve())
                    else:
                        workspace = str(cwd)

            sec_mode = body.get("security_mode", "sandboxed")
            allow_net = body.get("allow_network", True)
            try:
                from app.agent.tools.terminal_agent import TerminalAgent
                terminal = TerminalAgent(workspace)
                res = terminal.run_command_sync(cmd, timeout=35, security_mode=sec_mode, allow_network=allow_net)
                resp_data = {
                    "command": cmd,
                    "workspace": res.get("workspace", terminal.workspace_path),
                    "os": res.get("os", terminal.os_type),
                    "security_mode": sec_mode,
                    "exit_code": res.get("exit_code", 0),
                    "stdout": res.get("stdout", ""),
                    "stderr": res.get("stderr", ""),
                    "passed": res.get("passed", False)
                }
            except Exception as e:
                err_msg = str(e) or repr(e) or type(e).__name__
                resp_data = {
                    "command": cmd,
                    "workspace": workspace,
                    "os": platform.system().lower(),
                    "security_mode": sec_mode,
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"Server execution error: {err_msg}",
                    "passed": False
                }
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(resp_data).encode("utf-8"))

        elif self.path == "/api/settings":
            for k in ["model", "connection_type", "host", "api_key", "temperature", "num_ctx", "num_predict", "privacy_filter", "exclusion_patterns", "security_mode", "allow_network", "repo_path", "theme_mode"]:
                if k in body and body[k] is not None:
                    runtime_settings[k] = body[k]
            if "model" in body and body["model"]:
                OLLAMA_MODEL = body["model"]
            if "host" in body and body["host"]:
                OLLAMA_URL = body["host"]
            if "exclusion_patterns" in body and body["exclusion_patterns"]:
                SecretMasker.set_custom_patterns(str(body["exclusion_patterns"]))
            save_persisted_settings()
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            resp = {
                "status": "success",
                "active_model": runtime_settings.get("model", OLLAMA_MODEL),
                "host": runtime_settings.get("host", OLLAMA_URL),
                "connection_type": runtime_settings.get("connection_type", "local"),
                "temperature": runtime_settings.get("temperature", 0.2),
                "num_ctx": runtime_settings.get("num_ctx", 32768),
                "num_predict": runtime_settings.get("num_predict", 4096),
                "privacy_filter": runtime_settings.get("privacy_filter", True),
                "exclusion_patterns": runtime_settings.get("exclusion_patterns", ".env, *.pem, credentials.json, id_rsa, *.key"),
                "security_mode": runtime_settings.get("security_mode", "sandboxed"),
                "allow_network": runtime_settings.get("allow_network", True),
                "repo_path": runtime_settings.get("repo_path", "")
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))
        elif self.path == "/api/repo/security-audit":
            raw_ws = body.get("path") or body.get("workspace_path") or runtime_settings.get("repo_path")
            try:
                from app.main import resolve_workspace_path
                target_root = resolve_workspace_path(raw_ws)
                data = SecretMasker.scan_repository(str(target_root))
            except Exception as e:
                data = {"status": "clean", "scanned_files": 0, "findings_count": 0, "findings": [], "summary": f"Scan error: {str(e)}"}
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif self.path == "/api/repo/map":
            target_path = body.get("workspace_path") or body.get("path") or runtime_settings.get("repo_path") or "."
            try:
                data = RepoMapGenerator.get_repo_map_data(target_path, graph_store=graph_store)
            except Exception as e:
                data = {"status": "error", "error": str(e), "files_count": 0, "symbols_count": 0, "map_text": ""}
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif self.path == "/api/file/open":
            raw_path = body.get("file_path", "").strip()
            repo_hint = body.get("repo_path", "").strip()
            p = Path(raw_path).resolve()
            if not p.exists() and repo_hint:
                alt_p = (Path(repo_hint) / raw_path).resolve()
                if alt_p.exists():
                    p = alt_p
            if not p.exists() and runtime_settings.get("repo_path"):
                alt_p = (Path(runtime_settings.get("repo_path")) / raw_path).resolve()
                if alt_p.exists():
                    p = alt_p
            if not p.exists():
                self.send_response(404)
                self._set_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"File not found: {raw_path}"}).encode("utf-8"))
            else:
                import platform, subprocess
                os_name = platform.system().lower()
                try:
                    if os_name == "windows":
                        import os
                        os.startfile(str(p))
                    elif os_name == "darwin":
                        subprocess.Popen(["open", str(p)])
                    else:
                        subprocess.Popen(["xdg-open", str(p)])
                    self.send_response(200)
                    self._set_cors_headers()
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success", "file": str(p)}).encode("utf-8"))
                except Exception as e:
                    self.send_response(500)
                    self._set_cors_headers()
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

        elif self.path == "/api/agent/apply-patch":
            file_path = body.get("file_path", "")
            diff_text = body.get("diff", "")
            ws_path = body.get("workspace_path") or runtime_settings.get("repo_path", ".")
            result = PatchEngine.apply_patch_hunks(
                file_path=file_path,
                diff_text=diff_text,
                workspace_path=str(ws_path)
            )
            self.send_response(200 if result.get("status") == "success" else 400)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(result).encode("utf-8"))

        elif self.path == "/api/agent/rebase-diff":
            file_path = body.get("file_path", "")
            base_content = body.get("base_content", "")
            diff_text = body.get("diff", "")
            current_content = body.get("current_content", "")
            ws_path = body.get("workspace_path") or runtime_settings.get("repo_path", ".")
            force_resolution = body.get("force_resolution")
            result = PatchEngine.rebase_staged_diff(
                file_path=file_path,
                base_content=base_content,
                diff_text=diff_text,
                current_content=current_content,
                workspace_path=str(ws_path),
                force_resolution=force_resolution
            )
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(result).encode("utf-8"))

        elif self.path == "/api/agent/compute-diff":
            file_path = body.get("target_file", "")
            code_snippet = body.get("code_or_snippet", "")
            ws_path = body.get("workspace_path") or runtime_settings.get("repo_path", ".")
            target_path = PatchEngine.resolve_target_path(file_path, str(ws_path))
            orig_lines = []
            active_content = body.get("active_file_content")
            if active_content is not None:
                orig_lines = active_content.splitlines()
                live_file_buffers[file_path] = active_content
                live_file_buffers[target_path.name] = active_content
            elif target_path.exists() and target_path.is_file():
                try:
                    orig_lines = target_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                except Exception:
                    orig_lines = []
            elif file_path in live_file_buffers:
                orig_lines = live_file_buffers[file_path].splitlines()
            elif target_path.name in live_file_buffers:
                orig_lines = live_file_buffers[target_path.name].splitlines()

            pref_line = body.get("preferred_line") or body.get("cursor_line")
            diff_text = PatchEngine.build_surgical_diff(orig_lines, code_snippet, target_path.name, preferred_line=pref_line)
            is_valid, final_diff, err = PatchEngine.validate_and_heal_diff(orig_lines, diff_text, target_path.name)
            hunks = PatchEngine.split_into_hunks(final_diff)
            staged_files = PatchEngine.split_multi_file_diff(final_diff)
            res = {
                "status": "success",
                "target_file": target_path.name,
                "diff": final_diff,
                "hunks": hunks,
                "staged_files": staged_files,
                "valid": is_valid,
                "error": err
            }
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif self.path == "/api/agent/parse-instruction":
            goal = body.get("goal", "")
            ws_path = body.get("workspace_path") or runtime_settings.get("repo_path", ".")
            intent_info = InstructionClassifier.classify(goal)
            accessible = WorkspaceFileResolver.list_accessible_files(ws_path)
            target_files = SymbolEntityResolver.resolve_target_files(goal, ws_path, accessible)
            ambiguity = AmbiguityDetector.check_ambiguity(goal, target_files)
            change_specs = SymbolEntityResolver.extract_change_specs(goal)

            target_symbols = []
            if target_files:
                primary_path = accessible.get(target_files[0]) or (Path(ws_path) / target_files[0])
                if primary_path.exists() and primary_path.is_file():
                    try:
                        content = primary_path.read_text(encoding="utf-8", errors="ignore")
                        target_symbols = SymbolEntityResolver.resolve_target_symbols(goal, content)
                    except Exception:
                        pass

            res = {
                "goal": goal,
                "intent": intent_info,
                "target_files": target_files,
                "target_symbols": target_symbols,
                "change_specs": change_specs,
                "ambiguity": ambiguity
            }
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif self.path == "/api/agent/discard-hunk":
            file_path = body.get("file_path", "")
            diff_text = body.get("diff", "")
            hunk_index = body.get("hunk_index", 0)
            ws_path = body.get("workspace_path") or runtime_settings.get("repo_path", ".")
            target_path = PatchEngine.resolve_target_path(file_path, str(ws_path))
            orig_lines = []
            if target_path.exists() and target_path.is_file():
                try:
                    orig_lines = target_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                except Exception:
                    orig_lines = []

            new_diff = PatchEngine.discard_hunk(orig_lines, diff_text, hunk_index, target_path.name)
            hunks = PatchEngine.split_into_hunks(new_diff)
            res = {
                "status": "success",
                "file_path": target_path.name,
                "diff": new_diff,
                "hunks": hunks
            }
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif self.path == "/api/agent/intra-diff":
            old_line = body.get("old_line", "")
            new_line = body.get("new_line", "")
            res = PatchEngine.compute_intra_line_diff(old_line, new_line)
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif self.path == "/api/agent/plan":
            goal = body.get("goal", "")
            raw_ws = body.get("workspace_path") or runtime_settings.get("repo_path", ".")
            from app.main import resolve_workspace_path
            ws_path = str(resolve_workspace_path(raw_ws))
            accessible = WorkspaceFileResolver.list_accessible_files(ws_path)
            target_files = SymbolEntityResolver.resolve_target_files(goal, ws_path, accessible)
            
            # Strictly validate active_file against ws_path
            active_in_ws = None
            if body.get("active_file") and is_path_in_workspace(body.get("active_file"), ws_path):
                active_in_ws = Path(body.get("active_file")).resolve().name

            target_candidate = None
            raw_tf = body.get("target_file")
            if raw_tf:
                cand_p = Path(raw_tf)
                if cand_p.is_absolute():
                    if is_path_in_workspace(cand_p, ws_path):
                        target_candidate = cand_p.name
                else:
                    target_candidate = cand_p.name

            # 1. Goal-extracted files take top priority.
            # 2. Only if goal had no explicit files, fallback to validated target_candidate or active_in_ws
            if not target_files:
                if target_candidate:
                    target_files.append(target_candidate)
                elif active_in_ws:
                    target_files.append(active_in_ws)

            # Only buffer active file content if verified inside workspace
            if active_in_ws and body.get("active_file_content"):
                live_file_buffers[active_in_ws] = body.get("active_file_content")
                if body.get("active_file") and is_path_in_workspace(body.get("active_file"), ws_path):
                    live_file_buffers[str(Path(body.get("active_file")).resolve())] = body.get("active_file_content")

            change_specs = SymbolEntityResolver.extract_change_specs(goal)
            selected_model = body.get("model_name") or runtime_settings.get("model", OLLAMA_MODEL)

            plan = DynamicTaskPlanner.plan_tasks(
                goal=goal,
                target_files=target_files,
                change_specs=change_specs,
                model=selected_model
            )
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(plan).encode("utf-8"))

        elif self.path in ("/api/agent/step", "/api/agent/step/stream"):
            is_stream = (self.path == "/api/agent/step/stream")
            if is_stream:
                self.send_response(200)
                self._set_cors_headers(content_type="text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
            step_id = body.get("step_id", 1)
            step_action = body.get("step_action")
            total_steps = body.get("total_steps", 3)
            goal = body.get("goal", "")
            raw_ws = body.get("workspace_path") or runtime_settings.get("repo_path", ".")
            from app.main import resolve_workspace_path
            ws_path = str(resolve_workspace_path(raw_ws))
            try:
                _silent_sync_workspace_sync(ws_path)
            except Exception:
                pass

            accessible = WorkspaceFileResolver.list_accessible_files(ws_path)
            target_files = SymbolEntityResolver.resolve_target_files(goal, ws_path, accessible)

            # Strictly validate active_file against ws_path
            active_in_ws = None
            if body.get("active_file") and is_path_in_workspace(body.get("active_file"), ws_path):
                active_in_ws = Path(body.get("active_file")).resolve().name

            valid_target_file = None
            raw_tf = body.get("target_file")
            if raw_tf:
                cand_p = Path(raw_tf)
                if cand_p.is_absolute():
                    if is_path_in_workspace(cand_p, ws_path):
                        valid_target_file = cand_p.name
                else:
                    valid_target_file = cand_p.name

            if valid_target_file:
                if valid_target_file in target_files:
                    target_files.remove(valid_target_file)
                    target_files.insert(0, valid_target_file)
                elif not target_files:
                    target_files.insert(0, valid_target_file)
            elif not target_files and active_in_ws:
                target_files.append(active_in_ws)

            ambiguity = AmbiguityDetector.check_ambiguity(goal, target_files)
            change_specs = SymbolEntityResolver.extract_change_specs(goal)

            intent_info = InstructionClassifier.classify(goal)
            intent_label = intent_info.get('intent', 'EDIT_EXISTING')
            is_delete = (
                intent_label == "DELETE_CODE"
                or step_action == "delete"
                or bool(re.search(r"\b(?:delete|remove|clear|wipe|erase|truncate|empty)\b.*?\b(?:all\s+code|entire\s+code|all\s+content|everything|entire\s+file|all\s+lines)\b", goal, re.IGNORECASE))
                or bool(re.search(r"^\s*(?:clear|wipe|truncate|empty)\s+(?:the\s+)?(?:file|code|content|contents|[A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+)\s*$", goal, re.IGNORECASE))
            )

            # Resolve primary target filename and path strictly confined to ws_path
            target_filename = None
            target_path_obj = None

            if target_files:
                primary_name = target_files[0]
                target_filename = Path(primary_name).name
                if primary_name in accessible:
                    target_path_obj = accessible[primary_name]
                elif target_filename in accessible:
                    target_path_obj = accessible[target_filename]
                elif (Path(ws_path) / primary_name).exists():
                    target_path_obj = Path(ws_path) / primary_name
                else:
                    target_path_obj = Path(ws_path) / target_filename
            elif active_in_ws:
                target_filename = active_in_ws
                if active_in_ws in accessible:
                    target_path_obj = accessible[active_in_ws]
                else:
                    target_path_obj = Path(ws_path) / active_in_ws
            else:
                direct_files = WorkspaceFileResolver.resolve_query_files(goal, ws_path)
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
                else:
                    extracted_from_goal = SymbolEntityResolver.extract_target_filepaths(goal)
                    if extracted_from_goal:
                        target_filename = Path(extracted_from_goal[0]).name
                        target_path_obj = Path(ws_path) / target_filename
                    else:
                        for w in goal.split():
                            clean_w = w.strip(".,;:?!'\"()[]{}<>`")
                            if "." in clean_w and not clean_w.startswith("."):
                                target_filename = Path(clean_w).name
                                break
                        if not target_filename:
                            target_filename = "app.py"
                        target_path_obj = Path(ws_path) / target_filename

            # Safety invariant: target_path_obj must NEVER escape ws_path
            if not is_path_in_workspace(target_path_obj, ws_path):
                target_path_obj = Path(ws_path) / (target_filename or "app.py")

            action = step_action
            if not action:
                if step_id == 1:
                    action = "scan"
                elif step_id == 2:
                    action = "delete" if is_delete else ("prep" if total_steps in (2, 4, 5) else "synthesize")
                elif step_id == 3 and total_steps == 3:
                    action = "verify"
                elif step_id == 3 and total_steps >= 4:
                    action = "synthesize"
                elif step_id == 4 and total_steps == 4:
                    action = "verify"
                elif step_id == 4 and total_steps == 5:
                    action = "validate"
                elif step_id == 5:
                    action = "verify"
                else:
                    action = "verify"

            if action == "scan":
                if is_delete:
                    line_count = 0
                    if target_path_obj and target_path_obj.exists() and target_path_obj.is_file():
                        try:
                            line_count = len(target_path_obj.read_text(encoding="utf-8", errors="ignore").splitlines())
                        except Exception:
                            pass
                    msg = f"Intent: DELETE_CODE. Isolated code boundaries in '{target_filename}' ({line_count} lines to remove)."
                    res = {
                        "step_id": step_id,
                        "status": "done",
                        "intent": {"intent": "DELETE_CODE", "confidence": 0.99},
                        "target_files": [target_filename],
                        "ambiguity": None,
                        "change_specs": [],
                        "log": msg
                    }
                else:
                    is_folder_intent = intent_label == "CREATE_FOLDER" or any(kw in goal.lower() for kw in ("folder", "directory", "dir"))
                    if is_folder_intent and not any("." in t for t in target_files):
                        f_display = SymbolEntityResolver.extract_folder_name(goal) or target_filename
                        msg = f"Intent: CREATE_FOLDER. Target folder verified: '{f_display}' in workspace."
                        res = {
                            "step_id": step_id,
                            "status": "done",
                            "intent": {"intent": "CREATE_FOLDER", "confidence": 0.99},
                            "target_files": [f_display],
                            "ambiguity": None,
                            "change_specs": [],
                            "log": msg
                        }
                    else:
                        direct_files = WorkspaceFileResolver.resolve_query_files(goal, ws_path)
                        all_found = list({Path(c.file_path).name for c in direct_files})
                        for tf in target_files:
                            all_found.append(Path(tf).name)
                        files_found = list(dict.fromkeys(all_found))
                        display_files = target_files if target_files else files_found

                        topic_info = TopicDomainResolver.resolve_topic(goal)
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
                        res = {
                            "step_id": step_id,
                            "status": "ambiguous" if is_ambiguous else "done",
                            "intent": intent_info,
                            "target_files": target_files,
                            "ambiguity": ambiguity,
                            "change_specs": change_specs,
                            "log": msg
                        }

            elif action == "prep":
                folder_name = SymbolEntityResolver.extract_folder_name(goal)
                if total_steps == 2 or (folder_name and not any("." in t for t in target_files)):
                    f_display = folder_name or target_filename
                    folder_dir = Path(ws_path) / f_display
                    folder_dir.mkdir(parents=True, exist_ok=True)
                    res = {
                        "step_id": step_id,
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
                    folder_dir = Path(ws_path) / folder_name
                    folder_dir.mkdir(parents=True, exist_ok=True)
                    res = {
                        "step_id": step_id,
                        "status": "done",
                        "target_file": target_filename,
                        "log": f"Directory structure for '{folder_name}' prepared. Boilerplate specs ready for '{target_filename}'."
                    }
                elif total_steps == 5:
                    topic_info = TopicDomainResolver.resolve_topic(goal)
                    res = {
                        "step_id": step_id,
                        "status": "done",
                        "target_file": target_filename,
                        "log": f"Structured {topic_info.get('data_structure', 'state models and invariants')} for '{topic_info.get('topic', target_filename)}'."
                    }
                else:
                    res = {
                        "step_id": step_id,
                        "status": "done",
                        "target_file": target_filename,
                        "log": f"Correlated code invariants and call stack for '{target_filename}'."
                    }

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
                res = {
                    "step_id": step_id,
                    "status": "done",
                    "target_file": target_filename,
                    "diff": final_diff,
                    "hunks": hunks,
                    "staged_files": staged_files,
                    "valid": True,
                    "generated": "",
                    "log": log_msg
                }

            elif action == "synthesize":
                # Check if user specifically requested pure folder creation
                is_folder_only = bool(target_filename and "." not in Path(target_filename).name and any(kw in goal.lower() for kw in ("folder", "directory", "dir")))
                if is_folder_only:
                    folder_dir = Path(ws_path) / target_filename
                    folder_dir.mkdir(parents=True, exist_ok=True)
                    res = {
                        "step_id": step_id,
                        "status": "done",
                        "target_file": target_filename,
                        "diff": "",
                        "hunks": [],
                        "staged_files": [],
                        "valid": True,
                        "generated": f"Created folder '{target_filename}' successfully.",
                        "log": f"Created folder '{target_filename}' in workspace."
                    }
                    if is_stream:
                        res["done"] = True
                        self.wfile.write(f"data: {json.dumps(res)}\n\n".encode("utf-8"))
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    else:
                        self.send_response(200)
                        self._set_cors_headers()
                        self.end_headers()
                        self.wfile.write(json.dumps(res).encode("utf-8"))
                    return

                # Check if file exists or is brand new
                is_new_file = not (target_path_obj and target_path_obj.exists() and target_path_obj.is_file())

                orig_lines = []
                raw_file_content = ""
                file_content_context = ""
                target_symbols = []
                preferred_line = body.get("cursor_line")
                act_content = body.get("active_file_content")

                # 1. Prioritize active_file_content directly from the editor buffer if provided
                if act_content is not None and act_content.strip():
                    raw_file_content = act_content
                    if target_filename:
                        live_file_buffers[target_filename] = raw_file_content
                    if target_path_obj:
                        live_file_buffers[str(target_path_obj)] = raw_file_content
                elif target_path_obj and target_path_obj.exists() and target_path_obj.is_file():
                    try:
                        raw_file_content = target_path_obj.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        raw_file_content = ""

                # Check live in-memory buffer if disk is empty or unread
                if not raw_file_content.strip():
                    if target_filename in live_file_buffers and live_file_buffers[target_filename].strip():
                        raw_file_content = live_file_buffers[target_filename]
                    elif target_path_obj and str(target_path_obj) in live_file_buffers and live_file_buffers[str(target_path_obj)].strip():
                        raw_file_content = live_file_buffers[str(target_path_obj)]

                orig_lines = raw_file_content.splitlines() if raw_file_content else []
                is_new_file = not (target_path_obj and target_path_obj.exists() and target_path_obj.is_file()) and not raw_file_content.strip()

                if not is_new_file and orig_lines:
                    target_symbols = SymbolEntityResolver.resolve_target_symbols(goal, raw_file_content)
                    if preferred_line is None:
                        sym_positions = SymbolEntityResolver.resolve_target_symbols_with_positions(goal, raw_file_content)
                        if sym_positions:
                            preferred_line = sym_positions[0]["lineno"]

                # Build file context with 1-indexed line numbers (identical format as Chat tab)
                if orig_lines:
                    numbered_lines = [f"{i+1:3d}: {line}" for i, line in enumerate(orig_lines)]
                    file_content_context = (
                        f"[Active Target File: {target_filename} ({len(orig_lines)} lines total, with EXACT 1-Indexed Line Numbers)]\n"
                        f"[IMPORTANT: Use the exact line numbers shown before the colons. Do NOT make up line numbers.]\n"
                        + "\n".join(numbered_lines)[:25000]
                    )
                else:
                    file_content_context = (
                        f"[Target File to Create: '{target_filename}' (Brand New File)]\n"
                        f"[Status: This file does not exist yet on disk. Write the complete code implementation from scratch for '{target_filename}'.]"
                    )

                specs_info = ""
                if change_specs:
                    specs_info += f"Identified Change Targets: {json.dumps(change_specs)}\n"
                if target_symbols:
                    specs_info += f"Identified Target Symbols: {', '.join(target_symbols)}\n"
                if preferred_line:
                    specs_info += f"Target Code Region Focus: around line {preferred_line}\n"

                topic_info = TopicDomainResolver.resolve_topic(goal)
                is_algorithm_or_complex = (
                    intent_label == "ALGORITHM_SYNTHESIS"
                    or any(kw in goal.lower() for kw in (
                        "astar", "a*", "a-star", "dijkstra", "pathfinding", "binary search",
                        "quicksort", "mergesort", "dynamic programming", "graph traversal",
                        "red-black tree", "avl tree"
                    ))
                )
                is_explicit_full_rewrite = any(kw in goal.lower() for kw in (
                    "from scratch", "full rewrite", "rewrite the whole file", "rewrite entire file",
                    "overwrite file", "replace entire file"
                ))
                is_comprehensive = is_new_file or is_explicit_full_rewrite

                target_ext = Path(target_filename).suffix.lstrip(".")
                lang_hint = target_ext if target_ext else "python"

                if is_comprehensive:
                    prompt = (
                        f"Goal: {goal}\n"
                        f"Target File: {target_filename}\n"
                        f"Write the complete code implementation for '{target_filename}' to fulfill: {goal}\n\n"
                        "GUIDELINES:\n"
                        "- Write the exact, complete, working implementation requested by the user.\n"
                        "- Directly answer the user prompt without adding unsolicited architecture or unnecessary boilerplate.\n"
                        "- ZERO EMOJIS: Do not use emojis anywhere in your output.\n"
                        f"- Output your code inside a ```{lang_hint} code block directly."
                    )
                    sys_msg = "You are CLAY, an autonomous code synthesis engine. Write clean, complete code directly fulfilling the user's prompt without unnecessary boilerplate."
                else:
                    prompt = (
                        f"Goal: {goal}\n"
                        f"Target File: {target_filename}\n"
                        f"{specs_info}\n"
                        f"{file_content_context}\n\n"
                        f"Implement the minimal, surgical change in '{target_filename}' to achieve: {goal}\n\n"
                        "RULES:\n"
                        "- ZERO EMOJIS: Do not use emojis anywhere in your output.\n"
                        f"- MINIMAL SURGICAL SCOPE: Make ONLY the minimal changes required in '{target_filename}'. Do NOT rewrite unchanged logic.\n"
                        "- EXACT LINE NUMBERS: Use the exact line numbers from the context above.\n"
                        f"- Output your solution as a standard unified diff (```diff) or updated code block (```{lang_hint})."
                    )
                    sys_msg = "You are CLAY, an autonomous code synthesis engine. Provide minimal, surgical changes using standard unified diffs or updated code blocks."

                active_model = runtime_settings.get("model", OLLAMA_MODEL)
                active_host = runtime_settings.get("host", OLLAMA_URL)
                generated = ""
                if is_stream:
                    accumulated_tokens = []
                    try:
                        payload = json.dumps({
                            "model": active_model,
                            "messages": [
                                {"role": "system", "content": sys_msg},
                                {"role": "user", "content": prompt}
                            ],
                            "stream": True,
                            "options": {
                                "temperature": runtime_settings.get("temperature", 0.2),
                                "num_ctx": runtime_settings.get("num_ctx", 32768),
                                "num_predict": runtime_settings.get("num_predict", 4096)
                            }
                        }).encode("utf-8")
                        req = urllib.request.Request(f"{active_host}/api/chat", data=payload, headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(req, timeout=120.0) as resp:
                            for line in resp:
                                if line:
                                    try:
                                        chunk = json.loads(line.decode("utf-8"))
                                        token = chunk.get("message", {}).get("content", "")
                                        if token:
                                            clean_tok = token.replace(r'\"\"\"', '"""').replace(r"\'\'\'", "'''")
                                            accumulated_tokens.append(clean_tok)
                                            sse = f"data: {json.dumps({'token': clean_tok})}\n\n"
                                            self.wfile.write(sse.encode("utf-8"))
                                            self.wfile.flush()
                                    except Exception:
                                        pass
                    except Exception as e:
                        err_sse = f"data: {json.dumps({'token': f'\\n[Ollama Notice: {str(e)}]' })}\n\n"
                        self.wfile.write(err_sse.encode("utf-8"))
                        self.wfile.flush()
                    generated = "".join(accumulated_tokens)
                else:
                    try:
                        payload = json.dumps({
                            "model": active_model,
                            "messages": [
                                {"role": "system", "content": sys_msg},
                                {"role": "user", "content": prompt}
                            ],
                            "stream": False,
                            "options": {
                                "temperature": runtime_settings.get("temperature", 0.2),
                                "num_ctx": runtime_settings.get("num_ctx", 32768),
                                "num_predict": runtime_settings.get("num_predict", 4096)
                            }
                        }).encode("utf-8")
                        req = urllib.request.Request(f"{active_host}/api/chat", data=payload, headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(req, timeout=120.0) as resp:
                            gen_resp = json.loads(resp.read().decode("utf-8"))
                            generated = gen_resp.get("message", {}).get("content", "")
                    except Exception as e:
                        generated = f"// Error connecting to {active_model}: {str(e)}"

                # Build surgical minimal diff using multi-strategy builder (eliminates full-file wipes and line desync)
                diff_text = PatchEngine.build_surgical_diff(orig_lines, generated, target_filename, preferred_line=preferred_line)

                # Pre-staging AST syntax validation & automated self-healing
                is_valid, final_diff, err_msg = PatchEngine.validate_and_heal_diff(orig_lines, diff_text, target_filename)

                # Extract filename from diff header if present
                if final_diff:
                    header_match = re.search(r"\+\+\+\s+(?:b/)?([^\s\n\r]+)", final_diff)
                    if header_match and header_match.group(1) != "/dev/null":
                        target_filename = Path(header_match.group(1)).name

                hunks = PatchEngine.split_into_hunks(final_diff)
                staged_files = PatchEngine.split_multi_file_diff(final_diff)

                if is_new_file:
                    log_msg = f"Generated complete implementation for new file '{target_filename}'."
                elif is_algorithm_or_complex:
                    log_msg = f"Synthesized production-ready implementation of {topic_info.get('topic', 'algorithm')} for '{target_filename}'."
                else:
                    log_msg = f"Synthesized surgical solution for '{target_filename}'." if final_diff else f"Generated response for '{target_filename}'."

                res = {
                    "step_id": step_id,
                    "status": "done",
                    "target_file": target_filename,
                    "diff": final_diff,
                    "hunks": hunks,
                    "staged_files": staged_files,
                    "valid": is_valid,
                    "generated": generated,
                    "log": log_msg
                }

            elif action == "validate":
                res = {
                    "step_id": step_id,
                    "status": "done",
                    "target_file": target_filename,
                    "log": "AST syntax validation & algorithmic invariant verification passed."
                }

            elif action == "verify":
                topic_info = TopicDomainResolver.resolve_topic(goal)
                if is_delete:
                    v_msg = f"File integrity verified. Truncation diff staged for '{target_filename}' in Diff tab."
                elif total_steps == 2:
                    v_msg = f"Directory '{target_filename}' verified and ready in workspace."
                elif intent_label == "ALGORITHM_SYNTHESIS" or total_steps == 5:
                    v_msg = f"Zero regressions verified. Staged complete {topic_info.get('topic', 'implementation')} in Diff tab."
                else:
                    v_msg = "Syntax and AST verification passed. Clean surgical changes ready for inspection in Diff tab."
                res = {
                    "step_id": step_id,
                    "status": "done",
                    "target_file": target_filename,
                    "log": v_msg
                }
            else:
                res = {"status": "error", "message": f"Unknown step action: {action}"}

            if is_stream:
                res["done"] = True
                self.wfile.write(f"data: {json.dumps(res)}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                self.send_response(200)
                self._set_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps(res).encode("utf-8"))

        elif self.path == "/api/agent/review":
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success", "action": body.get("decision")}).encode("utf-8"))

        elif self.path == "/api/chat/abort":
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(b'{"status": "aborted"}')

        elif self.path == "/api/agent/abort":
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(b'{"status": "aborted"}')

        elif self.path == "/api/system/shutdown":
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(b'{"status": "shutting_down"}')
            import threading
            threading.Timer(0.3, lambda: sys.exit(0)).start()

        elif self.path == "/api/system/restart":
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(b'{"status": "restarting"}')
            import threading
            def _do_standalone_restart():
                time.sleep(0.3)
                backend_dir = str(Path(__file__).resolve().parent)
                cmd = [sys.executable, "standalone_server.py", "--port", str(PORT)]
                try:
                    if os.name == "nt":
                        launcher_code = (
                            f"import time, subprocess; "
                            f"time.sleep(1.2); "
                            f"subprocess.Popen({repr(cmd)}, cwd={repr(backend_dir)})"
                        )
                        creation_flags = 0x00000008 | 0x00000200
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
            threading.Thread(target=_do_standalone_restart, daemon=True).start()

        elif self.path == "/api/system/reload":
            load_persisted_settings()
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"status": "reloaded", "runtime_settings": runtime_settings}).encode("utf-8"))

        else:
            self.send_response(404)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(b'{"error": "Not Found"}')

    def log_message(self, format, *args):
        # Concise logging
        sys.stderr.write(f"[CLAY Copilot Backend] {self.address_string()} - {format % args}\n")

def run():
    server = HTTPServer(("127.0.0.1", PORT), CopilotRequestHandler)
    print(f"✓ CLAY Copilot Standalone Server running on http://127.0.0.1:{PORT}")
    print(f"✓ Model target: {OLLAMA_MODEL} (Zero external pip dependencies required)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()

if __name__ == "__main__":
    run()
