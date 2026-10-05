import json
import unittest
import sys
from pathlib import Path

_backend_dir = Path(__file__).resolve().parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))
_root_dir = _backend_dir.parent
if str(_root_dir) not in sys.path:
    sys.path.insert(0, str(_root_dir))

from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app, active_chat_history, ollama_provider
from app.llm.gemma_provider import OllamaGemmaProvider


class TestMultiTurnChatSession(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        active_chat_history.clear()
        import app.main as main_mod
        main_mod.active_rolling_summary = None

    def tearDown(self):
        active_chat_history.clear()
        import app.main as main_mod
        main_mod.active_rolling_summary = None

    def test_gemma_provider_injects_conversation_history(self):
        """Verify stream_reasoning includes prior turns in the messages payload."""
        provider = OllamaGemmaProvider(model_name="gemma4:test")
        
        history = [
            {"role": "user", "content": "What is in test2.py?"},
            {"role": "assistant", "content": "test2.py contains UnionFind, SegmentTree, Trie, and Graph.\n(Tip: If you would like a visual architecture diagram of these components, just ask!)"}
        ]
        
        captured_payload = None

        class MockStreamResponse:
            def __init__(self):
                self.status_code = 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc, tb):
                pass
            async def aiter_lines(self):
                yield '{"message": {"content": "```mermaid\\ngraph TD\\n  A[UnionFind] --> B[Graph]\\n```"}}'

        class MockAsyncClient:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc, tb):
                pass
            def stream(self, method, url, json=None, headers=None):
                nonlocal captured_payload
                captured_payload = json
                return MockStreamResponse()

        with patch("httpx.AsyncClient", MockAsyncClient):
            async def run():
                tokens = []
                async for tok in provider.stream_reasoning("yes", history=history):
                    tokens.append(tok)
                return tokens

            import asyncio
            tokens = asyncio.run(run())

        self.assertIsNotNone(captured_payload)
        messages = captured_payload["messages"]
        # System prompt first
        self.assertEqual(messages[0]["role"], "system")
        # History turns injected in order
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn("What is in test2.py?", messages[1]["content"])
        self.assertEqual(messages[2]["role"], "assistant")
        self.assertIn("visual architecture diagram", messages[2]["content"])
        # Final user prompt last
        self.assertEqual(messages[3]["role"], "user")
        self.assertIn("yes", messages[3]["content"])
        # Directive 14 should be in system prompt
        self.assertIn("MULTI-TURN AFFIRMATIVE RESPONSES & SESSION AWARENESS", messages[0]["content"])

    def test_chat_endpoint_stores_and_clears_history(self):
        """Verify /api/chat preserves history and /api/chat/clear resets it."""
        mock_tokens = ["Here ", "is ", "the ", "summary."]

        async def fake_stream(*args, **kwargs):
            for t in mock_tokens:
                yield t

        with patch.object(ollama_provider, "stream_reasoning", side_effect=fake_stream):
            res1 = self.client.post("/api/chat", json={"query": "Overview of repo", "stream": True})
            self.assertEqual(res1.status_code, 200)

            # active_chat_history should have 2 entries (user and assistant)
            self.assertEqual(len(active_chat_history), 2)
            self.assertEqual(active_chat_history[0]["role"], "user")
            self.assertEqual(active_chat_history[0]["content"], "Overview of repo")
            self.assertEqual(active_chat_history[1]["role"], "assistant")
            self.assertEqual(active_chat_history[1]["content"], "Here is the summary.")

            # Turn 2 with affirmative response 'yes'
            res2 = self.client.post("/api/chat", json={
                "query": "yes",
                "stream": True,
                "history": active_chat_history
            })
            self.assertEqual(res2.status_code, 200)
            self.assertEqual(len(active_chat_history), 4)

            # Clear chat
            res_clear = self.client.post("/api/chat/clear")
            self.assertEqual(res_clear.status_code, 200)
            self.assertEqual(res_clear.json()["status"], "cleared")
            self.assertEqual(len(active_chat_history), 0)

    def test_chat_endpoint_triggers_compaction_and_clears_summary(self):
        """Verify /api/chat triggers history compaction for long conversations and /api/chat/clear resets rolling summary."""
        import app.main as main_mod

        long_history = []
        for i in range(1, 6):
            long_history.append({"role": "user", "content": f"Update module_{i}.py to use RSA-256 algorithm"})
            long_history.append({"role": "assistant", "content": f"Updated module_{i}.py with RSA-256."})

        captured_kwargs = {}

        async def fake_stream(*args, **kwargs):
            captured_kwargs.update(kwargs)
            yield "Understood."

        with patch.object(ollama_provider, "stream_reasoning", side_effect=fake_stream):
            res = self.client.post("/api/chat", json={
                "query": "Now check module_6.py",
                "stream": True,
                "history": long_history
            })
            self.assertEqual(res.status_code, 200)

            # Compacted history sent to provider should be exactly the last 4 turns
            sent_history = captured_kwargs.get("history")
            self.assertIsNotNone(sent_history)
            self.assertEqual(len(sent_history), 4)

            # Rolling summary should be generated and passed to provider
            sent_summary = captured_kwargs.get("summary")
            self.assertIsNotNone(sent_summary)
            self.assertIn("Files discussed", sent_summary)
            self.assertIn("module_1.py", sent_summary)

            # Rolling summary should be stored on backend
            self.assertIsNotNone(main_mod.active_rolling_summary)

            # Clearing chat should reset active_rolling_summary
            clear_res = self.client.post("/api/chat/clear")
            self.assertEqual(clear_res.status_code, 200)
            self.assertIsNone(main_mod.active_rolling_summary)

    def test_resolve_query_files_with_multi_turn_history(self):
        """Verifies that follow-up prompts like 'yes give me flowchart' pull files from previous conversation turns."""
        from app.retrieval.file_resolver import WorkspaceFileResolver
        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp()
        try:
            sample_file = Path(temp_dir) / "pipeline.py"
            sample_file.write_text("def run_job():\n    pass\n", encoding="utf-8")

            # 1. Query with direct file reference
            chunks1 = WorkspaceFileResolver.resolve_query_files("tell me about pipeline.py", temp_dir)
            self.assertEqual(len(chunks1), 1)
            self.assertEqual(Path(chunks1[0].file_path).name, "pipeline.py")

            # 2. Follow-up query 'yes give me flowchart' WITHOUT history should not match
            chunks2_no_hist = WorkspaceFileResolver.resolve_query_files("yes give me flowchart", temp_dir)
            self.assertEqual(len(chunks2_no_hist), 0)

            # 3. Follow-up query WITH history mentioning pipeline.py should resolve pipeline.py
            history = [
                {"role": "user", "content": "tell me about pipeline.py"},
                {"role": "assistant", "content": "Here is pipeline.py. (Tip: If you would like a visual flowchart of this workflow, just ask!)"}
            ]
            chunks3_with_hist = WorkspaceFileResolver.resolve_query_files("yes give me flowchart", temp_dir, history=history)
            self.assertEqual(len(chunks3_with_hist), 1)
            self.assertEqual(Path(chunks3_with_hist[0].file_path).name, "pipeline.py")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
