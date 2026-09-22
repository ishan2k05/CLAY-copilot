import unittest
import asyncio
from pathlib import Path
from app.parser.symbols import CodeChunk
from app.retrieval.hybrid_search import HybridSearchEngine
from app.retrieval.file_resolver import WorkspaceFileResolver
from app.index.vector_store import LocalVectorStore
from app.index.graph_store import SQLiteGraphStore
from app.main import app, runtime_settings
from fastapi.testclient import TestClient

class TestRAGAndSettings(unittest.TestCase):

    def setUp(self):
        self.vector_store = LocalVectorStore()
        self.graph_store = SQLiteGraphStore(db_path=":memory:")
        asyncio.run(self.graph_store.init_db())
        self.engine = HybridSearchEngine(self.vector_store, self.graph_store)
        self.client = TestClient(app)

    def test_context_prompt_token_budget_cap(self):
        chunks = [
            CodeChunk(
                chunk_id=f"chk_{i}",
                file_path=f"/workspace/app/module_{i}.py",
                content="def test_function():\n" + "    x = 1\n" * 40,
                start_line=1,
                end_line=41,
                symbol_name=f"test_func_{i}",
                symbol_type="function"
            )
            for i in range(10)
        ]

        prompt = HybridSearchEngine.build_context_prompt(chunks, max_chars=800, workspace_root="/workspace")
        self.assertLessEqual(len(prompt), 800)
        self.assertIn("[app/module_0.py:1-41 (test_func_0)]", prompt)
        self.assertIn("... [truncated]", prompt)

    def test_security_mode_filtering(self):
        chunks = [
            CodeChunk(
                chunk_id="c1",
                file_path="/safe/workspace/src/app.py",
                content="print('safe')",
                start_line=1,
                end_line=1,
                symbol_name="app",
                symbol_type="module"
            ),
            CodeChunk(
                chunk_id="c2",
                file_path="/safe/workspace/.env",
                content="SECRET_KEY=12345",
                start_line=1,
                end_line=1,
                symbol_name="secret",
                symbol_type="module"
            ),
            CodeChunk(
                chunk_id="c3",
                file_path="/etc/passwd",
                content="root:x:0:0",
                start_line=1,
                end_line=1,
                symbol_name="system",
                symbol_type="module"
            )
        ]

        filtered = HybridSearchEngine.filter_chunks_by_security(
            chunks,
            security_mode="sandboxed",
            workspace_root="/safe/workspace"
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].chunk_id, "c1")

    def test_settings_api_get_and_post(self):
        res = self.client.get("/api/settings")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("temperature", data)
        self.assertIn("num_ctx", data)
        self.assertIn("num_predict", data)
        self.assertIn("security_mode", data)
        self.assertIn("auto_execution", data)
        self.assertIn("allow_network", data)
        self.assertIn("connection_type", data)

        payload = {
            "temperature": 0.35,
            "num_ctx": 65536,
            "num_predict": 4096,
            "security_mode": "strict",
            "auto_execution": "safe",
            "allow_network": False,
            "queued_mode": "immediate",
            "connection_type": "cloud",
            "api_key": "sk_ollama_secret_key_123"
        }
        post_res = self.client.post("/api/settings", json=payload)
        self.assertEqual(post_res.status_code, 200)
        updated = post_res.json()
        self.assertEqual(updated["temperature"], 0.35)
        self.assertEqual(updated["num_ctx"], 65536)
        self.assertEqual(updated["num_predict"], 4096)
        self.assertEqual(updated["security_mode"], "strict")
        self.assertEqual(updated["auto_execution"], "safe")
        self.assertFalse(updated["allow_network"])
        self.assertEqual(updated["queued_mode"], "immediate")
        self.assertEqual(updated["connection_type"], "cloud")
        self.assertEqual(updated["api_key"], "sk_ollama_secret_key_123")
        self.assertEqual(updated["host"], "https://ollama.com/api")

        # Reset back
        self.client.post("/api/settings", json={
            "temperature": 0.20,
            "num_ctx": 32768,
            "num_predict": 2048,
            "security_mode": "sandboxed",
            "auto_execution": "always",
            "connection_type": "local",
            "api_key": "",
            "host": "http://localhost:11434",
            "allow_network": True,
            "queued_mode": "queue"
        })

    def test_workspace_file_resolver_fuzzy_match_typo(self):
        from app.retrieval.file_resolver import WorkspaceFileResolver
        backend_dir = str(Path(__file__).resolve().parent.parent)

        # Fuzzy match with typo "soloution.py"
        chunks = WorkspaceFileResolver.resolve_query_files(
            "whast in teh soloution.py",
            backend_dir,
            security_mode="sandboxed"
        )
        self.assertGreaterEqual(len(chunks), 1)
        self.assertTrue(any("solution.py" in c.file_path for c in chunks))
        matched = [c for c in chunks if "solution.py" in c.file_path][0]
        self.assertTrue("calculator" in matched.content or "print_pattern" in matched.content or len(matched.content) > 0)

    def test_access_and_catalog_query_detection(self):
        from app.retrieval.file_resolver import WorkspaceFileResolver
        backend_dir = str(Path(__file__).resolve().parent.parent)

        # Access inquiry query
        is_access = WorkspaceFileResolver.is_access_or_catalog_query("how much access do you have to my repository?")
        self.assertTrue(is_access)

        summary = WorkspaceFileResolver.get_workspace_catalog_summary(backend_dir, security_mode="sandboxed")
        self.assertIn("[Workspace Repository Context]", summary)
        self.assertIn("SANDBOXED", summary)
        self.assertIn("solution.py", summary)

    def test_repo_path_sync_in_settings(self):
        res = self.client.post("/api/settings", json={"repo_path": "/tmp/custom_repo"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["repo_path"], "/tmp/custom_repo")

        get_res = self.client.get("/api/settings")
        self.assertEqual(get_res.json()["repo_path"], "/tmp/custom_repo")

        # Reset back
        self.client.post("/api/settings", json={"repo_path": str(Path(__file__).resolve().parent.parent)})

    def test_essential_repo_tree_endpoint(self):
        backend_dir = str(Path(__file__).resolve().parent.parent)
        res = self.client.get(f"/api/repo/tree?path={backend_dir}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertGreater(data["stats"]["total_files"], 0)
        self.assertGreater(data["stats"]["total_dirs"], 0)

        # Ensure __pycache__ and venv are excluded
        def check_no_excluded(nodes):
            for n in nodes:
                self.assertNotIn("__pycache__", n["name"])
                self.assertNotIn("venv", n["name"])
                self.assertFalse(n["name"].endswith(".pyc"))
                if n["type"] == "directory":
                    check_no_excluded(n.get("children", []))

        check_no_excluded(data["tree"])

        # Ensure essential code files like solution.py and requirements.txt are present
        all_file_names = []
        def collect_files(nodes):
            for n in nodes:
                if n["type"] == "file":
                    all_file_names.append(n["name"])
                else:
                    collect_files(n.get("children", []))
        collect_files(data["tree"])
        self.assertIn("solution.py", all_file_names)
        self.assertIn("requirements.txt", all_file_names)

    def test_repo_update_and_summary_query(self):
        backend_dir = str(Path(__file__).resolve().parent.parent)
        
        # Test typo 'sumamrize repo fo rme'
        self.assertTrue(WorkspaceFileResolver.is_access_or_catalog_query("sumamrize repo fo rme"))
        self.assertTrue(WorkspaceFileResolver.is_access_or_catalog_query("summarize repo"))
        self.assertTrue(WorkspaceFileResolver.is_access_or_catalog_query("explain codebase"))

        # Test catalog summary includes entrypoints
        summary = WorkspaceFileResolver.get_workspace_catalog_summary(backend_dir, "sandboxed")
        self.assertIn("Workspace Repository Context", summary)
        self.assertIn("Accessible Files", summary)
        self.assertIn("Key Repository Entrypoints", summary)

        # Test 'whast is app.py' resolves package directory 'app'
        chunks = WorkspaceFileResolver.resolve_query_files("whast is app.py", backend_dir, "sandboxed")
        self.assertGreater(len(chunks), 0)
        chunk_names = [c.symbol_name for c in chunks]
        self.assertTrue(any("app" in name for name in chunk_names))

    def test_mermaid_graph_fallback_when_empty_or_new_repo(self):
        # Querying an unknown / empty repo should return available: False and no AuthRoute mock
        res = self.client.get("/api/graph/mermaid?path=/tmp/empty_test_repo")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data.get("available", True))
        self.assertNotIn("AuthRoute", data.get("mermaid", ""))
        self.assertIn("No SQLite code graph available", data.get("message", ""))

if __name__ == "__main__":
    unittest.main()


