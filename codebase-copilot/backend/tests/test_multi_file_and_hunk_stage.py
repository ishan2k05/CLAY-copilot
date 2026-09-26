import unittest
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.agent.patch_engine import PatchEngine

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

if __name__ == "__main__":
    unittest.main()
