import unittest
import sys
from pathlib import Path

_backend_dir = Path(__file__).resolve().parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))
_root_dir = _backend_dir.parent
if str(_root_dir) not in sys.path:
    sys.path.insert(0, str(_root_dir))

from app.agent.instruction_parser import (
    InstructionClassifier,
    SymbolEntityResolver,
    AmbiguityDetector,
    ContextWindowBuilder,
    DynamicTaskPlanner,
    TopicDomainResolver,
    normalize_prompt_text,
)
from app.agent.patch_engine import PatchEngine
from app.agent.llm_planner import LLMInstructionDecomposer

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

    def test_create_new_nlp_intent_and_target_extraction(self):
        # 1. User specific prompt: "write me code for DSA and put them in a new file , test2.py"
        prompt1 = "write me code for DSA and put them in a new file , test2.py"
        c1 = InstructionClassifier.classify(prompt1)
        self.assertEqual(c1["intent"], "CREATE_NEW")
        targets1 = SymbolEntityResolver.extract_target_filepaths(prompt1)
        self.assertIn("test2.py", targets1)

        # Target resolution should not discard test2.py even if accessible_files is empty or only has test.py
        accessible_mock = {"test.py": Path("/tmp/test.py")}
        resolved1 = SymbolEntityResolver.resolve_target_files(prompt1, "/tmp", accessible_mock)
        self.assertIn("test2.py", resolved1)

        # 2. Folder and file combination: "create a folder dsa and put test2.py inside it"
        prompt2 = "create a folder dsa and put test2.py inside it"
        c2 = InstructionClassifier.classify(prompt2)
        self.assertIn(c2["intent"], ("CREATE_NEW", "CREATE_FOLDER"))
        targets2 = SymbolEntityResolver.extract_target_filepaths(prompt2)
        self.assertTrue(any("dsa/test2.py" in t or "test2.py" in t for t in targets2))

        # 3. User specific bug reproduction: "create me a new folder named documentation , inside it put plan.md inside teh plan,md just put a plaeholder statement"
        prompt3 = "create me a new folder named documentation , inside it put plan.md inside teh plan,md just put a plaeholder statement"
        c3 = InstructionClassifier.classify(prompt3)
        self.assertIn(c3["intent"], ("CREATE_NEW", "CREATE_FOLDER"))
        targets3 = SymbolEntityResolver.extract_target_filepaths(prompt3)
        self.assertIn("documentation/plan.md", targets3)
        self.assertNotIn("named", targets3)
        self.assertNotIn("named/plan.md", targets3)

        # 4. Pure folder creation with connecting word: "create me a new folder named documentation"
        prompt4 = "create me a new folder named documentation"
        c4 = InstructionClassifier.classify(prompt4)
        self.assertEqual(c4["intent"], "CREATE_FOLDER")
        targets4 = SymbolEntityResolver.extract_target_filepaths(prompt4)
        self.assertIn("documentation", targets4)
        self.assertNotIn("named", targets4)

    def test_arbitrary_extensions_and_folder_creation(self):
        # TypeScript
        targets_ts = SymbolEntityResolver.extract_target_filepaths("write user interface and save in src/types/user.ts")
        self.assertTrue(any("user.ts" in t for t in targets_ts))

        # Rust
        targets_rs = SymbolEntityResolver.extract_target_filepaths("implement algorithm in solver.rs")
        self.assertTrue(any("solver.rs" in t for t in targets_rs))

        # Go
        targets_go = SymbolEntityResolver.extract_target_filepaths("create a new file handlers.go with http endpoints")
        self.assertTrue(any("handlers.go" in t for t in targets_go))

        # HTML / CSS
        targets_css = SymbolEntityResolver.extract_target_filepaths("put styles in static/css/main.css")
        self.assertTrue(any("main.css" in t for t in targets_css))

        # Pure folder creation
        prompt_folder = "create folder algorithms"
        c_folder = InstructionClassifier.classify(prompt_folder)
        self.assertEqual(c_folder["intent"], "CREATE_FOLDER")
        targets_folder = SymbolEntityResolver.extract_target_filepaths(prompt_folder)
        self.assertIn("algorithms", targets_folder)

    def test_patch_engine_new_file_and_subfolder_creation(self):
        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp()
        try:
            # Test creating a new file in a nested folder via PatchEngine
            diff_text = (
                "--- /dev/null\n"
                "+++ b/dsa/test2.py\n"
                "@@ -0,0 +1,4 @@\n"
                "+def binary_search(arr, target):\n"
                "+    return -1\n"
            )
            result = PatchEngine.apply_patch_hunks("dsa/test2.py", diff_text, workspace_path=temp_dir)
            self.assertEqual(result["status"], "success")
            self.assertTrue(result["created"])

            created_file = Path(temp_dir) / "dsa" / "test2.py"
            self.assertTrue(created_file.exists())
            content = created_file.read_text(encoding="utf-8")
            self.assertIn("def binary_search", content)

            # Test creating non-python file (.ts)
            diff_ts = (
                "--- /dev/null\n"
                "+++ b/frontend/models.ts\n"
                "@@ -0,0 +1,3 @@\n"
                "+export interface Node {\n"
                "+    value: number;\n"
                "+}\n"
            )
            res_ts = PatchEngine.apply_patch_hunks("frontend/models.ts", diff_ts, workspace_path=temp_dir)
            self.assertEqual(res_ts["status"], "success")
            self.assertTrue((Path(temp_dir) / "frontend" / "models.ts").exists())
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_dynamic_task_planner_folder_creation(self):
        plan = DynamicTaskPlanner.plan_tasks(
            goal="create a folder named documentation",
            target_files=["documentation"]
        )
        self.assertEqual(plan["total_steps"], 2)
        self.assertEqual(plan["steps"][0]["action"], "scan")
        self.assertEqual(plan["steps"][1]["action"], "prep")
        self.assertIn("documentation", plan["steps"][0]["title"])
        self.assertIn("documentation", plan["steps"][1]["title"])

    def test_dynamic_task_planner_delete_code(self):
        plan = DynamicTaskPlanner.plan_tasks(
            goal="delete all code in test1.py",
            target_files=["test1.py"]
        )
        self.assertEqual(plan["total_steps"], 3)
        self.assertEqual(plan["steps"][0]["action"], "scan")
        self.assertEqual(plan["steps"][1]["action"], "delete")
        self.assertEqual(plan["steps"][2]["action"], "verify")
        self.assertIn("test1.py", plan["steps"][0]["title"])
        self.assertIn("truncation diff", plan["steps"][1]["title"].lower())
        self.assertIn("test1.py", plan["steps"][1]["title"])

    def test_dynamic_task_planner_partial_removal_is_edit_existing(self):
        """
        Partial code removals like 'remove first print statement' must NOT trigger
        a full-file wipe / truncation diff. They must be routed to surgical synthesis.
        """
        plan = DynamicTaskPlanner.plan_tasks(
            goal="remove first print statement",
            target_files=["app.py"]
        )
        self.assertEqual(plan["total_steps"], 3)
        self.assertEqual(plan["steps"][0]["action"], "scan")
        self.assertEqual(plan["steps"][1]["action"], "synthesize")
        self.assertEqual(plan["steps"][2]["action"], "verify")
        self.assertNotIn("truncation diff", plan["steps"][1]["title"].lower())
        self.assertIn("app.py", plan["steps"][1]["title"])

    def test_dynamic_task_planner_new_file(self):
        plan = DynamicTaskPlanner.plan_tasks(
            goal="write me basic code in test1.py",
            target_files=["test1.py"]
        )
        self.assertEqual(plan["total_steps"], 3)
        self.assertEqual(plan["steps"][0]["action"], "scan")
        self.assertEqual(plan["steps"][1]["action"], "synthesize")
        self.assertEqual(plan["steps"][2]["action"], "verify")
        self.assertIn("test1.py", plan["steps"][0]["title"])
        self.assertIn("test1.py", plan["steps"][1]["title"])
        self.assertIn("test1.py", plan["steps"][2]["title"])

    def test_dynamic_task_planner_nested_folder_and_file(self):
        plan = DynamicTaskPlanner.plan_tasks(
            goal="create folder utils and file helper.py",
            target_files=["helper.py"]
        )
        self.assertEqual(plan["total_steps"], 4)
        self.assertEqual(plan["steps"][0]["action"], "scan")
        self.assertEqual(plan["steps"][1]["action"], "prep")
        self.assertEqual(plan["steps"][2]["action"], "synthesize")
        self.assertEqual(plan["steps"][3]["action"], "verify")
        self.assertIn("utils", plan["steps"][0]["title"])
        self.assertIn("helper.py", plan["steps"][1]["title"])
        self.assertIn("helper.py", plan["steps"][2]["title"])

    def test_dynamic_task_planner_bug_fix(self):
        plan = DynamicTaskPlanner.plan_tasks(
            goal="fix failing test in test_auth.py",
            target_files=["test_auth.py"]
        )
        self.assertEqual(plan["total_steps"], 4)
        self.assertEqual(plan["steps"][0]["action"], "scan")
        self.assertEqual(plan["steps"][1]["action"], "prep")
        self.assertEqual(plan["steps"][2]["action"], "synthesize")
        self.assertEqual(plan["steps"][3]["action"], "verify")
        self.assertIn("test_auth.py", plan["steps"][0]["title"])
        self.assertIn("test_auth.py", plan["steps"][2]["title"])

    def test_dynamic_task_planner_huge_prompt_max_5_steps(self):
        huge_goal = (
            "refactor the authentication service and user session logic across all files "
            "to support oauth2 and jwt verification pipelines with strict security headers"
        )
        plan = DynamicTaskPlanner.plan_tasks(
            goal=huge_goal,
            target_files=["auth.py", "session.py"]
        )
        self.assertEqual(plan["total_steps"], 5)
        self.assertEqual(plan["complexity"], "huge")
        actions = [s["action"] for s in plan["steps"]]
        self.assertEqual(actions, ["scan", "prep", "synthesize", "validate", "verify"])
        # Ensure all titles are unique and non-empty
        titles = [s["title"] for s in plan["steps"]]
        self.assertEqual(len(titles), len(set(titles)))
        for s in plan["steps"]:
            self.assertTrue(len(s["title"]) > 10)
            self.assertTrue(len(s["progress"]) > 10)
            self.assertTrue(len(s["detail"]) > 10)

    def test_dynamic_task_planner_astar_sophisticated_algorithm(self):
        # Prompt exactly as tested by the user with typos: "write me a sophesticated adn large code for astar algo in test.py"
        user_prompt = "write me a sophesticated adn large code for astar algo in test.py"
        plan = DynamicTaskPlanner.plan_tasks(
            goal=user_prompt,
            target_file="test.py"
        )
        self.assertEqual(plan["total_steps"], 5)
        self.assertEqual(plan["complexity"], "huge")
        self.assertEqual(plan["target_file"], "test.py")
        self.assertEqual(plan["topic"], "A* Pathfinding Algorithm")

        actions = [s["action"] for s in plan["steps"]]
        self.assertEqual(actions, ["scan", "prep", "synthesize", "validate", "verify"])

        # Check domain-specific dynamic titles
        self.assertIn("A* Pathfinding Algorithm", plan["steps"][0]["title"])
        self.assertIn("heuristic", plan["steps"][0]["title"].lower())
        self.assertIn("priority queue", plan["steps"][1]["title"].lower())
        self.assertIn("A* Pathfinding Algorithm", plan["steps"][2]["title"])
        self.assertIn("AST validation", plan["steps"][3]["title"])
        self.assertIn("A* Pathfinding Algorithm", plan["steps"][4]["title"])

        # Ensure all 5 titles are completely unique
        titles = [s["title"] for s in plan["steps"]]
        self.assertEqual(len(titles), len(set(titles)))

    def test_dynamic_task_planner_compound_folder_and_placeholder_file(self):
        # Prompt exactly as shown in user screenshot with typo: "create a new folder docs , and makea placeholder file inside it script.py"
        user_prompt = "create a new folder docs , and makea placeholder file inside it script.py"
        plan = DynamicTaskPlanner.plan_tasks(
            goal=user_prompt
        )
        self.assertEqual(plan["total_steps"], 4)
        actions = [s["action"] for s in plan["steps"]]
        self.assertEqual(actions, ["scan", "prep", "synthesize", "verify"])
        self.assertIn("docs", plan["folder_name"])
        self.assertTrue(any("script.py" in t for t in (plan["target_file"], plan.get("steps", [{}])[2].get("title", ""))))
        self.assertIn("docs", plan["steps"][1]["title"])

    def test_llm_instruction_decomposer_json_parsing(self):
        # 1. Markdown code fences
        md_json = "```json\n{\"primary_intent\": \"CREATE_FOLDER_AND_FILE\", \"steps\": [{\"id\": 1, \"action\": \"scan\"}, {\"id\": 2, \"action\": \"prep\"}]}\n```"
        parsed = LLMInstructionDecomposer._extract_json(md_json)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["primary_intent"], "CREATE_FOLDER_AND_FILE")

        # 2. Trailing commas cleaning
        trailing_comma_json = "{\"primary_intent\": \"CREATE_NEW\", \"steps\": [{\"id\": 1, \"action\": \"scan\"}, ], }"
        parsed2 = LLMInstructionDecomposer._extract_json(trailing_comma_json)
        self.assertIsNotNone(parsed2)
        self.assertEqual(parsed2["primary_intent"], "CREATE_NEW")

    def test_llm_instruction_decomposer_fallback_execution(self):
        import asyncio
        user_prompt = "create a new folder docs , and makea placeholder file inside it script.py"
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            plan = loop.run_until_complete(
                LLMInstructionDecomposer.decompose_goal(
                    goal=user_prompt,
                    workspace_dir=".",
                    provider=None
                )
            )
            self.assertEqual(plan["total_steps"], 4)
            self.assertEqual(plan["source"], "heuristic_fallback")
            actions = [s["action"] for s in plan["steps"]]
            self.assertEqual(actions, ["scan", "prep", "synthesize", "verify"])
        finally:
            loop.close()

if __name__ == "__main__":
    unittest.main()



