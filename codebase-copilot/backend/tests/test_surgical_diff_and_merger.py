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

from fastapi.testclient import TestClient

from app.agent.patch_engine import PatchEngine, SurgicalDiffBuilder
from app.main import app

SAMPLE_PING_PONG = """import pygame
import sys

# Game constants
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 600
PADDLE_WIDTH = 15
PADDLE_HEIGHT = 80
BALL_RADIUS = 8
BALL_SPEED_X = 5
BALL_SPEED_Y = 5
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

class Paddle:
    def __init__(self, x, y):
        self.rect = pygame.Rect(x, y, PADDLE_WIDTH, PADDLE_HEIGHT)
        self.speed = 7

    def move(self, up=True):
        if up and self.rect.top > 0:
            self.rect.y -= self.speed
        elif not up and self.rect.bottom < SCREEN_HEIGHT:
            self.rect.y += self.speed

    def draw(self, surface):
        pygame.draw.rect(surface, WHITE, self.rect)

class Ball:
    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.vx = BALL_SPEED_X
        self.vy = BALL_SPEED_Y

    def update(self):
        self.x += self.vx
        self.y += self.vy

def main():
    pygame.init()
    screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
    clock = pygame.time.Clock()
    paddle = Paddle(30, 250)
    ball = Ball(400, 300)

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
        screen.fill(BLACK)
        paddle.draw(screen)
        pygame.display.flip()
        clock.tick(60)

    pygame.quit()
    sys.exit()

if __name__ == "__main__":
    main()
"""

class TestSurgicalDiffAndMerger(unittest.TestCase):

    def setUp(self):
        self.orig_lines = SAMPLE_PING_PONG.splitlines()

    def test_single_line_change_from_full_code_does_not_wipe_file(self):
        # Model returns entire ping pong code, but only changed PADDLE_HEIGHT to 120
        new_code = SAMPLE_PING_PONG.replace("PADDLE_HEIGHT = 80", "PADDLE_HEIGHT = 120")
        diff = SurgicalDiffBuilder.build_surgical_diff(self.orig_lines, f"```python\n{new_code}\n```", "pingpong.py")

        # Crucial check: Diff must NOT delete 50 lines. It should only delete 1 line and add 1 line!
        lines = diff.splitlines()
        del_lines = [l for l in lines if l.startswith("-") and not l.startswith("---")]
        add_lines = [l for l in lines if l.startswith("+") and not l.startswith("+++")]

        self.assertEqual(len(del_lines), 1, f"Expected only 1 deletion line, got: {del_lines}")
        self.assertEqual(len(add_lines), 1, f"Expected only 1 addition line, got: {add_lines}")
        self.assertIn("-PADDLE_HEIGHT = 80", del_lines[0])
        self.assertIn("+PADDLE_HEIGHT = 120", add_lines[0])

    def test_single_line_change_from_search_replace_block(self):
        sr_text = (
            "<<<<<<< SEARCH\n"
            "PADDLE_HEIGHT = 80\n"
            "=======\n"
            "PADDLE_HEIGHT = 120\n"
            ">>>>>>> REPLACE"
        )
        diff = SurgicalDiffBuilder.build_surgical_diff(self.orig_lines, sr_text, "pingpong.py")
        self.assertTrue(diff.startswith("--- a/pingpong.py"))
        self.assertIn("-PADDLE_HEIGHT = 80", diff)
        self.assertIn("+PADDLE_HEIGHT = 120", diff)
        del_lines = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
        self.assertEqual(len(del_lines), 1)

    def test_single_line_change_from_variable_snippet(self):
        snippet = "```python\nPADDLE_HEIGHT = 120\n```"
        diff = SurgicalDiffBuilder.build_surgical_diff(self.orig_lines, snippet, "pingpong.py")
        self.assertIn("-PADDLE_HEIGHT = 80", diff)
        self.assertIn("+PADDLE_HEIGHT = 120", diff)
        del_lines = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
        self.assertEqual(len(del_lines), 1)

    def test_partial_function_snippet_splicing(self):
        new_func = (
            "def update(self):\n"
            "    self.x += self.vx * 2\n"
            "    self.y += self.vy * 2\n"
        )
        diff = SurgicalDiffBuilder.build_surgical_diff(self.orig_lines, f"```python\n{new_func}\n```", "pingpong.py")
        self.assertIn("-        self.x += self.vx", diff)
        self.assertIn("+        self.x += self.vx * 2", diff)
        self.assertNotIn("+pygame.init()", diff)
        self.assertNotIn("-pygame.init()", diff)


    def test_fuzzy_hunk_anchor_drift_realignment(self):
        # Diff header says line 2, but PADDLE_HEIGHT is at line 8
        drifting_diff = (
            "--- a/pingpong.py\n"
            "+++ b/pingpong.py\n"
            "@@ -2,3 +2,3 @@\n"
            " PADDLE_WIDTH = 15\n"
            "-PADDLE_HEIGHT = 80\n"
            "+PADDLE_HEIGHT = 140\n"
            " BALL_RADIUS = 8\n"
        )
        patched = PatchEngine._apply_unified_diff(self.orig_lines, drifting_diff.splitlines())
        patched_text = "\n".join(patched)
        self.assertIn("PADDLE_HEIGHT = 140", patched_text)
        self.assertNotIn("PADDLE_HEIGHT = 80", patched_text)
        self.assertIn("import pygame", patched_text)
        self.assertIn("import sys", patched_text)

    def test_validate_and_heal_diff_catches_and_repairs(self):
        valid_diff = (
            "--- a/pingpong.py\n"
            "+++ b/pingpong.py\n"
            "@@ -6,3 +6,3 @@\n"
            "-PADDLE_HEIGHT = 80\n"
            "+PADDLE_HEIGHT = 120\n"
        )
        is_valid, final_diff, err = PatchEngine.validate_and_heal_diff(self.orig_lines, valid_diff, "pingpong.py")
        self.assertTrue(is_valid)
        self.assertIsNone(err)

    def test_prevent_accidental_file_wipe_on_dev_null_header(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "pingpong.py"
            f.write_text(SAMPLE_PING_PONG)

            stray_diff = (
                "--- /dev/null\n"
                "+++ b/pingpong.py\n"
                "@@ -7,3 +7,3 @@\n"
                "-PADDLE_HEIGHT = 80\n"
                "+PADDLE_HEIGHT = 150\n"
            )
            res = PatchEngine.apply_patch_hunks("pingpong.py", stray_diff, td)
            self.assertEqual(res["status"], "success")

            content = f.read_text()
            self.assertGreater(len(content.splitlines()), 40)
            self.assertIn("PADDLE_HEIGHT = 150", content)
            self.assertIn("import pygame", content)

    def test_compute_diff_api_endpoint(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "pingpong.py"
            f.write_text(SAMPLE_PING_PONG)

            client = TestClient(app)
            resp = client.post("/api/agent/compute-diff", json={
                "target_file": "pingpong.py",
                "code_or_snippet": "PADDLE_HEIGHT = 130",
                "workspace_path": td
            })
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "success")
            self.assertTrue(data["valid"])
            self.assertIn("+PADDLE_HEIGHT = 130", data["diff"])
            self.assertIn("-PADDLE_HEIGHT = 80", data["diff"])

    def test_typescript_single_line_change_does_not_wipe_file(self):
        ts_code = [
            "import { useState, useEffect } from 'react';",
            "",
            "export const API_BASE_URL = 'https://api.example.com';",
            "export const MAX_RETRIES: number = 3;",
            "export const TIMEOUT_MS = 5000;",
            "",
            "export function useUserData(userId: string) {",
            "  const [user, setUser] = useState(null);",
            "  useEffect(() => {",
            "    fetch(`${API_BASE_URL}/users/${userId}`)",
            "      .then(res => res.json())",
            "      .then(data => setUser(data));",
            "  }, [userId]);",
            "  return user;",
            "}"
        ]
        snippet = "```typescript\nexport const MAX_RETRIES: number = 10;\n```"
        diff = SurgicalDiffBuilder.build_surgical_diff(ts_code, snippet, "userHooks.ts")
        del_lines = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
        add_lines = [l for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
        self.assertEqual(len(del_lines), 1)
        self.assertEqual(len(add_lines), 1)
        self.assertIn("-export const MAX_RETRIES: number = 3;", del_lines[0])
        self.assertIn("+export const MAX_RETRIES: number = 10;", add_lines[0])

    def test_typescript_function_splicing_with_curly_braces(self):
        ts_code = [
            "import axios from 'axios';",
            "",
            "export function calculateDiscount(price: number): number {",
            "  if (price > 100) {",
            "    return price * 0.9;",
            "  }",
            "  return price;",
            "}",
            "",
            "export function formatCurrency(val: number): string {",
            "  return `$${val.toFixed(2)}`;",
            "}"
        ]
        snippet = (
            "```ts\n"
            "export function calculateDiscount(price: number): number {\n"
            "  if (price > 200) {\n"
            "    return price * 0.8;\n"
            "  }\n"
            "  return price * 0.95;\n"
            "}\n"
            "```"
        )
        diff = SurgicalDiffBuilder.build_surgical_diff(ts_code, snippet, "pricing.ts")
        self.assertIn("+    return price * 0.8;", diff)
        self.assertNotIn("formatCurrency", [l for l in diff.splitlines() if l.startswith("+") or l.startswith("-")])

    def test_css_surgical_diff_from_search_replace(self):
        css_lines = [
            ":root {",
            "  --primary-color: #3b82f6;",
            "  --secondary-color: #10b981;",
            "  --bg-dark: #0f172a;",
            "}",
            "",
            ".button-submit {",
            "  background: var(--primary-color);",
            "  border-radius: 4px;",
            "}"
        ]
        sr = (
            "<<<<<<< SEARCH\n"
            "  --primary-color: #3b82f6;\n"
            "=======\n"
            "  --primary-color: #8b5cf6;\n"
            ">>>>>>> REPLACE"
        )
        diff = SurgicalDiffBuilder.build_surgical_diff(css_lines, sr, "theme.css")
        del_lines = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
        add_lines = [l for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
        self.assertEqual(len(del_lines), 1)
        self.assertEqual(len(add_lines), 1)
        self.assertIn("--primary-color: #8b5cf6;", add_lines[0])

    def test_rust_single_line_change(self):
        rust_lines = [
            "use std::time::Duration;",
            "",
            "pub const DEFAULT_PORT: u16 = 8080;",
            "pub const TIMEOUT_SECS: u64 = 30;",
            "",
            "pub fn init_server() {",
            "    println!(\"Starting server...\");",
            "}"
        ]
        snippet = "```rust\npub const TIMEOUT_SECS: u64 = 60;\n```"
        diff = SurgicalDiffBuilder.build_surgical_diff(rust_lines, snippet, "config.rs")
        del_lines = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
        add_lines = [l for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
        self.assertEqual(len(del_lines), 1)
        self.assertEqual(len(add_lines), 1)
        self.assertIn("TIMEOUT_SECS: u64 = 60", add_lines[0])

    def test_preferred_line_disambiguation(self):
        # File has duplicate blocks at beginning and later down
        lines = [
            "# Header block",
            "def calculate():",
            "    val = 10",
            "    return val",
            "",
            "# Many lines intervening...",
        ] + [f"# comment line {i}" for i in range(50)] + [
            "def calculate_active():",
            "    val = 10",
            "    return val",
            "",
            "def calculate():",
            "    val = 10",
            "    return val",
        ]
        sr = (
            "<<<<<<< SEARCH\n"
            "    val = 10\n"
            "=======\n"
            "    val = 99\n"
            ">>>>>>> REPLACE"
        )
        # Without preferred_line, it hits the first occurrence near line 1
        diff_no_pref = SurgicalDiffBuilder.build_surgical_diff(lines, sr, "test.py")
        self.assertIn("# Header block", diff_no_pref)

        # With preferred_line pointing to the occurrence near line 62
        diff_with_pref = SurgicalDiffBuilder.build_surgical_diff(lines, sr, "test.py", preferred_line=62)
        self.assertNotIn("# Header block", diff_with_pref)
        self.assertIn("def calculate():", diff_with_pref)

    def test_nested_indent_preservation_when_diff_base_indent_zero(self):
        orig_lines = [
            "def outer():",
            "    def inner():",
            "        x = 1",
            "        return x",
        ]
        # Diff hunk has replacement where diff_base_indent is ""
        diff_text = (
            "--- a/test.py\n"
            "+++ b/test.py\n"
            "@@ -3,2 +3,2 @@\n"
            "-        x = 1\n"
            "-        return x\n"
            "+        x = 42\n"
            "+        return x * 2\n"
        )
        patched = PatchEngine._apply_unified_diff(orig_lines, diff_text.splitlines())
        self.assertEqual(patched[2], "        x = 42")
        self.assertEqual(patched[3], "        return x * 2")

    def test_syntax_repair_escaped_quotes(self):
        orig = "def hello():\n    pass\n"
        diff_with_escaped_quotes = (
            "--- a/test.py\n"
            "+++ b/test.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def hello():\n"
            "+    \\\"\\\"\\\"Placeholder docstring.\\\"\\\"\\\"\n"
            "     pass\n"
        )
        valid, healed, err = PatchEngine.validate_and_heal_diff(orig, diff_with_escaped_quotes, "test.py")
        self.assertTrue(valid)
        self.assertNotIn('\\"\\"\\"', healed)
        self.assertIn('"""Placeholder docstring."""', healed)

    def test_multi_edit_hunk_with_top_level_import_and_inner_variable(self):
        orig_code = (
            "from test2 import UnionFind, Trie\n"
            "\n"
            "def main() -> None:\n"
            "    uf = UnionFind()\n"
            "    elements = [\"a\", \"b\", \"c\"]\n"
            "    for e in elements:\n"
            "        uf.make_set(e)\n"
        )
        diff_text = (
            "--- a/test1.py\n"
            "+++ b/test1.py\n"
            "@@ -1,7 +1,8 @@\n"
            " from test2 import UnionFind, Trie\n"
            "+from typing import List\n"
            " \n"
            " def main() -> None:\n"
            "     uf = UnionFind()\n"
            "-    elements = [\"a\", \"b\", \"c\"]\n"
            "+    elements: List[str] = [\"a\", \"b\", \"c\"]\n"
            "     for e in elements:\n"
        )
        patched = PatchEngine._apply_unified_diff(orig_code.splitlines(), diff_text.splitlines())
        patched_str = "\n".join(patched)
        self.assertIn("from typing import List\n\ndef main()", patched_str)
        self.assertIn("elements: List[str] = [\"a\", \"b\", \"c\"]", patched_str)
        import ast
        ast.parse(patched_str)

    def test_syntax_repair_misplaced_import(self):
        bad_code = (
            "def main():\n"
            "    x = 1\n"
            "from typing import List\n"
            "    y: List[int] = [x]\n"
        )
        repaired = PatchEngine._attempt_syntax_repair([], [], bad_code)
        self.assertIsNotNone(repaired)
        import ast
        ast.parse(repaired)
        self.assertIn("from typing import List", repaired)

    def test_single_line_print_replacement(self):
        orig_lines = ['print ( " hello " )']
        generated = '```python\nprint("human")\n```'
        diff = PatchEngine.build_surgical_diff(orig_lines, generated, "app.py")
        self.assertIn('-print ( " hello " )', diff)
        self.assertIn('+print("human")', diff)
        valid, healed, err = PatchEngine.validate_and_heal_diff(orig_lines, diff, "app.py")
        self.assertTrue(valid)

    def test_single_statement_with_header_comment(self):
        orig_lines = ['print ( " hello " )']
        generated = '```python\n# app.py\nprint("human")\n```'
        diff = PatchEngine.build_surgical_diff(orig_lines, generated, "app.py")
        self.assertIn('-print ( " hello " )', diff)
        self.assertIn('+print("human")', diff)
        valid, healed, err = PatchEngine.validate_and_heal_diff(orig_lines, diff, "app.py")
        self.assertTrue(valid)

    def test_multi_line_print_replacement_with_cursor(self):
        orig_lines = ['# header', 'print ( " hello " )', '# footer']
        generated = '```python\nprint("human")\n```'
        diff = PatchEngine.build_surgical_diff(orig_lines, generated, "app.py", preferred_line=2)
        self.assertIn('-print ( " hello " )', diff)
        self.assertIn('+print("human")', diff)
        valid, healed, err = PatchEngine.validate_and_heal_diff(orig_lines, diff, "app.py")
        self.assertTrue(valid)

    def test_bare_at_at_diff_applies_cleanly(self):
        orig = [
            "from fastapi import FastAPI, Depends, HTTPException",
            "import os",
            "",
            "def foo():",
            "    return 42",
        ]
        # Diff with bare @@ header
        diff_lines = [
            "--- a/app.py",
            "+++ b/app.py",
            "@@",
            "-from fastapi import FastAPI, Depends, HTTPException",
            "+from fastapi import FastAPI, Depends",
        ]
        applied = PatchEngine._apply_unified_diff(orig, diff_lines)
        self.assertIn("from fastapi import FastAPI, Depends", applied)
        self.assertNotIn("from fastapi import FastAPI, Depends, HTTPException", applied)
        self.assertIn("def foo():", applied)
        self.assertIn("    return 42", applied)

    def test_unmatched_deletion_does_not_wipe_innocent_functions(self):
        orig = [
            "from fastapi import FastAPI",
            "",
            "class SimpleCache:",
            "    pass",
            "",
            "async def long_running_computation(task_id: str, payload: dict) -> None:",
            "    '''heavy work'''",
            "    return None",
        ]
        # Diff with an unmatched deletion hunk at the end
        diff_lines = [
            "--- a/app.py",
            "+++ b/app.py",
            "@@",
            "-class ProcessResult(BaseModel):",
            "-    task_id: str",
        ]
        applied = PatchEngine._apply_unified_diff(orig, diff_lines)
        # Verify innocent functions were NOT deleted
        self.assertIn("class SimpleCache:", applied)
        self.assertIn("async def long_running_computation(task_id: str, payload: dict) -> None:", applied)
        self.assertIn("    '''heavy work'''", applied)

    def test_placeholder_file_refusal(self):
        with tempfile.TemporaryDirectory() as td:
            # Attempting to apply a patch to "FILE" when no matching file exists should refuse
            res = PatchEngine.apply_patch_hunks("FILE", "@@\n+x = 1\n", td)
            self.assertEqual(res["status"], "error")
            self.assertIn("Refusing to create", res["message"])
            # Ensure no extensionless file was created on disk
            self.assertFalse((Path(td) / "FILE").exists())

    def test_try_except_sibling_clauses_insertion(self):
        orig = [
            "def calc():",
            "    while True:",
            "        try:",
            "            expr = input('Enter: ')",
            "            result = eval(expr)",
            "            return result",
            "        except Exception as e:",
            "            print('Error:', e)",
        ]
        diff = (
            "--- a/calc.py\n"
            "+++ b/calc.py\n"
            "@@ -5,4 +5,10 @@\n"
            "             result = eval(expr)\n"
            "             return result\n"
            "+        except SyntaxError:\n"
            "+            print('Syntax Error')\n"
            "+            continue\n"
            "+        except NameError:\n"
            "+            print('Name Error')\n"
            "+            continue\n"
            "         except Exception as e:\n"
            "             print('Error:', e)\n"
        )
        with tempfile.TemporaryDirectory() as td:
            target_f = Path(td) / "calc.py"
            target_f.write_text("\n".join(orig) + "\n")
            res = PatchEngine.apply_patch_hunks("calc.py", diff, td)
            self.assertEqual(res["status"], "success", f"Failed with: {res.get('message')}")
            content = target_f.read_text()
            import ast
            ast.parse(content)
            self.assertIn("        except SyntaxError:\n            print('Syntax Error')", content)
            self.assertIn("        except NameError:\n            print('Name Error')", content)
            self.assertIn("        except Exception as e:", content)

    def test_multi_clause_try_except_repair(self):
        # Even if clauses were misindented deeper into the try block (12 spaces instead of 8)
        bad_code = (
            "def calc():\n"
            "    while True:\n"
            "        try:\n"
            "            expr = input('Enter: ')\n"
            "            result = eval(expr)\n"
            "            return result\n"
            "            except SyntaxError:\n"
            "                print('Syntax Error')\n"
            "                continue\n"
            "            except NameError:\n"
            "                print('Name Error')\n"
            "                continue\n"
            "        except Exception as e:\n"
            "            print('Error:', e)\n"
        )
        repaired = PatchEngine._attempt_syntax_repair([], [], bad_code, "calc.py")
        self.assertIsNotNone(repaired)
        import ast
        ast.parse(repaired)
        self.assertIn("        except SyntaxError:", repaired)
        self.assertIn("        except NameError:", repaired)
        self.assertIn("        except Exception as e:", repaired)

if __name__ == "__main__":
    unittest.main()


