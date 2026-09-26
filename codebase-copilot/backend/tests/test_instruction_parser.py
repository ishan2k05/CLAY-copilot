import unittest
from pathlib import Path
from backend.app.agent.instruction_parser import (
    InstructionClassifier,
    SymbolEntityResolver,
    AmbiguityDetector,
    ContextWindowBuilder,
)
from backend.app.agent.patch_engine import PatchEngine

class TestInstructionParserAndSmarterContext(unittest.TestCase):

    def test_intent_classification(self):
        # 1. Edit existing
        res1 = InstructionClassifier.classify("increase paddle height to 120 in game.py")
        self.assertEqual(res1["intent"], "EDIT_EXISTING")

        # 2. Create new
        res2 = InstructionClassifier.classify("create a new file auth_middleware.py with jwt validation")
        self.assertEqual(res2["intent"], "CREATE_NEW")

        # 3. Fix test
        res3 = InstructionClassifier.classify("fix failing pytest in test_auth.py")
        self.assertEqual(res3["intent"], "FIX_TEST")

        # 4. Multi-file refactor
        res4 = InstructionClassifier.classify("rename getUser to fetchUser across both api.py and client.ts")
        self.assertEqual(res4["intent"], "MULTI_FILE_REFACTOR")

        # 5. Explain
        res5 = InstructionClassifier.classify("explain how the token bucket rate limiter works")
        self.assertEqual(res5["intent"], "EXPLAIN")

    def test_entity_symbol_and_change_specs(self):
        goal = "increase PADDLE_HEIGHT from 80 to 120 in game.py"
        specs = SymbolEntityResolver.extract_change_specs(goal)
        self.assertTrue(len(specs) >= 1)
        self.assertEqual(specs[0]["target"], "PADDLE_HEIGHT")
        self.assertEqual(specs[0]["to"], "120")

        code = (
            "import os\n\n"
            "PADDLE_HEIGHT = 80\n"
            "BALL_SPEED = 5\n\n"
            "def update_paddle_position(pos):\n"
            "    return pos + 10\n"
        )
        symbols = SymbolEntityResolver.resolve_target_symbols("increase paddle height in update_paddle_position", code)
        self.assertIn("PADDLE_HEIGHT", symbols)
        self.assertIn("update_paddle_position", symbols)

    def test_ambiguity_detector(self):
        # If user explicitly specifies filename, not ambiguous
        amb1 = AmbiguityDetector.check_ambiguity("change button color in buttons.css", ["styles/buttons.css", "styles/theme.css"])
        self.assertIsNone(amb1)

        # If user gives a vague goal that matches multiple files, ambiguous
        amb2 = AmbiguityDetector.check_ambiguity("change button color", ["styles/buttons.css", "styles/theme.css"])
        self.assertIsNotNone(amb2)
        self.assertEqual(amb2["status"], "ambiguous")
        self.assertIn("buttons.css", amb2["candidates"])
        self.assertIn("theme.css", amb2["candidates"])

    def test_ast_skeleton_context_builder(self):
        # Create a 150-line Python mock file
        lines = [
            "import os",
            "import sys",
            "from typing import List, Dict",
            "",
            "MAX_RETRIES = 5",
            "TIMEOUT = 30",
            "",
            "def helper_one():",
            "    '''First helper function docstring.'''",
            "    a = 1",
            "    b = 2",
            "    return a + b",
            "",
        ]
        # Add 100 filler lines
        for i in range(100):
            lines.append(f"def filler_func_{i}():\n    return {i}")

        lines.extend([
            "",
            "def target_calculation(x, y):",
            "    '''Target function that should be preserved in full.'''",
            "    multiplier = 10",
            "    result = (x + y) * multiplier",
            "    return result",
            "",
            "class ExtraService:",
            "    '''Class docstring.'''",
            "    def run(self):",
            "        pass",
        ])

        full_content = "\n".join(lines)
        ctx, included_lines = ContextWindowBuilder.build_ast_skeleton_context(
            full_content,
            "service.py",
            target_symbols=["target_calculation"]
        )

        # Context must contain imports & constants
        self.assertIn("import os", ctx)
        self.assertIn("MAX_RETRIES = 5", ctx)
        # Context must contain verbatim target calculation function body
        self.assertIn("def target_calculation(x, y):", ctx)
        self.assertIn("multiplier = 10", ctx)
        self.assertIn("result = (x + y) * multiplier", ctx)
        # Context must collapse filler functions to save space
        self.assertIn("collapsed for brevity", ctx)
        # Verify line numbers are prefixed
        self.assertRegex(ctx, r"\d+:\s+def target_calculation")

    def test_per_hunk_split_and_discard(self):
        orig_content = (
            "line1\nline2\nline3\nline4\nline5\n"
            "line6\nline7\nline8\nline9\nline10\n"
            "line11\nline12\nline13\nline14\nline15\n"
        )
        orig_lines = orig_content.splitlines()

        # Diff modifying line2 (hunk 0) and line14 (hunk 1)
        diff_text = (
            "--- a/test.txt\n+++ b/test.txt\n"
            "@@ -1,4 +1,4 @@\n"
            " line1\n"
            "-line2\n"
            "+line2_modified\n"
            " line3\n"
            " line4\n"
            "@@ -12,4 +12,4 @@\n"
            " line12\n"
            " line13\n"
            "-line14\n"
            "+line14_modified\n"
            " line15"
        )

        hunks = PatchEngine.split_into_hunks(diff_text)
        self.assertEqual(len(hunks), 2)
        self.assertEqual(hunks[0]["index"], 0)
        self.assertEqual(hunks[1]["index"], 1)

        # Discard hunk 0 (line2 modification)
        remaining_diff = PatchEngine.discard_hunk(orig_lines, diff_text, 0, "test.txt")
        self.assertNotIn("line2_modified", remaining_diff)
        self.assertIn("line14_modified", remaining_diff)

    def test_intra_line_diff(self):
        old_l = "const PADDLE_HEIGHT = 80;"
        new_l = "const PADDLE_HEIGHT = 120;"
        intra = PatchEngine.compute_intra_line_diff(old_l, new_l)

        old_dels = [s["text"] for s in intra["old_spans"] if s["type"] == "del"]
        new_adds = [s["text"] for s in intra["new_spans"] if s["type"] == "add"]
        self.assertEqual(old_dels, ["80"])
        self.assertEqual(new_adds, ["120"])

    def test_multi_file_diff_split(self):
        multi_diff = (
            "--- a/backend/main.py\n+++ b/backend/main.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-PORT = 8000\n"
            "+PORT = 8080\n"
            "--- a/frontend/api.ts\n+++ b/frontend/api.ts\n"
            "@@ -1,2 +1,2 @@\n"
            "-const BASE = 8000;\n"
            "+const BASE = 8080;\n"
        )
        files = PatchEngine.split_multi_file_diff(multi_diff)
        self.assertEqual(len(files), 2)
        self.assertIn("backend/main.py", files[0]["file_path"])
        self.assertIn("frontend/api.ts", files[1]["file_path"])

    def test_gameplay_and_speed_intents(self):
        c1 = InstructionClassifier.classify("make snake move faster in test1.py")
        self.assertEqual(c1["intent"], "EDIT_EXISTING")

        c2 = InstructionClassifier.classify("speed up snake movement")
        self.assertEqual(c2["intent"], "EDIT_EXISTING")

        c3 = InstructionClassifier.classify("increase speed to 0.05")
        self.assertEqual(c3["intent"], "EDIT_EXISTING")

    def test_enhanced_change_specs(self):
        specs1 = SymbolEntityResolver.extract_change_specs("increase speed to 0.05")
        self.assertTrue(any(s.get("to") == "0.05" for s in specs1))

        specs2 = SymbolEntityResolver.extract_change_specs("change timeout from 3000 to 5000")
        self.assertTrue(any(s.get("from") == "3000" and s.get("to") == "5000" for s in specs2))

if __name__ == "__main__":
    unittest.main()

