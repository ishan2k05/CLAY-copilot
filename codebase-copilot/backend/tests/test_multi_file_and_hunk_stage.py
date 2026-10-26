import unittest
import sys
from pathlib import Path

_backend_dir = Path(__file__).resolve().parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))
_root_dir = _backend_dir.parent
if str(_root_dir) not in sys.path:
    sys.path.insert(0, str(_root_dir))

from fastapi.testclient import TestClient
from app.main import app
from app.agent.patch_engine import PatchEngine

class TestMultiFileAndHunkStageEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_parse_instruction_endpoint(self):
        resp = self.client.post("/api/agent/parse-instruction", json={
            "goal": "increase paddle speed to 12 in game.py"
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["intent"]["intent"], "EDIT_EXISTING")
        self.assertTrue(len(data["change_specs"]) >= 1)
        self.assertEqual(data["change_specs"][0]["to"], "12")

    def test_intra_diff_endpoint(self):
        resp = self.client.post("/api/agent/intra-diff", json={
            "old_line": "PADDLE_HEIGHT = 80",
            "new_line": "PADDLE_HEIGHT = 120"
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        old_dels = [s["text"] for s in data["old_spans"] if s["type"] == "del"]
        new_adds = [s["text"] for s in data["new_spans"] if s["type"] == "add"]
        self.assertEqual(old_dels, ["80"])
        self.assertEqual(new_adds, ["120"])

    def test_discard_hunk_endpoint(self):
        multi_hunk_diff = (
            "--- a/dummy.py\n+++ b/dummy.py\n"
            "@@ -1,4 +1,4 @@\n"
            " line1\n"
            "-line2\n"
            "+line2_mod\n"
            " line3\n"
            " line4\n"
            "@@ -10,4 +10,4 @@\n"
            " line10\n"
            " line11\n"
            "-line12\n"
            "+line12_mod\n"
            " line13"
        )
        resp = self.client.post("/api/agent/discard-hunk", json={
            "file_path": "dummy.py",
            "diff": multi_hunk_diff,
            "hunk_index": 0
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # Hunk 0 was discarded, so hunk 1 should remain
        self.assertNotIn("line2_mod", data["diff"])
        self.assertIn("line12_mod", data["diff"])

    def test_compute_diff_returns_hunks_and_staged_files(self):
        resp = self.client.post("/api/agent/compute-diff", json={
            "target_file": "new_module.py",
            "code_or_snippet": "def hello():\n    return 'world'\n"
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("hunks", data)
        self.assertIn("staged_files", data)
        self.assertEqual(data["target_file"], "new_module.py")

    def test_build_multi_surgical_diff_extracts_both_files(self):
        llm_response = (
            "Here is the code for both files:\n\n"
            "```py\n"
            "# calc.py\n"
            "def multiply(a, b):\n"
            "    return a * b\n"
            "```\n\n"
            "```py\n"
            "# app.py\n"
            "from calc import multiply\n\n"
            "def app():\n"
            "    result = multiply(5, 4)\n"
            "    print(f'The result of multiply(5, 4) is {result}')\n"
            "```\n"
        )
        file_map = {
            "calc.py": [],
            "app.py": []
        }
        diff, staged = PatchEngine.build_multi_surgical_diff(
            file_orig_lines_map=file_map,
            generated_text=llm_response,
            default_filename="calc.py",
            allow_multi_file=True,
            authorized_files=["calc.py", "app.py"]
        )
        self.assertEqual(len(staged), 2)
        staged_names = [s["file_path"] for s in staged]
        self.assertIn("calc.py", staged_names)
        self.assertIn("app.py", staged_names)
        self.assertTrue(all(s["valid"] for s in staged))
        self.assertIn("calc.py", diff)
        self.assertIn("app.py", diff)

    def test_compute_diff_multi_file_endpoint(self):
        llm_response = (
            "```python\n"
            "# calc.py\n"
            "def multiply(a, b):\n"
            "    return a * b\n"
            "```\n\n"
            "```python\n"
            "# app.py\n"
            "from calc import multiply\n"
            "print(multiply(5, 4))\n"
            "```\n"
        )
        resp = self.client.post("/api/agent/compute-diff", json={
            "target_file": "calc.py",
            "target_files": ["calc.py", "app.py"],
            "allow_multi_file": True,
            "code_or_snippet": llm_response
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("staged_files", data)
        self.assertEqual(len(data["staged_files"]), 2)
        paths = [sf["file_path"] for sf in data["staged_files"]]
        self.assertIn("calc.py", paths)
        self.assertIn("app.py", paths)

    def test_build_multi_surgical_diff_respects_allow_multi_file_false(self):
        llm_response = (
            "```python\n"
            "# calc.py\n"
            "def add(a, b):\n"
            "    return a + b\n"
            "```\n\n"
            "```python\n"
            "# rogue_example.py\n"
            "print(add(1, 2))\n"
            "```\n"
        )
        diff, staged = PatchEngine.build_multi_surgical_diff(
            file_orig_lines_map={"calc.py": []},
            generated_text=llm_response,
            default_filename="calc.py",
            allow_multi_file=False,
            authorized_files=["calc.py"]
        )
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["file_path"], "calc.py")
        self.assertNotIn("rogue_example.py", diff)

    def test_build_multi_surgical_diff_respects_authorized_files(self):
        llm_response = (
            "```python\n"
            "# calc.py\n"
            "def add(a, b):\n"
            "    return a + b\n"
            "```\n\n"
            "```python\n"
            "# app.py\n"
            "from calc import add\n"
            "```\n\n"
            "```python\n"
            "# unauthorized_test.py\n"
            "assert add(1, 2) == 3\n"
            "```\n"
        )
        diff, staged = PatchEngine.build_multi_surgical_diff(
            file_orig_lines_map={"calc.py": [], "app.py": []},
            generated_text=llm_response,
            default_filename="calc.py",
            allow_multi_file=True,
            authorized_files=["calc.py", "app.py"]
        )
        staged_names = [s["file_path"] for s in staged]
        self.assertEqual(staged_names, ["calc.py", "app.py"])
        self.assertNotIn("unauthorized_test.py", diff)

    def test_build_surgical_diff_strips_synthetic_filename_header(self):
        orig_lines = ["def add(a, b):", "    return a - b"]
        snippet = "```python\n# calc.py\ndef add(a, b):\n    return a + b\n```"
        diff = PatchEngine.build_surgical_diff(orig_lines, snippet, "calc.py")
        self.assertNotIn("+# calc.py", diff)
        self.assertIn("+    return a + b", diff)

        new_file_diff = PatchEngine.build_surgical_diff([], snippet, "calc.py")
        self.assertNotIn("+# calc.py", new_file_diff)
        self.assertIn("+def add(a, b):", new_file_diff)

    def test_duplicate_basenames_in_subfolders(self):
        llm_response = (
            "```typescript\n"
            "// src/index.ts\n"
            "export const client = true;\n"
            "```\n\n"
            "```typescript\n"
            "// server/index.ts\n"
            "export const server = true;\n"
            "```\n"
        )
        file_map = {
            "src/index.ts": ["export const client = false;"],
            "server/index.ts": ["export const server = false;"]
        }
        diff, staged = PatchEngine.build_multi_surgical_diff(
            file_orig_lines_map=file_map,
            generated_text=llm_response,
            default_filename="src/index.ts",
            allow_multi_file=True,
            authorized_files=["src/index.ts", "server/index.ts"]
        )
        self.assertEqual(len(staged), 2)
        self.assertEqual(staged[0]["file_path"], "src/index.ts")
        self.assertEqual(staged[1]["file_path"], "server/index.ts")
        self.assertIn("export const client = true;", staged[0]["diff"])
        self.assertIn("export const server = true;", staged[1]["diff"])

    def test_resolve_target_files_same_basename_different_dirs(self):
        import tempfile
        from app.agent.instruction_parser import SymbolEntityResolver
        from app.retrieval.file_resolver import WorkspaceFileResolver

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "src" / "utils.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
            (root / "tests" / "utils.py").write_text("def test_helper():\n    assert True\n", encoding="utf-8")

            WorkspaceFileResolver._cached_files.clear()
            accessible = WorkspaceFileResolver.list_accessible_files(tmpdir)

            # Both POSIX keys must be present, and bare 'utils.py' must NOT be mapped to a single file
            self.assertIn("src/utils.py", accessible)
            self.assertIn("tests/utils.py", accessible)
            self.assertNotIn("utils.py", accessible)

            # Explicit multi-file prompt with subfolders must preserve both files
            resolved = SymbolEntityResolver.resolve_target_files(
                "Update tests/utils.py and src/utils.py to add logging",
                tmpdir,
                accessible
            )
            self.assertEqual(resolved, ["tests/utils.py", "src/utils.py"])

    def test_ambiguity_detector_triggers_on_bare_duplicate_basename(self):
        import tempfile
        from app.agent.instruction_parser import SymbolEntityResolver, AmbiguityDetector
        from app.retrieval.file_resolver import WorkspaceFileResolver

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "src" / "utils.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
            (root / "tests" / "utils.py").write_text("def test_helper():\n    assert True\n", encoding="utf-8")

            WorkspaceFileResolver._cached_files.clear()
            accessible = WorkspaceFileResolver.list_accessible_files(tmpdir)

            # Bare filename query 'refactor utils.py' must return both candidates and trigger ambiguity with folder paths
            candidates = SymbolEntityResolver.resolve_target_files("refactor utils.py", tmpdir, accessible)
            self.assertEqual(set(candidates), {"src/utils.py", "tests/utils.py"})

            amb = AmbiguityDetector.check_ambiguity("refactor utils.py", candidates)
            self.assertIsNotNone(amb)
            self.assertTrue(amb.get("detected"))
            self.assertEqual(set(amb.get("candidates", [])), {"src/utils.py", "tests/utils.py"})

            # Disambiguated query 'refactor src/utils.py' must NOT trigger ambiguity
            amb_specific = AmbiguityDetector.check_ambiguity("refactor src/utils.py", candidates)
            self.assertIsNone(amb_specific)

    def test_apply_patch_hunks_and_new_file_in_subfolder_with_same_basename(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            src_utils = root / "src" / "utils.py"
            tests_utils = root / "tests" / "utils.py"
            src_utils.write_text("def helper():\n    return 1\n", encoding="utf-8")
            tests_utils.write_text("def test_helper():\n    assert 1 == 1\n", encoding="utf-8")

            # 1. Patch targeting tests/utils.py with a bare +++ b/utils.py header must NOT overwrite src/utils.py
            bare_header_diff = (
                "--- a/utils.py\n"
                "+++ b/utils.py\n"
                "@@ -1,2 +1,2 @@\n"
                " def test_helper():\n"
                "-    assert 1 == 1\n"
                "+    assert 2 == 2\n"
            )
            res = PatchEngine.apply_patch_hunks("tests/utils.py", bare_header_diff, tmpdir)
            self.assertEqual(res.get("status"), "success")
            self.assertIn("assert 2 == 2", tests_utils.read_text(encoding="utf-8"))
            self.assertIn("return 1", src_utils.read_text(encoding="utf-8"))

            # 2. Resolving a brand-new file in a subfolder (e.g. docs/utils.py) must not resolve to existing src/utils.py
            new_sub_target = PatchEngine.resolve_target_path("docs/utils.py", tmpdir)
            self.assertEqual(new_sub_target.resolve(), (root / "docs" / "utils.py").resolve())

    def test_compute_diff_does_not_contaminate_same_basename_files(self):
        import asyncio
        import tempfile
        from app.main import compute_diff, ComputeDiffRequest, live_file_buffers
        from app.retrieval.file_resolver import WorkspaceFileResolver

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            src_utils = root / "src" / "utils.py"
            tests_utils = root / "tests" / "utils.py"
            src_utils.write_text("def calc():\n    return 10\n", encoding="utf-8")
            tests_utils.write_text("def test_calc():\n    assert True\n", encoding="utf-8")

            WorkspaceFileResolver._cached_files.clear()
            live_file_buffers.clear()

            llm_snippet = (
                "```python\n"
                "# src/utils.py\n"
                "def calc():\n"
                "    return 99\n"
                "```\n\n"
                "```python\n"
                "# tests/utils.py\n"
                "def test_calc():\n"
                "    assert 99 == 99\n"
                "```\n"
            )
            req = ComputeDiffRequest(
                target_file="src/utils.py",
                target_files=["src/utils.py", "tests/utils.py"],
                allow_multi_file=True,
                code_or_snippet=llm_snippet,
                workspace_path=tmpdir,
                active_file=str(src_utils),
                active_file_content="def calc():\n    return 20\n"
            )
            res = asyncio.run(compute_diff(req))
            staged = res.get("staged_files", [])
            self.assertEqual(len(staged), 2)
            # src/utils.py diff should be against active_file_content (return 20 -> return 99)
            self.assertIn("-    return 20", staged[0]["diff"])
            self.assertIn("+    return 99", staged[0]["diff"])
            # tests/utils.py diff must be against tests/utils.py on disk (assert True -> assert 99 == 99), NOT src/utils.py!
            self.assertIn("-    assert True", staged[1]["diff"])
            self.assertIn("+    assert 99 == 99", staged[1]["diff"])
            self.assertNotIn("return 20", staged[1]["diff"])


if __name__ == "__main__":
    unittest.main()

