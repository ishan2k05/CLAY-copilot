import fnmatch
import re
from pathlib import Path
from typing import List, Dict, Any

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

    _custom_patterns_raw = ".env, *.pem, credentials.json, id_rsa, *.key"
    _custom_patterns_list = [".env", "*.pem", "credentials.json", "id_rsa", "*.key"]

    @classmethod
    def set_custom_patterns(cls, patterns_str: str) -> None:
        """Sets user-configured exclusion patterns (comma-separated globs or file names)."""
        if not patterns_str or not patterns_str.strip():
            return
        cls._custom_patterns_raw = patterns_str
        cls._custom_patterns_list = [p.strip().lower() for p in patterns_str.split(",") if p.strip()]

    @classmethod
    def get_custom_patterns(cls) -> str:
        return cls._custom_patterns_raw

    @staticmethod
    def mask_text(content: str, enabled: bool = True) -> str:
        """Applies regex heuristics to redact credentials and tokens. If disabled, returns original content."""
        if not enabled:
            return content
        sanitized = content
        for pattern, replacement in SECRET_PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)
        return sanitized

    @classmethod
    def is_file_sensitive(cls, file_path: str) -> bool:
        """Determines if a file should be excluded based on name, extension, or custom patterns."""
        path = Path(file_path)
        name = path.name.lower()
        rel_str = str(path).lower()

        # Check default extensions
        if path.suffix.lower() in EXCLUDED_EXTENSIONS:
            return True

        # Check default sensitive substrings
        for sub in EXCLUDED_SUBSTRINGS:
            if sub in name:
                return True

        # Check user-configured custom patterns
        for pat in getattr(cls, "_custom_patterns_list", []):
            if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel_str, pat) or pat in name:
                return True

        return False

    @classmethod
    def scan_repository(cls, workspace_path: str) -> Dict[str, Any]:
        """
        Deep security audit of the repository.
        Scans all files for leaked secrets, credentials, API keys, private keys,
        and sensitive configuration files (.env, credentials.json, id_rsa, etc.).
        """
        root = Path(workspace_path).resolve()
        if not root.exists() or not root.is_dir():
            return {
                "status": "clean",
                "scanned_files": 0,
                "findings_count": 0,
                "findings": [],
                "summary": "Zero Leaked Secrets Detected"
            }

        EXCLUDED_DIRS = {
            "venv", ".venv", "env", "node_modules", "dist", "build",
            "__pycache__", ".git", ".idea", ".vscode", ".gemini", ".system_generated"
        }

        findings = []
        files_scanned = 0

        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in EXCLUDED_DIRS for part in p.parts):
                continue
            if any(part.startswith(".") and part not in [".env", ".env.local", ".env.production", ".env.staging"] for part in p.parts):
                continue

            files_scanned += 1
            try:
                rel_path = str(p.relative_to(root))
            except Exception:
                rel_path = p.name

            # 1. Check if the file itself is a sensitive file (.env, id_rsa, .pem, etc.)
            if cls.is_file_sensitive(str(p)):
                findings.append({
                    "file": rel_path,
                    "type": "Sensitive Configuration File",
                    "severity": "HIGH",
                    "detail": f"File name or extension matches sensitive credential patterns ({p.name})",
                    "line": 1
                })

            # 2. Check file content for secret patterns if size < 2MB
            try:
                if p.stat().st_size < 2 * 1024 * 1024:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                    lines = content.splitlines()
                    for line_idx, line in enumerate(lines, 1):
                        stripped = line.strip()
                        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                            continue
                        for pattern, replacement in SECRET_PATTERNS:
                            m = pattern.search(line)
                            if m:
                                pattern_name = replacement.strip("[]")
                                findings.append({
                                    "file": rel_path,
                                    "type": pattern_name.replace("_", " ").title(),
                                    "severity": "CRITICAL" if "KEY" in replacement or "TOKEN" in replacement else "HIGH",
                                    "detail": f"Pattern {pattern_name} detected",
                                    "line": line_idx
                                })
                                break
            except Exception:
                continue

        is_clean = len(findings) == 0
        summary = "Zero Leaked Secrets Detected" if is_clean else f"{len(findings)} Potential Secrets Detected"

        return {
            "status": "clean" if is_clean else "warning",
            "scanned_files": files_scanned,
            "findings_count": len(findings),
            "findings": findings[:50],
            "summary": summary
        }
