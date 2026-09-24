import unittest
import asyncio
import tempfile
import shutil
from pathlib import Path
from app.parser.symbols import CodeChunk, SymbolNode, RelationshipEdge
from app.index.vector_store import LocalVectorStore
from app.index.graph_store import SQLiteGraphStore
from app.retrieval.hybrid_search import HybridSearchEngine
from app.retrieval.repo_map import RepoMapGenerator

class TestRepoMapAndBM25(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test_bm25.sqlite")
        self.graph_store = SQLiteGraphStore(self.db_path)
        await self.graph_store.init_db()

    async def asyncTearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_sqlite_bm25_indexing_and_search(self):
        """Verify SQLite FTS5 BM25 search indexes chunks and matches tokens accurately."""
        chunks = [
            CodeChunk(
                chunk_id="auth_service.py#verify_jwt",
                file_path=str(Path(self.temp_dir) / "auth_service.py"),
                symbol_type="function",
                symbol_name="verify_jwt",
                start_line=10,
                end_line=25,
                content="def verify_jwt(token: str):\n    decoded = jwt.decode(token, secret_key)\n    return decoded"
            ),
            CodeChunk(
                chunk_id="payment_gateway.py#process_stripe",
                file_path=str(Path(self.temp_dir) / "payment_gateway.py"),
                symbol_type="function",
                symbol_name="process_stripe",
                start_line=50,
                end_line=75,
                content="def process_stripe(charge_amount, customer_card):\n    return stripe.Charge.create(amount=charge_amount)"
            ),
            CodeChunk(
                chunk_id="db_pool.py#connect",
                file_path=str(Path(self.temp_dir) / "db_pool.py"),
                symbol_type="function",
                symbol_name="connect",
                start_line=1,
                end_line=15,
                content="def connect(database_url):\n    return psycopg2.connect(database_url)"
            )
        ]

        await self.graph_store.index_chunks_bm25(chunks)

        # Search for JWT token keyword
        results_jwt = await self.graph_store.bm25_search("verify_jwt decode secret_key")
        self.assertTrue(len(results_jwt) >= 1)
        self.assertEqual(results_jwt[0]["chunk_id"], "auth_service.py#verify_jwt")

        # Search for stripe charge keyword
        results_stripe = await self.graph_store.bm25_search("stripe charge customer_card")
        self.assertTrue(len(results_stripe) >= 1)
        self.assertEqual(results_stripe[0]["chunk_id"], "payment_gateway.py#process_stripe")

    async def test_hybrid_search_with_bm25_stream(self):
        """Verify BM25 Stream actively participates in RRF scoring and boosts exact matches."""
        vstore = LocalVectorStore()
        chunks = [
            CodeChunk(
                chunk_id="crypto.py#asymmetric_verify",
                file_path="crypto.py",
                symbol_type="function",
                symbol_name="asymmetric_verify",
                start_line=1,
                end_line=20,
                content="def asymmetric_verify(public_key, signature, digest):\n    return ed25519.verify(public_key, signature, digest)"
            ),
            CodeChunk(
                chunk_id="helpers.py#format_date",
                file_path="helpers.py",
                symbol_type="function",
                symbol_name="format_date",
                start_line=1,
                end_line=10,
                content="def format_date(dt): return dt.strftime('%Y-%m-%d')"
            )
        ]
        await vstore.add_chunks(chunks)
        await self.graph_store.index_chunks_bm25(chunks)

        engine = HybridSearchEngine(vstore, self.graph_store)
        results = await engine.search("ed25519 asymmetric_verify signature", top_k=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].symbol_name, "asymmetric_verify")

    def test_repo_map_generator(self):
        """Verify RepoMapGenerator creates a structured, compact architectural blueprint."""
        # Create mock repo files in temp_dir
        main_py = Path(self.temp_dir) / "main.py"
        main_py.write_text("class ServerApp:\n    def start(): pass\n    def stop(): pass\n\ndef main(): pass\n", encoding="utf-8")

        auth_py = Path(self.temp_dir) / "auth.py"
        auth_py.write_text("class Authenticator:\n    def authenticate(): pass\n", encoding="utf-8")

        # Populate SQLite symbol graph
        syms = [
            SymbolNode(id="main.py#ServerApp", name="ServerApp", symbol_type="class", file_path=str(main_py), start_line=1, end_line=3),
            SymbolNode(id="main.py#start", name="start", symbol_type="method", file_path=str(main_py), start_line=2, end_line=2, parent_symbol="ServerApp"),
            SymbolNode(id="main.py#main", name="main", symbol_type="function", file_path=str(main_py), start_line=5, end_line=5),
            SymbolNode(id="auth.py#Authenticator", name="Authenticator", symbol_type="class", file_path=str(auth_py), start_line=1, end_line=2),
            SymbolNode(id="auth.py#authenticate", name="authenticate", symbol_type="method", file_path=str(auth_py), start_line=2, end_line=2, parent_symbol="Authenticator"),
        ]
        self.graph_store._sync_insert_batch(syms, [])

        repo_map = RepoMapGenerator.generate_repo_map(
            self.temp_dir,
            graph_store=self.graph_store,
            max_chars=4000
        )

        self.assertIn("GLOBAL REPO MAP", repo_map)
        self.assertIn("main.py", repo_map)
        self.assertIn("ServerApp", repo_map)
        self.assertIn("def start()", repo_map)
        self.assertIn("auth.py", repo_map)
        self.assertIn("Authenticator", repo_map)

        # Verify structured data endpoint output
        data = RepoMapGenerator.get_repo_map_data(self.temp_dir, graph_store=self.graph_store)
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["files_count"] >= 2)
        self.assertIn("GLOBAL REPO MAP", data["map_text"])

if __name__ == "__main__":
    unittest.main()
