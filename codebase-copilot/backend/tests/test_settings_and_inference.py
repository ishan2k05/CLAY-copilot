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

from fastapi.testclient import TestClient

from app.main import app, ollama_provider, runtime_settings, load_persisted_settings, save_persisted_settings, SETTINGS_FILE
from app.security.secret_masker import SecretMasker


class TestSettingsAndInferenceOptions(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        # Store original settings
        self.orig_settings = dict(runtime_settings)
        self.orig_temp = ollama_provider.temperature
        self.orig_ctx = ollama_provider.num_ctx
        self.orig_predict = ollama_provider.num_predict

    def tearDown(self):
        # Restore original settings
        ollama_provider.temperature = self.orig_temp
        ollama_provider.num_ctx = self.orig_ctx
        ollama_provider.num_predict = self.orig_predict
        for k, v in self.orig_settings.items():
            runtime_settings[k] = v
        save_persisted_settings()

    def test_save_all_settings_and_retrieve(self):
        """Verify that all settings from Image 2 can be configured, saved, and retrieved."""
        payload = {
            "temperature": 0.45,
            "num_ctx": 16384,
            "num_predict": 1024,
            "privacy_filter": False,
            "exclusion_patterns": ".env, *.pem, credentials.json, id_rsa, *.key, *.confidential, token_secret.txt",
            "security_mode": "full_access",
            "allow_network": False
        }
        res = self.client.post("/api/settings", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertEqual(data["temperature"], 0.45)
        self.assertEqual(data["num_ctx"], 16384)
        self.assertEqual(data["num_predict"], 1024)
        self.assertFalse(data["privacy_filter"])
        self.assertEqual(data["exclusion_patterns"], payload["exclusion_patterns"])
        self.assertEqual(ollama_provider.temperature, 0.45)
        self.assertEqual(ollama_provider.num_ctx, 16384)
        self.assertEqual(ollama_provider.num_predict, 1024)

        # GET /api/settings should return matching state
        get_res = self.client.get("/api/settings")
        self.assertEqual(get_res.status_code, 200)
        get_data = get_res.json()
        self.assertEqual(get_data["temperature"], 0.45)
        self.assertEqual(get_data["num_ctx"], 16384)
        self.assertEqual(get_data["num_predict"], 1024)
        self.assertFalse(get_data["privacy_filter"])
        self.assertEqual(get_data["exclusion_patterns"], payload["exclusion_patterns"])

    def test_settings_disk_persistence(self):
        """Verify settings write to runtime_settings.json and reload cleanly on restart."""
        payload = {
            "temperature": 0.70,
            "num_ctx": 65536,
            "num_predict": 8192,
            "privacy_filter": True,
            "exclusion_patterns": "*.secret, custom_secrets.env"
        }
        self.client.post("/api/settings", json=payload)

        # Verify file exists on disk
        self.assertTrue(SETTINGS_FILE.exists())
        disk_data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(disk_data["temperature"], 0.70)
        self.assertEqual(disk_data["num_ctx"], 65536)
        self.assertEqual(disk_data["num_predict"], 8192)
        self.assertTrue(disk_data["privacy_filter"])
        self.assertEqual(disk_data["exclusion_patterns"], "*.secret, custom_secrets.env")

        # Simulate fresh process startup by wiping provider options and calling load_persisted_settings()
        ollama_provider.temperature = 0.2
        ollama_provider.num_ctx = 32768
        ollama_provider.num_predict = 2048

        load_persisted_settings()
        self.assertEqual(ollama_provider.temperature, 0.70)
        self.assertEqual(ollama_provider.num_ctx, 65536)
        self.assertEqual(ollama_provider.num_predict, 8192)

    def test_custom_privacy_redaction_patterns(self):
        """Verify SecretMasker dynamically excludes files matching custom patterns."""
        custom_patterns = ".env, *.pem, credentials.json, id_rsa, *.key, *.confidential, auth_token.bin"
        SecretMasker.set_custom_patterns(custom_patterns)

        # Standard file
        self.assertFalse(SecretMasker.is_file_sensitive("/repo/src/app.py"))
        self.assertFalse(SecretMasker.is_file_sensitive("/repo/src/main.ts"))

        # Default sensitive files
        self.assertTrue(SecretMasker.is_file_sensitive("/repo/.env"))
        self.assertTrue(SecretMasker.is_file_sensitive("/repo/certs/cert.pem"))
        self.assertTrue(SecretMasker.is_file_sensitive("/repo/id_rsa"))

        # Newly added custom patterns
        self.assertTrue(SecretMasker.is_file_sensitive("/repo/docs/strategy.confidential"))
        self.assertTrue(SecretMasker.is_file_sensitive("/repo/auth_token.bin"))

    def test_zero_leak_privacy_guard_toggle(self):
        """Verify Zero-Leak Privacy Guard toggle redacts secrets when True and preserves them when False."""
        sample_code = "api_key = 'sk_live_998877665544332211'\npassword = 'super_secret_db_password'"

        # Guard ENABLED (default): secrets must be masked
        masked = SecretMasker.mask_text(sample_code, enabled=True)
        self.assertNotIn("sk_live_998877665544332211", masked)
        self.assertNotIn("super_secret_db_password", masked)
        self.assertIn("[REDACTED_SECRET]", masked)

        # Guard DISABLED: secrets pass through unchanged
        raw = SecretMasker.mask_text(sample_code, enabled=False)
        self.assertIn("sk_live_998877665544332211", raw)
    def test_standalone_server_settings_and_persistence(self):
        """Verify standalone_server runtime_settings and persistence match main backend."""
        import standalone_server
        orig_standalone = dict(standalone_server.runtime_settings)
        try:
            standalone_server.runtime_settings["temperature"] = 0.55
            standalone_server.runtime_settings["num_ctx"] = 8192
            standalone_server.runtime_settings["num_predict"] = 1024
            standalone_server.runtime_settings["privacy_filter"] = False
            standalone_server.runtime_settings["exclusion_patterns"] = "*.custom, .secrets"
            standalone_server.save_persisted_settings()

            # Verify persisted to disk
            self.assertTrue(standalone_server.SETTINGS_FILE.exists())
            disk = json.loads(standalone_server.SETTINGS_FILE.read_text(encoding="utf-8"))
            self.assertEqual(disk["temperature"], 0.55)
            self.assertEqual(disk["num_ctx"], 8192)
            self.assertEqual(disk["num_predict"], 1024)
            self.assertFalse(disk["privacy_filter"])

            # Reload
            standalone_server.runtime_settings["temperature"] = 0.2
            standalone_server.load_persisted_settings()
            self.assertEqual(standalone_server.runtime_settings["temperature"], 0.55)
            self.assertEqual(standalone_server.runtime_settings["num_ctx"], 8192)
        finally:
            standalone_server.runtime_settings = orig_standalone
            standalone_server.save_persisted_settings()


if __name__ == "__main__":
    unittest.main()
