import unittest
import asyncio
import sys
from pathlib import Path

_backend_dir = Path(__file__).resolve().parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))
_root_dir = _backend_dir.parent
if str(_root_dir) not in sys.path:
    sys.path.insert(0, str(_root_dir))

from app.parser.ast_chunker import PythonASTChunker
from app.parser.symbols import CodeChunk
from app.security.secret_masker import SecretMasker
from app.index.graph_store import SQLiteGraphStore
from app.index.vector_store import LocalVectorStore
from app.retrieval.hybrid_search import HybridSearchEngine
from app.agent.planner import AgentPlanner
from app.agent.patch_engine import PatchEngine
from app.agent.tools.terminal_agent import TerminalAgent
from app.llm.gemma_provider import OllamaGemmaProvider

TEST_DB = "test_integration_graph.sqlite"

class TestFullIntegration(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.db_path = TEST_DB
        if Path(self.db_path).exists():
            Path(self.db_path).unlink()

    async def asyncTearDown(self):
        if Path(self.db_path).exists():
            Path(self.db_path).unlink()

    async def test_end_to_end_indexing_and_hybrid_retrieval(self):
        # 1. Simulate Repository Files
        auth_code = '''
class AuthService:
    """Handles authentication, JWT signing, and token verification."""
    def __init__(self, user_repo):
        self.repo = user_repo

    def authenticate_user(self, username, password):
        """Validates credentials and issues JWT token."""
        user = self.repo.find_by_username(username)
        return user
'''
        chunker = PythonASTChunker()
        symbols, relationships, chunks = chunker.parse_file("backend/auth/service.py", auth_code)

        # 2. Populate SQLite Graph
        gstore = SQLiteGraphStore(self.db_path)
        await gstore.init_db()
        await gstore.insert_batch(symbols, relationships)

        # 3. Populate Vector Store
        vstore = LocalVectorStore()
        await vstore.add_chunks(chunks)

        # 4. Perform Hybrid Search
        engine = HybridSearchEngine(vstore, gstore)
        results = await engine.search("authenticate user JWT credentials", top_k=2)

        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0].symbol_name, "AuthService.authenticate_user")

        # 5. Build Context Prompt
        context = HybridSearchEngine.build_context_prompt(results)
        self.assertIn("AuthService.authenticate_user", context)
        self.assertIn("Validates credentials", context)

    async def test_mermaid_graph_generation(self):
        gstore = SQLiteGraphStore(self.db_path)
        await gstore.init_db()

        auth_code = '''
class PaymentRouter:
    def checkout(self):
        return PaymentService.process()
'''
        chunker = PythonASTChunker()
        syms, rels, _ = chunker.parse_file("routes/pay.py", auth_code)
        await gstore.insert_batch(syms, rels)

        import sqlite3
        with sqlite3.connect(self.db_path) as db:
            cursor = db.execute("SELECT source_symbol, target_symbol, relation_type FROM relationships")
            rows = cursor.fetchall()
            self.assertTrue(any("PaymentService.process" in r[1] for r in rows))

    async def test_agent_planner_generates_steps(self):
        provider = OllamaGemmaProvider()
        planner = AgentPlanner(provider)
        plan = await planner.create_plan("Add user password reset endpoint", "Auth module with User model")

        self.assertEqual(plan["total_steps"], 3)
        self.assertEqual(plan["steps"][0]["status"], "done")
        self.assertEqual(plan["steps"][1]["status"], "active")

    def test_patch_engine_validation(self):
        valid = "def safe():\n    return True\n"
        invalid = "def broken(:\n    return\n"
        self.assertTrue(PatchEngine.validate_python_syntax(valid))
        self.assertFalse(PatchEngine.validate_python_syntax(invalid))

    async def test_terminal_agent_security_guard(self):
        agent = TerminalAgent(".")
        res = await agent.run_command("sudo rm -rf /etc")
        self.assertFalse(res["passed"])
        self.assertIn("Execution blocked", res["stderr"])

if __name__ == "__main__":
    unittest.main()
