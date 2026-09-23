import unittest
from app.parser.ast_chunker import PythonASTChunker
from app.security.secret_masker import SecretMasker

class TestCodebaseCopilotBackend(unittest.TestCase):

    def test_ast_chunker_extracts_classes_and_functions(self):
        sample_code = (
            "class AuthService:\n"
            '    """Handles authentication and tokens."""\n'
            "    def __init__(self, repo):\n"
            "        self.repo = repo\n\n"
            "    def login(self, username, password):\n"
            '        """Authenticates user."""\n'
            "        user = self.repo.get_by_username(username)\n"
            "        return user\n"
        )
        chunker = PythonASTChunker()
        symbols, relationships, chunks = chunker.parse_file("auth.py", sample_code)

        self.assertGreaterEqual(len(symbols), 3)
        class_syms = [s for s in symbols if s.symbol_type == "class"]
        self.assertEqual(len(class_syms), 1)
        self.assertEqual(class_syms[0].name, "AuthService")
        self.assertEqual(class_syms[0].docstring, "Handles authentication and tokens.")

        fn_syms = [s for s in symbols if s.symbol_type == "method"]
        self.assertTrue(any(s.name == "AuthService.login" for s in fn_syms))

        # Verify calls relationship
        calls = [r for r in relationships if r.relation_type == "calls"]
        self.assertTrue(any("repo.get_by_username" in r.target_symbol for r in calls))

    def test_secret_masker_redacts_credentials(self):
        text = 'aws_access_key: AKIA1234567890ABCDEF, user_password = "SuperSecretPassword123!"'
        masked = SecretMasker.mask_text(text)

        self.assertNotIn("AKIA1234567890ABCDEF", masked)
        self.assertIn("[REDACTED_AWS_KEY]", masked)
        self.assertNotIn("SuperSecretPassword123!", masked)
        self.assertIn("[REDACTED_SECRET]", masked)

    def test_sensitive_file_detector(self):
        self.assertTrue(SecretMasker.is_file_sensitive(".env"))
        self.assertTrue(SecretMasker.is_file_sensitive("config.env.local"))
        self.assertTrue(SecretMasker.is_file_sensitive("private_key.pem"))
        self.assertFalse(SecretMasker.is_file_sensitive("main.py"))

    def test_scan_repository_detection(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Create a regular clean file
            (root / "clean.py").write_text("print('hello')\n")
            clean_res = SecretMasker.scan_repository(td)
            self.assertEqual(clean_res["status"], "clean")
            self.assertEqual(clean_res["findings_count"], 0)
            self.assertEqual(clean_res["summary"], "Zero Leaked Secrets Detected")

            # Create a sensitive file
            (root / ".env").write_text("DATABASE_URL=postgres://localhost\n")
            # Create a file with leaked AWS key
            (root / "leak.py").write_text('key = "AKIA1234567890ABCDEF"\n')

            dirty_res = SecretMasker.scan_repository(td)
            self.assertEqual(dirty_res["status"], "warning")
            self.assertGreaterEqual(dirty_res["findings_count"], 2)
            self.assertIn("Potential Secrets Detected", dirty_res["summary"])

    def test_ast_chunker_preserves_main_entrypoint_and_calls(self):
        code = (
            "def main():\n"
            '    filename = "example.txt"\n'
            '    content = "Hello, this is a simple file operation in Python."\n'
            '    with open(filename, "w") as file:\n'
            '        file.write(content)\n'
            '    print(f"Successfully wrote to {filename}")\n'
            '    try:\n'
            '        with open(filename, "r") as file:\n'
            '            data = file.read()\n'
            '        print(f"Read from {filename}: {data}")\n'
            '    except FileNotFoundError:\n'
            '        print("The file was not found.")\n\n'
            'if __name__ == "__main__":\n'
            '    main()\n'
        )
        chunker = PythonASTChunker()
        symbols, relationships, chunks = chunker.parse_file("test_c.py", code)

        # 1. Main function is extracted
        self.assertTrue(any(s.name == "main" for s in symbols))
        # 2. Entrypoint block is extracted
        entrypoint_syms = [s for s in symbols if s.symbol_type == "entrypoint"]
        self.assertGreaterEqual(len(entrypoint_syms), 1)
        # 3. Main call relationship is detected
        calls_main = [r for r in relationships if r.target_symbol == "main" and r.relation_type == "calls"]
        self.assertGreaterEqual(len(calls_main), 1)
        # 4. Entrypoint and whole-file chunk exist in chunks
        chunk_contents = " ".join(c.content for c in chunks)
        self.assertIn('if __name__ == "__main__":', chunk_contents)
        self.assertIn("main()", chunk_contents)

if __name__ == "__main__":
    unittest.main()
