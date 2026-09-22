#!/usr/bin/env python3
"""
Codebase Copilot - Zero-Dependency Standalone Server
Uses standard Python library (http.server, urllib, sqlite3, ast)
to provide the full backend API on port 8765 with zero pip installs!
"""

import sys
import json
import urllib.request
import urllib.error
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

# Import local modules
from app.parser.ast_chunker import PythonASTChunker
from app.security.secret_masker import SecretMasker
from app.index.graph_store import SQLiteGraphStore

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "gemma4:31b-cloud"
PORT = 8765

graph_store = SQLiteGraphStore("code_graph.sqlite")
graph_store._sync_init_db()
ast_chunker = PythonASTChunker()

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
                from app.main import graph_store, runtime_settings, DEFAULT_WORKSPACE
                target_path = runtime_settings.get("repo_path") or DEFAULT_WORKSPACE
                target_norm = str(Path(target_path).resolve())
                diagram_lines = ["graph TD"]
                with sqlite3.connect(graph_store.db_path) as db:
                    cursor = db.execute("SELECT source_symbol, target_symbol, relation_type, file_path FROM relationships LIMIT 30")
                    for src, tgt, rel, fpath in cursor.fetchall():
                        if fpath:
                            try:
                                if not str(Path(fpath).resolve()).startswith(target_norm):
                                    continue
                            except Exception:
                                pass
                        clean_src = src.split("#")[-1].replace(".", "_")
                        clean_tgt = tgt.split("#")[-1].replace(".", "_")
                        diagram_lines.append(f"  {clean_src} -->|{rel}| {clean_tgt}")
                if len(diagram_lines) > 1:
                    data = {"mermaid": "\n".join(diagram_lines), "available": True}
                else:
                    data = {"mermaid": "", "available": False, "message": "No SQLite code graph available for this repository.\nClick 'Scan & Index Workspace' to analyze codebase structure."}
            except Exception:
                data = {"mermaid": "", "available": False, "message": "No SQLite code graph available for this repository.\nClick 'Scan & Index Workspace' to analyze codebase structure."}
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

        else:
            self.send_response(404)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(b'{"error": "Not Found"}')

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            body = json.loads(post_body)
        except Exception:
            body = {}

        if self.path == "/api/chat":
            query = body.get("query", "")
            context_file = body.get("context_file")
            code_ctx = ""
            if context_file and Path(context_file).exists():
                if not SecretMasker.is_file_sensitive(context_file):
                    code_ctx = Path(context_file).read_text(encoding="utf-8", errors="ignore")

            sanitized_query = SecretMasker.mask_text(query)
            sanitized_ctx = SecretMasker.mask_text(code_ctx)

            self.send_response(200)
            self._set_cors_headers(content_type="text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            # Connect to Ollama
            ollama_payload = json.dumps({
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": "You are Codebase Copilot. Provide architectural analysis and clean diffs."},
                    {"role": "user", "content": f"Context:\n{sanitized_ctx}\n\nTask: {sanitized_query}"}
                ],
                "stream": True
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/chat",
                data=ollama_payload,
                headers={"Content-Type": "application/json"}
            )

            try:
                with urllib.request.urlopen(req, timeout=120.0) as resp:
                    for line in resp:
                        if line:
                            try:
                                chunk = json.loads(line.decode("utf-8"))
                                token = chunk.get("message", {}).get("content", "")
                                if token:
                                    sse = f"data: {json.dumps({'token': token})}\n\n"
                                    self.wfile.write(sse.encode("utf-8"))
                                    self.wfile.flush()
                            except Exception:
                                pass
            except Exception as e:
                err_sse = f"data: {json.dumps({'token': f'\\n⚠️ [Ollama Notice: {str(e)} - Ensure ollama serve is active]' })}\n\n"
                self.wfile.write(err_sse.encode("utf-8"))

            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        elif self.path == "/api/index/scan":
            root_dir = body.get("workspace_path", ".")
            root = Path(root_dir)
            all_syms = []
            all_rels = []
            indexed_count = 0

            if root.exists():
                for p in root.rglob("*.py"):
                    if any(part.startswith(".") or part in ["venv", "node_modules", "dist"] for part in p.parts):
                        continue
                    if SecretMasker.is_file_sensitive(str(p)):
                        continue
                    try:
                        content = p.read_text(encoding="utf-8", errors="ignore")
                        syms, rels, _ = ast_chunker.parse_file(str(p), content)
                        all_syms.extend(syms)
                        all_rels.extend(rels)
                        indexed_count += 1
                    except Exception:
                        continue

            graph_store._sync_insert_batch(all_syms, all_rels)

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
        sys.stderr.write(f"[Copilot Backend] {self.address_string()} - {format % args}\n")

def run():
    server = HTTPServer(("127.0.0.1", PORT), CopilotRequestHandler)
    print(f"✓ Codebase Copilot Standalone Server running on http://127.0.0.1:{PORT}")
    print(f"✓ Model target: {OLLAMA_MODEL} (Zero external pip dependencies required)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()

if __name__ == "__main__":
    run()
