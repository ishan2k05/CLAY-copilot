import re
from typing import Tuple, Dict, Any, List

class PromptInjectionGuard:
    """
    Defense-in-depth prompt injection detector.
    Scans user prompts and repository context for adversarial patterns,
    jailbreak attempts, system prompt overrides, and delimiter manipulation.
    """

    INJECTION_PATTERNS = [
        # Direct instruction overrides
        (re.compile(r"(?i)\b(ignore|disregard|forget|bypass|override)\s+(all\s+)?(previous|prior|above|system)\s+(instructions|prompts|rules|commands)"), "instruction_override"),
        (re.compile(r"(?i)\b(you\s+must\s+now|from\s+now\s+on\s+you\s+are|act\s+as)\s+(an\s+unrestricted|a\s+jailbroken|dan\s+mode|chaos\s+mode)"), "jailbreak_persona"),
        (re.compile(r"(?i)\b(system\s+override|admin\s+mode\s+enabled|root\s+access\s+granted)"), "system_override_spoof"),
        # Prompt / Instruction leak attempts
        (re.compile(r"(?i)\b(reveal|print|display|dump|repeat|output)\s+(your\s+)?(system\s+prompt|initial\s+instructions|system\s+message|base\s+instructions)"), "prompt_leak_probe"),
        # Special delimiter injection / token breakout attempts
        (re.compile(r"(?i)(<\|im_start\|>|<\|im_end\|>|\[SYSTEM_PROMPT\]|<\/system>|\[INST\]|\[\/INST\])"), "delimiter_breakout"),
        (re.compile(r"(?i)###\s*(System|Instruction|Assistant):"), "markdown_role_spoof"),
        # Recursive / prompt injection payload wrappers
        (re.compile(r"(?i)\b(base64|rot13|hex)\s+decode\s+and\s+execute"), "obfuscated_payload")
    ]

    @classmethod
    def scan_prompt(cls, text: str) -> Tuple[bool, str, str]:
        """
        Scans input for prompt injection signatures.
        Returns: (is_safe: bool, reason: str, sanitized_text: str)
        """
        if not text:
            return True, "empty_text", ""

        for pattern, threat_type in cls.INJECTION_PATTERNS:
            match = pattern.search(text)
            if match:
                return False, f"Prompt injection threat detected: {threat_type} ('{match.group(0)}')", cls.neutralize(text)

        return True, "safe", text

    @classmethod
    def neutralize(cls, text: str) -> str:
        """Neutralizes detected injection tokens without crashing user flow."""
        neutralized = text
        for pattern, _ in cls.INJECTION_PATTERNS:
            neutralized = pattern.sub("[BLOCKED_INJECTION_PATTERN]", neutralized)
        return neutralized


class CommandSafetyGuard:
    """
    Cross-Platform (Linux + Windows) destructive shell command filter.
    Blocks dangerous disk wipes, privilege escalations, fork bombs,
    system shutdowns, and reverse shells.
    """

    # Commands blocked regardless of OS
    GLOBAL_DANGEROUS_PATTERNS = [
        re.compile(r"(?i):\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), # POSIX fork bomb
        re.compile(r"(?i)\b(curl|wget)\b.*\|\s*(sh|bash|zsh|powershell|cmd)\b"), # Remote script pipe execution
        re.compile(r"(?i)\b(nc|ncat|netcat)\b.*-e\s+"), # Netcat reverse shell
        re.compile(r"(?i)\bbash\s+-i\s+>&"), # Bash reverse shell
        re.compile(r"(?i)\b(drop\s+database|truncate\s+table)\b"), # Database catastrophic wipes
    ]

    # Linux-specific dangerous commands
    LINUX_DANGEROUS_PATTERNS = [
        re.compile(r"(?i)\brm\s+-[a-z]*r[a-z]*f\s+(\/|~|\.\.|\*)"), # rm -rf / or ~ or *
        re.compile(r"(?i)\bmkfs(\.[a-z0-9]+)?\s+"), # Disk format
        re.compile(r"(?i)\bdd\s+if=\S+\s+of=\/dev\/[a-z0-9]+"), # Direct block device overwrites
        re.compile(r"(?i)\bchmod\s+(-R\s+)?777\s+\/"), # Root permission degradation
        re.compile(r"(?i)\b(shutdown|reboot|init\s+0|poweroff)\b"), # Machine shutdown
        re.compile(r"(?i)>\s*\/etc\/(passwd|shadow|sudoers)"), # System user overwrite
        re.compile(r"(?i)\bsudo\s+(rm|mkfs|dd|chmod|chown)\b") # Sudo destructive actions
    ]

    # Windows-specific dangerous commands
    WINDOWS_DANGEROUS_PATTERNS = [
        re.compile(r"(?i)\bformat\s+[a-z]:"), # format c:
        re.compile(r"(?i)\bdel\s+(\/[a-z]\s+)*[a-z]:\\"), # del /f /s /q c:\
        re.compile(r"(?i)\b(rd|rmdir)\s+(\/s\s+\/q|\/q\s+\/s)\s+[a-z]:\\"), # rmdir /s /q c:\
        re.compile(r"(?i)\bRemove-Item\b.*-Recurse\b.*(-Force\b.*)?([a-z]:\\|\$env:)"), # PS recursive wipe on drive root
        re.compile(r"(?i)\breg\s+(delete|add)\s+hklm"), # Windows registry damage
        re.compile(r"(?i)\b(shutdown\s+\/s|Stop-Computer)\b"), # Windows shutdown
        re.compile(r"(?i)\btaskkill\s+\/f\s+\/im\s+(csrss\.exe|smss\.exe|svchost\.exe|lsass\.exe)"), # Windows kernel crash
        re.compile(r"(?i)\bpowershell(\.exe)?\s+(-e|-enc|-encodedcommand)\b") # Encoded obfuscated payload
    ]

    @classmethod
    def validate_command(cls, command: str, os_type: str = "linux") -> Tuple[bool, str]:
        """
        Validates command safety for the target operating system.
        os_type: 'linux', 'windows', or 'darwin'
        Returns: (is_allowed: bool, reason: str)
        """
        cmd_clean = command.strip()
        if not cmd_clean:
            return True, "empty_command"

        # 1. Global patterns
        for pattern in cls.GLOBAL_DANGEROUS_PATTERNS:
            if pattern.search(cmd_clean):
                return False, f"Command rejected: matches global high-risk pattern '{pattern.pattern}'"

        # 2. Linux / Unix checks
        if os_type.lower() in ("linux", "darwin"):
            for pattern in cls.LINUX_DANGEROUS_PATTERNS:
                if pattern.search(cmd_clean):
                    return False, f"Command rejected: dangerous Linux operation pattern '{pattern.pattern}'"

        # 3. Windows checks
        if os_type.lower() == "windows":
            for pattern in cls.WINDOWS_DANGEROUS_PATTERNS:
                if pattern.search(cmd_clean):
                    return False, f"Command rejected: dangerous Windows operation pattern '{pattern.pattern}'"

        return True, "safe"


class OutputIntegrityGuard:
    """
    Guards model output against accidental prompt leakage,
    secret reflections, or unprompted harmful system commands.
    """

    SYSTEM_LEAK_PHRASES = [
        re.compile(r"(?i)my\s+system\s+prompt\s+is\s*:"),
        re.compile(r"(?i)here\s+are\s+my\s+exact\s+instructions\s*:"),
        re.compile(r"(?i)<system_directives>")
    ]

    @classmethod
    def sanitize_output(cls, output: str) -> str:
        """Sanitizes model output to prevent system token leaks."""
        clean = output
        for pattern in cls.SYSTEM_LEAK_PHRASES:
            clean = pattern.sub("[REDACTED_SYSTEM_INSTRUCTION]", clean)
        return clean
