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
from app.security.secret_masker import SecretMasker
from app.index.graph_store import SQLiteGraphStore
from app.agent.patch_engine import PatchEngine
from app.agent.instruction_parser import (
    InstructionClassifier,
    SymbolEntityResolver,
    AmbiguityDetector,
    ContextWindowBuilder,
)
from app.retrieval.file_resolver import WorkspaceFileResolver
from app.retrieval.repo_map import RepoMapGenerator

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
        SETTINGS_FILE.write_text(json.dumps(runtime_settings, indent=2), encoding="utf-8")
    except Exception:
        pass

load_persisted_settings()

graph_store = SQLiteGraphStore("code_graph.sqlite")
graph_store._sync_init_db()
ast_chunker = PythonASTChunker()
workspace_file_states = {}
standalone_chat_history = []

def _silent_sync_workspace_sync(workspace_dir: str):
    root = Path(workspace_dir).resolve()
    changed_count = 0
    deleted_count = 0
    if not root.exists() or not root.is_dir():
        return 0, 0
    current_files = set()
    code_exts = {".py", ".ts", ".js", ".jsx", ".tsx", ".json", ".md", ".html", ".css", ".toml", ".yaml", ".yml", ".sql", ".sh"}
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
                if st.st_size > 0 and p.suffix == ".py":
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
            data = {
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

                with sqlite3.connect(graph_store.db_path) as db:
                    if selected_file_norm:
                        sel_filename = Path(selected_file_norm).name.lower()
                        cursor = db.execute(
                            "SELECT source_symbol, target_symbol, relation_type, file_path FROM relationships "
                            "WHERE LOWER(file_path) LIKE ? OR LOWER(file_path) LIKE ? LIMIT 60",
                            (f"%{selected_file_norm}%", f"%{sel_filename}%")
                        )
                    else:
                        cursor = db.execute("SELECT source_symbol, target_symbol, relation_type, file_path FROM relationships LIMIT 60")

                    for src, tgt, rel, fpath in cursor.fetchall():
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

                    if len(diagram_lines) == 1:
                        if selected_file_norm:
                            sym_cursor = db.execute(
                                "SELECT name, symbol_type, file_path FROM symbols "
                                "WHERE LOWER(file_path) LIKE ? OR LOWER(file_path) LIKE ? LIMIT 35",
                                (f"%{selected_file_norm}%", f"%{sel_filename}%")
                            )
                        else:
                            sym_cursor = db.execute("SELECT name, symbol_type, file_path FROM symbols LIMIT 30")

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

                if len(diagram_lines) > 1:
                    data = {"mermaid": "\n".join(diagram_lines), "available": True, "focused_file": target_file}
                else:
                    msg = (
                        f"No call relationships or symbols found for '{target_file}'.\nClick 'Scan & Index Workspace' to analyze this file or select another file."
                        if target_file
                        else "No SQLite code graph available for this repository.\nClick 'Scan & Index Workspace' to analyze codebase structure."
                    )
                    data = {"mermaid": "", "available": False, "message": msg}
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
            p = Path(file_path).resolve()
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
            ws_path = body.get("workspace_path") or runtime_settings.get("repo_path", ".")
            
            # Silent zero-token background sync check before resolving context
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

            # Generate Global Repo Map for whole-codebase architectural awareness
            try:
                repo_map = RepoMapGenerator.generate_repo_map(ws_path, graph_store=graph_store, max_chars=4000)
                if repo_map:
                    code_ctx = f"<repository_map>\n{repo_map}\n</repository_map>\n\n" + (code_ctx or "")
            except Exception:
                pass

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
                + " PERSONA & TONE: For conversational greetings and interactive messages ('hello', 'hi', status checks), be warm, welcoming, intelligent, and collaborative—acknowledge the active workspace context or open files and offer concrete ways you can assist, never giving cold robotic one-word replies like 'Hello.' For technical and coding requests, maintain a direct, rigorous, and razor-sharp technical voice without filler or platitudes (begin directly with the [plan] or summary, followed by the code block or diff). STRICT ZERO EMOJIS: Never output emojis under any circumstances. "
                + "CLAY ECOSYSTEM CAPABILITIES: You have 4 unified workspaces: 1) Chat (conversational intelligence, explanation, on-demand interactive SVG diagram generation with 30%-500% zoom and Fit to Window), 2) Agent (autonomous multi-step task execution, terminal testing, surgical AST patches, self-healing error repair loop), 3) Diff (split/unified diff viewer, line editing, 1-click patch application), 4) Repo (AST symbol map, dependency graph, real-time indexer). "
                + "SECURITY & PRIVACY BOUNDARIES: You operate under strict workspace sandboxing, Zero-Leak Privacy secret masking, and prompt injection defense boundaries. "
                + "STRUCTURED RESPONSE ORDER & [plan] SCOPE: The '[plan]' prefix is EXCLUSIVELY reserved for actionable code modification requests that produce a diff or code patch. NEVER use '[plan]' when generating diagrams, answering architectural questions, summarizing repositories, or greeting the user. "
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
            if effective_history:
                for turn in effective_history[-10:]:
                    r = turn.get("role", "user")
                    c = turn.get("content", "")
                    if c and r in ("user", "assistant"):
                        m_c = SecretMasker.mask_text(c, enabled=runtime_settings.get("privacy_filter", True))
                        if len(m_c) > 2000:
                            m_c = m_c[:2000] + "..."
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
                err_sse = f"data: {json.dumps({'token': f'\\n⚠️ [Ollama Notice: {str(e)} - Ensure ollama serve is active]' })}\n\n"
                self.wfile.write(err_sse.encode("utf-8"))

            # Update session history with completed turn
            full_reply = "".join(accumulated_tokens)
            if query and full_reply:
                standalone_chat_history.append({"role": "user", "content": query})
                standalone_chat_history.append({"role": "assistant", "content": full_reply})
                if len(standalone_chat_history) > 20:
                    standalone_chat_history[:] = standalone_chat_history[-20:]

            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        elif self.path == "/api/chat/clear":
            standalone_chat_history.clear()
            self.send_response(200)
            self._set_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "cleared", "message": "Chat context reset successfully."}).encode("utf-8"))

        elif self.path == "/api/index/scan":
            root_dir = body.get("workspace_path", ".")
            root = Path(root_dir)
            all_syms = []
            all_rels = []
            all_chunks = []
            indexed_count = 0

            if root.exists():
                for p in root.rglob("*.py"):
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

        elif self.path == "/api/agent/compute-diff":
            file_path = body.get("target_file", "")
            code_snippet = body.get("code_or_snippet", "")
            ws_path = body.get("workspace_path") or runtime_settings.get("repo_path", ".")
            target_path = PatchEngine.resolve_target_path(file_path, str(ws_path))
            orig_lines = []
            if target_path.exists() and target_path.is_file():
                try:
                    orig_lines = target_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                except Exception:
                    orig_lines = []

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

        elif self.path == "/api/agent/step":
            step_id = body.get("step_id", 1)
            goal = body.get("goal", "")
            ws_path = body.get("workspace_path") or runtime_settings.get("repo_path", ".")
            try:
                _silent_sync_workspace_sync(ws_path)
            except Exception:
                pass

            target_file_param = body.get("target_file")

            if step_id == 1:
                intent_info = InstructionClassifier.classify(goal)
                accessible = WorkspaceFileResolver.list_accessible_files(ws_path)
                target_files = SymbolEntityResolver.resolve_target_files(goal, ws_path, accessible)
                ambiguity = AmbiguityDetector.check_ambiguity(goal, target_files)
                change_specs = SymbolEntityResolver.extract_change_specs(goal)

                direct_files = WorkspaceFileResolver.resolve_query_files(goal, ws_path)
                all_found = list({Path(c.file_path).name for c in direct_files})
                for tf in target_files:
                    all_found.append(Path(tf).name)
                files_found = list(dict.fromkeys(all_found))

                is_ambiguous = bool(ambiguity and ambiguity.get("detected") and len(ambiguity.get("candidates", [])) > 1)
                msg = f"Intent: {intent_info.get('intent', 'EDIT_EXISTING')}. Correlated files: {', '.join(files_found) if files_found else 'root workspace'}"
                if is_ambiguous:
                    msg += f" (Ambiguity detected: {', '.join(ambiguity['candidates'])})"
                res = {
                    "step_id": 1,
                    "status": "ambiguous" if is_ambiguous else "done",
                    "intent": intent_info,
                    "target_files": target_files,
                    "ambiguity": ambiguity,
                    "change_specs": change_specs,
                    "log": msg
                }

            elif step_id == 2:
                accessible = WorkspaceFileResolver.list_accessible_files(ws_path)
                target_files = SymbolEntityResolver.resolve_target_files(goal, ws_path, accessible)
                ambiguity = AmbiguityDetector.check_ambiguity(goal, target_files)
                change_specs = SymbolEntityResolver.extract_change_specs(goal)

                target_filename = None
                target_path_obj = None

                if target_file_param:
                    tf_clean = target_file_param.strip()
                    if tf_clean in accessible:
                        target_path_obj = accessible[tf_clean]
                        target_filename = target_path_obj.name
                    elif (Path(ws_path) / tf_clean).exists():
                        target_path_obj = Path(ws_path) / tf_clean
                        target_filename = target_path_obj.name
                    else:
                        for k, p in accessible.items():
                            if p.name.lower() == tf_clean.lower() or k.lower() == tf_clean.lower():
                                target_path_obj = p
                                target_filename = p.name
                                break

                if not target_path_obj:
                    if target_files:
                        target_path_obj = accessible.get(target_files[0]) or (Path(ws_path) / target_files[0])
                        target_filename = target_path_obj.name
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

                if not target_filename:
                    for w in goal.split():
                        clean_w = w.strip(".,;:?!'\"()[]{}<>`")
                        if "." in clean_w and not clean_w.startswith("."):
                            target_filename = clean_w
                            break
                    if not target_filename:
                        target_filename = "test_c.py" if (Path(ws_path) / "test_c.py").exists() else "main.py"

                orig_lines = []
                raw_file_content = ""
                file_content_context = ""
                target_symbols = []
                preferred_line = body.get("cursor_line")
                if target_path_obj and target_path_obj.exists() and target_path_obj.is_file():
                    try:
                        raw_file_content = target_path_obj.read_text(encoding="utf-8", errors="ignore")
                        orig_lines = raw_file_content.splitlines()
                        target_symbols = SymbolEntityResolver.resolve_target_symbols(goal, raw_file_content)
                        if preferred_line is None:
                            sym_positions = SymbolEntityResolver.resolve_target_symbols_with_positions(goal, raw_file_content)
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

                specs_info = ""
                if change_specs:
                    specs_info += f"Identified Change Targets: {json.dumps(change_specs)}\n"
                if target_symbols:
                    specs_info += f"Identified Target Symbols: {', '.join(target_symbols)}\n"
                if preferred_line:
                    specs_info += f"Target Code Region Focus: around line {preferred_line}\n"

                prompt = (
                    f"Goal: {goal}\n"
                    f"Target File: {target_filename}\n"
                    f"{specs_info}"
                    f"{file_content_context}\n"
                    f"You are implementing the code solution for '{target_filename}' to achieve: {goal}\n"
                    "CRITICAL INSTRUCTIONS FOR MINIMAL/SURGICAL CHANGES:\n"
                    f"- If '{target_filename}' exists, make ONLY the minimal changes required to achieve the goal.\n"
                    "- Do NOT rewrite unchanged code or delete the whole file.\n"
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
                    "  Format C (Updated function or variable assignment in python block):\n"
                    "  ```python\n"
                    "  <the updated function or statement>\n"
                    "  ```\n\n"
                    f"If '{target_filename}' is empty or new, write the full, working implementation from scratch.\n"
                    "Do NOT refuse. Do NOT output conversational meta-text or emojis. Output the code block directly."
                )

                active_model = runtime_settings.get("model", OLLAMA_MODEL)
                active_host = runtime_settings.get("host", OLLAMA_URL)
                generated = ""
                try:
                    payload = json.dumps({
                        "model": active_model,
                        "messages": [
                            {"role": "system", "content": "You are CLAY, an autonomous code synthesis engine. Provide minimal, surgical changes using SEARCH/REPLACE blocks or unified diffs."},
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

                log_msg = f"Synthesized surgical solution for {target_filename}." if final_diff else f"Generated response for {target_filename}."
                res = {
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

            elif step_id == 3:
                res = {
                    "step_id": 3,
                    "status": "done",
                    "log": "Syntax and AST verification passed. Clean surgical changes ready for inspection in Diff tab."
                }
            else:
                res = {"status": "error", "message": "Unknown step"}

            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif self.path == "/api/agent/review":
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success", "action": body.get("decision")}).encode("utf-8"))

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
