import asyncio
import os
import platform
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional
from app.security.guardrails import CommandSafetyGuard

class TerminalAgent:
    """
    Cross-Platform (Linux + Windows) Sandboxed Subprocess Runner.
    Auto-detects host OS, translates shell commands appropriately,
    and applies strict CommandSafetyGuard security filtering.
    """

    def __init__(self, workspace_path: str):
        self.workspace_path = str(Path(workspace_path).resolve())
        self.os_type = platform.system().lower() # 'windows', 'linux', 'darwin'
        self.is_windows = self.os_type == "windows"

    def get_system_info(self) -> Dict[str, Any]:
        """Returns detected host OS, shell type, and recommended commands."""
        if self.is_windows:
            shell_name = "PowerShell / CMD"
            recommended = ["dir", "python --version", "git status", "pytest", "npm.cmd test"]
        else:
            shell_name = "bash" if Path("/bin/bash").exists() else "sh"
            recommended = ["ls -la", "python3 --version", "git status", "pytest", "npm test"]

        return {
            "os": self.os_type,
            "platform": platform.platform(),
            "is_windows": self.is_windows,
            "shell": shell_name,
            "workspace": self.workspace_path,
            "recommended_commands": recommended
        }

    # Safe commands permitted in Strict Mode without elevated review
    STRICT_SAFE_COMMAND_PREFIXES = (
        "ls", "dir", "git status", "git diff", "git log", "git show",
        "pytest", "python -m unittest", "python3 -m unittest", "npm test", "npm.cmd test",
        "python --version", "python3 --version", "node -v", "npm -v",
        "cat", "type", "echo", "pwd", "whoami"
    )

    # Commands requiring network access
    NETWORK_COMMANDS = (
        "curl", "wget", "git clone", "git pull", "git push", "git fetch",
        "npm install", "npm i", "pip install", "pip3 install", "ssh", "scp", "sftp"
    )

    async def run_command(
        self,
        command: str,
        timeout: int = 45,
        security_mode: str = "sandboxed",
        allow_network: bool = True
    ) -> Dict[str, Any]:
        """
        Executes a terminal command safely within the workspace directory,
        strictly enforcing the selected Security Mode (full, sandboxed, strict)
        and Network policies.
        """
        cmd_clean = command.strip()

        # 1. Mode: Strict (Command review & read-only enforcement)
        if security_mode == "strict":
            # In strict mode, only verified safe read-only/testing operations are allowed
            is_strict_safe = any(cmd_clean.startswith(prefix) for prefix in self.STRICT_SAFE_COMMAND_PREFIXES)
            if not is_strict_safe:
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"Execution blocked: Strict mode requires explicit review for modifying command '{command}'.",
                    "passed": False,
                    "os": self.os_type,
                    "security_mode": "strict"
                }

        # 2. Mode: Sandboxed (Default - Destruction Filter + Network Firewall)
        if security_mode in ("sandboxed", "strict"):
            # Multi-OS Command Safety Guardrail
            is_safe, reason = CommandSafetyGuard.validate_command(command, self.os_type)
            if not is_safe:
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"Execution blocked: {reason}",
                    "passed": False,
                    "os": self.os_type,
                    "security_mode": security_mode
                }

            # Network Access Enforcement
            if not allow_network:
                if any(net_cmd in cmd_clean for net_cmd in self.NETWORK_COMMANDS):
                    return {
                        "exit_code": -1,
                        "stdout": "",
                        "stderr": "Execution blocked: Sandboxed network requests are disabled in settings.",
                        "passed": False,
                        "os": self.os_type,
                        "security_mode": security_mode
                    }

        # 3. Mode: Full Access (Permits execution, logs policy mode)

        # 2. Prepare OS-Specific Process Invocation
        try:
            if self.is_windows:
                # Prefer PowerShell on Windows for robust scripting and POSIX alias parity (ls, cat, etc.)
                if shutil.which("powershell.exe"):
                    process = await asyncio.create_subprocess_exec(
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        command,
                        cwd=self.workspace_path,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                else:
                    # Fallback to cmd.exe
                    process = await asyncio.create_subprocess_exec(
                        "cmd.exe",
                        "/c",
                        command,
                        cwd=self.workspace_path,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
            else:
                # Linux & macOS shell runner
                shell_executable = "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"
                process = await asyncio.create_subprocess_shell(
                    command,
                    cwd=self.workspace_path,
                    executable=shell_executable,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout
            )

            stdout = stdout_bytes.decode("utf-8", errors="ignore")
            stderr = stderr_bytes.decode("utf-8", errors="ignore")
            exit_code = process.returncode or 0

            return {
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "passed": (exit_code == 0),
                "os": self.os_type
            }

        except asyncio.TimeoutError:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout} seconds.",
                "passed": False,
                "os": self.os_type
            }
        except Exception as e:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e),
                "passed": False,
                "os": self.os_type
            }

    async def run_tests(self) -> Dict[str, Any]:
        """Auto-detects and runs available test runners with OS compatibility."""
        root = Path(self.workspace_path)

        has_pytest = (root / "pytest.ini").exists() or (root / "tests").exists() or any(root.glob("test_*.py"))
        has_npm = (root / "package.json").exists()

        if has_pytest:
            cmd = "python -m pytest" if self.is_windows else "pytest"
            return await self.run_command(cmd)
        elif has_npm:
            cmd = "npm.cmd test" if self.is_windows else "npm test"
            return await self.run_command(cmd)
        else:
            cmd = "python -m unittest" if self.is_windows else "python3 -m unittest"
            return await self.run_command(cmd)
