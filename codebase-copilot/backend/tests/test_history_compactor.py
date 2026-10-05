import unittest
import sys
from pathlib import Path

_backend_dir = Path(__file__).resolve().parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))
_root_dir = _backend_dir.parent
if str(_root_dir) not in sys.path:
    sys.path.insert(0, str(_root_dir))

from app.llm.history_compactor import HistoryCompactor


class TestHistoryCompactor(unittest.TestCase):

    def test_short_history_passthrough_verbatim(self):
        history = [
            {"role": "user", "content": "How does auth work?"},
            {"role": "assistant", "content": "Auth uses JWT tokens in verifier.ts."},
            {"role": "user", "content": "Can we make it faster?"},
            {"role": "assistant", "content": "Yes, we can cache the public keys."}
        ]
        compacted, summary = HistoryCompactor.compact_history(history, num_ctx=8192)

        # All 4 turns preserved
        self.assertEqual(len(compacted), 4)
        self.assertEqual(compacted[0]["content"], history[0]["content"])
        self.assertEqual(compacted[3]["content"], history[3]["content"])
        # No summary needed for short history
        self.assertIsNone(summary)

    def test_rolling_compaction_on_longer_conversation(self):
        history = []
        for i in range(1, 11):
            history.append({"role": "user", "content": f"Step {i}: modify file_{i}.py with logic {i}"})
            history.append({"role": "assistant", "content": f"Step {i} completed for file_{i}.py"})

        # Total 20 turns
        self.assertEqual(len(history), 20)

        compacted, summary = HistoryCompactor.compact_history(history, num_ctx=8192)

        # Exactly last 4 turns retained in compacted list
        self.assertEqual(len(compacted), 4)
        self.assertEqual(compacted[-1]["content"], history[-1]["content"])
        self.assertEqual(compacted[-2]["content"], history[-2]["content"])

        # Summary generated for turns 1 to 16
        self.assertIsNotNone(summary)
        self.assertIn("Files discussed", summary)
        self.assertIn("file_1.py", summary)
        self.assertLess(len(summary), 1500)

    def test_historical_code_block_redaction(self):
        long_code = "\n".join([f"    const line_{i} = calculate({i});" for i in range(100)])
        code_message = f"Here is the implementation:\n```typescript\n{long_code}\n```\nDone."

        redacted = HistoryCompactor.redact_code_blocks(code_message, max_lines=30)

        self.assertNotIn("line_99", redacted)
        self.assertIn("Historical code implementation: 100 lines", redacted)
        self.assertIn("```typescript", redacted)

    def test_architectural_decision_retention(self):
        history = [
            {"role": "user", "content": "We must use RSA-256 keypairs instead of HMAC for security in auth.ts"},
            {"role": "assistant", "content": "Understood, configuring RSA-256 in auth.ts."},
            {"role": "user", "content": "Ensure port: 8765 is used for the daemon."},
            {"role": "assistant", "content": "Port 8765 configured."},
            {"role": "user", "content": "Now let's check step 3"},
            {"role": "assistant", "content": "Step 3 ready."},
            {"role": "user", "content": "Now let's check step 4"},
            {"role": "assistant", "content": "Step 4 ready."}
        ]
        compacted, summary = HistoryCompactor.compact_history(history, num_ctx=8192)

        self.assertEqual(len(compacted), 4)
        self.assertIsNotNone(summary)
        # Verify key decisions from Turn 1 and 2 are captured in summary
        self.assertTrue("RSA-256" in summary or "auth.ts" in summary)
        self.assertIn("auth.ts", summary)

    def test_budget_scaling_based_on_num_ctx(self):
        budget_small = HistoryCompactor.calculate_budget(num_ctx=4096)
        budget_large = HistoryCompactor.calculate_budget(num_ctx=32768)

        self.assertLess(budget_small["summary_max_chars"], budget_large["summary_max_chars"])
        self.assertEqual(budget_small["summary_max_chars"], 900)
        self.assertEqual(budget_large["summary_max_chars"], 3200)


if __name__ == "__main__":
    unittest.main()
