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

if __name__ == "__main__":
    unittest.main()
