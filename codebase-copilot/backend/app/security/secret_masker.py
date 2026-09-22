import re
from pathlib import Path
from typing import List

# Patterns matching credentials and secrets
SECRET_PATTERNS = [
    # AWS Access Key ID
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    # Generic Private Key Block
    (re.compile(r"-----BEGIN (?:[A-Z ]+) PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+) PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    # Generic API Keys / Passwords / Secrets in key=value format (handles special chars)
    (re.compile(r"(?i)(api[_-]?key|secret|password|token|auth[_-]?key)\s*[:=]\s*['\"]([^'\"]{4,})['\"]"), r"\1='[REDACTED_SECRET]'"),
    # Bearer Tokens
    (re.compile(r"(?i)bearer\s+[a-zA-Z0-9_\-\.]{20,}"), "Bearer [REDACTED_TOKEN]")
]

# Sensitive files to exclude completely from indexing and prompt context
EXCLUDED_EXTENSIONS = {".pem", ".key", ".pfx"}
EXCLUDED_SUBSTRINGS = {".env", "id_rsa", "credentials.json", "service-account"}

class SecretMasker:
    """Zero-Leak Privacy Guard: Sanitizes code context before cloud LLM transmission."""

    @staticmethod
    def mask_text(content: str) -> str:
        """Applies regex heuristics to redact credentials and tokens."""
        sanitized = content
        for pattern, replacement in SECRET_PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)
        return sanitized

    @staticmethod
    def is_file_sensitive(file_path: str) -> bool:
        """Determines if a file should be excluded based on name or extension."""
        path = Path(file_path)
        name = path.name.lower()

        # Check extensions
        if path.suffix.lower() in EXCLUDED_EXTENSIONS:
            return True

        # Check sensitive substrings
        for sub in EXCLUDED_SUBSTRINGS:
            if sub in name:
                return True

        return False
