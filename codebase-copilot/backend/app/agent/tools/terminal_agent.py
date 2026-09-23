import asyncio
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional
from app.security.guardrails import CommandSafetyGuard

class TerminalAgent:
    """
    Cross-Platform (Linux + Windows) Sandboxed Subprocess Runner.
    Auto-detects host OS, translates shell commands appropriately,
    and applies strict CommandSafetyGuard security filtering.
    """

    def __init__(self, workspace_path: Optional[str] = None):
        ws = workspace_path.strip() if (workspace_path and isinstance(workspace_path, str)) else ""
        if ws and ws != "." and Path(ws).exists() and Path(ws).is_dir():
            self.workspace_path = str(Path(ws).resolve())
        else:
            # Intelligent workspace resolution: check cwd and repo root
            cwd = Path.cwd().resolve()
            if cwd.name == "backend" and (cwd.parent / "backend").exists():
                self.workspace_path = str(cwd.parent.resolve())
            elif (cwd / "backend").exists() or (cwd / "extension").exists():
                self.workspace_path = str(cwd)
            else:
                # Upstream check to locate codebase-copilot folder
                candidate = Path(__file__).resolve().parent.parent.parent.parent
                if candidate.exists() and (candidate / "backend").exists():
                    self.workspace_path = str(candidate.resolve())
                else:
                    self.workspace_path = str(cwd)

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
        "pytest", "python -m pytest", "python -m unittest", "python3 -m unittest",
        "python -m py_compile", "python3 -m py_compile", "npm test", "npm.cmd test",
        "python --version", "python3 --version", "node -v", "npm -v",
        "cat", "type", "echo", "pwd", "whoami", "get-childitem", "get-content"
    )

    # Commands requiring network access
    NETWORK_COMMANDS = (
        "curl", "wget", "git clone", "git pull", "git push", "git fetch",
        "npm install", "npm i", "pip install", "pip3 install", "ssh", "scp", "sftp"
    )

    def run_command_sync(
        self,
        command: str,
        timeout: int = 45,
        security_mode: str = "sandboxed",
        allow_network: bool = True
    ) -> Dict[str, Any]:
        """
        Synchronously executes a terminal command safely within the workspace directory,
        strictly enforcing the selected Security Mode and Network policies.
        Uses subprocess.run for rock-solid cross-platform reliability without event-loop limitations.
        """
        cmd_clean = command.strip()

        # 1. Mode: Strict (Command review & read-only enforcement)
        if security_mode == "strict":
            is_strict_safe = any(cmd_clean.lower().startswith(prefix) for prefix in self.STRICT_SAFE_COMMAND_PREFIXES)
            if not is_strict_safe:
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"Execution blocked: Strict mode requires explicit review for modifying command '{command}'.",
                    "passed": False,
                    "os": self.os_type,
                    "security_mode": "strict",
                    "workspace": self.workspace_path
                }

        # 2. Mode: Sandboxed (Default - Destruction Filter + Network Firewall)
        if security_mode in ("sandboxed", "strict"):
            is_safe, reason = CommandSafetyGuard.validate_command(command, self.os_type)
            if not is_safe:
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"Execution blocked: {reason}",
                    "passed": False,
                    "os": self.os_type,
                    "security_mode": security_mode,
                    "workspace": self.workspace_path
                }

            if not allow_network:
                if any(net_cmd in cmd_clean.lower() for net_cmd in self.NETWORK_COMMANDS):
                    return {
                        "exit_code": -1,
                        "stdout": "",
                        "stderr": "Execution blocked: Sandboxed network requests are disabled in settings.",
                        "passed": False,
                        "os": self.os_type,
                        "security_mode": security_mode,
                        "workspace": self.workspace_path
                    }

        # 3. Synchronous Process Invocation via subprocess.run
        try:
            if self.is_windows:
                # Prefer PowerShell on Windows for robust scripting and POSIX alias parity (ls, dir, cat, etc.)
                ps_path = shutil.which("powershell.exe") or "powershell.exe"
                run_args = [ps_path, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd_clean]
                proc = subprocess.run(
                    run_args,
                    cwd=self.workspace_path,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout
                )
            else:
                # Linux & macOS shell runner
                shell_executable = shutil.which("bash") or "/bin/sh"
                proc = subprocess.run(
                    cmd_clean,
                    shell=True,
                    executable=shell_executable,
                    cwd=self.workspace_path,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout
                )

            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode

            return {
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "passed": (exit_code == 0),
                "os": self.os_type,
                "workspace": self.workspace_path
            }

        except subprocess.TimeoutExpired:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout} seconds.",
                "passed": False,
                "os": self.os_type,
                "workspace": self.workspace_path
            }
        except Exception as e:
            err_msg = str(e) or repr(e) or type(e).__name__
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Process execution error: {err_msg}",
                "passed": False,
                "os": self.os_type,
                "workspace": self.workspace_path
            }

    async def run_command(
        self,
        command: str,
        timeout: int = 45,
        security_mode: str = "sandboxed",
        allow_network: bool = True
    ) -> Dict[str, Any]:
        """
        Asynchronously runs command using executor to prevent event loop blocking or Windows asyncio subprocess issues.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.run_command_sync,
            command,
            timeout,
            security_mode,
            allow_network
        )

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
