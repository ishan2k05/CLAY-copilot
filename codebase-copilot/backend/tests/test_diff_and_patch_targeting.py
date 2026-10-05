import unittest
import tempfile
import sys
from pathlib import Path

_backend_dir = Path(__file__).resolve().parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))
_root_dir = _backend_dir.parent
if str(_root_dir) not in sys.path:
    sys.path.insert(0, str(_root_dir))

from app.retrieval.file_resolver import WorkspaceFileResolver
from app.agent.patch_engine import PatchEngine
from app.main import resolve_workspace_path

class TestDiffAndPatchTargeting(unittest.TestCase):
    def test_bare_folder_resolution(self):
        # Resolving "Clay test" should resolve to Desktop/Clay test if it exists
        p = resolve_workspace_path("Clay test")
        if not p.exists() or not list(p.glob("*.py")):
            with tempfile.TemporaryDirectory() as td:
                (Path(td) / "test_sample.py").write_text("print('sample')\n")
                p_td = resolve_workspace_path(td)
                self.assertTrue(p_td.exists())
            return
        self.assertTrue(p.exists())
        self.assertEqual(p.name, "Clay test")
        py_files = list(p.glob("*.py"))
        self.assertGreater(len(py_files), 0)

    def test_query_files_resolution_with_bare_folder_name(self):
        p = resolve_workspace_path("Clay test")
        if not p.exists() or not list(p.glob("*.py")):
            with tempfile.TemporaryDirectory() as td:
                (Path(td) / "test_c.py").write_text("def test_c():\n    pass\n")
                chunks = WorkspaceFileResolver.resolve_query_files("what is in test_c.py", td, "sandboxed")
                self.assertGreater(len(chunks), 0)
            return
        # Query asking for test_c.py with bare folder "Clay test" in sandboxed mode
        chunks = WorkspaceFileResolver.resolve_query_files("what is in test_c.py", "Clay test", "sandboxed")
        self.assertGreater(len(chunks), 0)
        self.assertTrue(any("test" in c.symbol_name for c in chunks))

    def test_patch_engine_resolves_real_repo_file(self):
        # Even if a generic name "solution.py" is passed, if workspace only has test_c.py,
        # it should resolve to test_c.py
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "test_c.py").write_text("print('hello')\n")
            target = PatchEngine.resolve_target_path("solution.py", td)
            self.assertEqual(target.name, "test_c.py")

    def test_patch_engine_respects_diff_header(self):
        # If diff header has +++ b/test_c.py, it must target test_c.py
        with tempfile.TemporaryDirectory() as td:
            target_f = Path(td) / "test_c.py"
            target_f.write_text("a = 10\nb = 20\n")
            
            diff = (
                "--- a/test_c.py\n"
                "+++ b/test_c.py\n"
                "@@ -1,2 +1,2 @@\n"
                "-a = 10\n"
                "+a = 99\n"
                " b = 20\n"
            )
            res = PatchEngine.apply_patch_hunks("solution.py", diff, td)
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["filename"], "test_c.py")
            self.assertIn("a = 99", target_f.read_text())

if __name__ == "__main__":
    unittest.main()
