import asyncio
import os
import platform
import re
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional
try:
    from app.security.guardrails import CommandSafetyGuard
except ModuleNotFoundError:
    try:
        from backend.app.security.guardrails import CommandSafetyGuard
    except ModuleNotFoundError:
        from ...security.guardrails import CommandSafetyGuard

# Active process registry for cancellation and abort support
ACTIVE_TERMINAL_PROCESSES: Dict[str, Dict[str, Any]] = {}

def kill_process_tree(pid: int) -> bool:
    """Kills a process and all of its child processes cleanly across Windows and POSIX."""
    if not pid:
        return False
    try:
        if platform.system().lower() == "windows":
            # Forcefully terminate entire process tree on Windows
            subprocess.run(
                ["taskkill", "/pid", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=5
            )
            return True
        else:
            # POSIX: terminate process group
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                os.kill(pid, signal.SIGKILL)
            return True
    except Exception:
        return False

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

    # Shell chaining and subshell execution characters disallowed in strict mode
    SHELL_CHAINING_PATTERN = re.compile(r"[;&|`\r\n]|\$\(|\$\{")

    # Commands requiring network access
    NETWORK_COMMANDS = (
        "curl", "wget", "git clone", "git pull", "git push", "git fetch",
        "npm install", "npm i", "yarn add", "pip install", "pip3 install",
        "invoke-webrequest", "iwr ", "invoke-restmethod", "irm ",
        "start-bitstransfer", "netcat", "ncat", "socat", "ssh", "scp", "sftp"
    )

    def run_command_sync(
        self,
        command: str,
        timeout: int = 45,
        security_mode: str = "sandboxed",
        allow_network: bool = True,
        execution_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Synchronously executes a terminal command safely within the workspace directory,
        strictly enforcing the selected Security Mode and Network policies.
        Uses subprocess.Popen with process tree tracking for rock-solid cancellation and abort support.
        """
        cmd_clean = command.strip()
        exec_id = execution_id or str(uuid.uuid4())

        # 1. Mode: Strict (Command review & read-only enforcement)
        if security_mode == "strict":
            if self.SHELL_CHAINING_PATTERN.search(cmd_clean):
                return {
                    "execution_id": exec_id,
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": "Execution blocked: Command chaining and subshells (;, &, |, `, $()) are disallowed in Strict mode.",
                    "passed": False,
                    "os": self.os_type,
                    "security_mode": "strict",
                    "workspace": self.workspace_path
                }

            cmd_lower = cmd_clean.lower()
            is_strict_safe = any(cmd_lower == prefix or cmd_lower.startswith(prefix + " ") for prefix in self.STRICT_SAFE_COMMAND_PREFIXES)
            if not is_strict_safe:
                return {
                    "execution_id": exec_id,
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
                    "execution_id": exec_id,
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
                        "execution_id": exec_id,
                        "exit_code": -1,
                        "stdout": "",
                        "stderr": "Execution blocked: Sandboxed network requests are disabled in settings.",
                        "passed": False,
                        "os": self.os_type,
                        "security_mode": security_mode,
                        "workspace": self.workspace_path
                    }

        # 3. Process Invocation via subprocess.Popen with execution tracking
        proc = None
        try:
            creationflags = 0
            if self.is_windows:
                # Creation flag for process group isolation
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
                ps_path = shutil.which("powershell.exe") or "powershell.exe"
                run_args = [ps_path, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd_clean]
                proc = subprocess.Popen(
                    run_args,
                    cwd=self.workspace_path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags
                )
            else:
                shell_executable = shutil.which("bash") or "/bin/sh"
                proc = subprocess.Popen(
                    cmd_clean,
                    shell=True,
                    executable=shell_executable,
                    cwd=self.workspace_path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    start_new_session=True
                )

            ACTIVE_TERMINAL_PROCESSES[exec_id] = {
                "process": proc,
                "pid": proc.pid,
                "command": cmd_clean,
                "started_at": time.time(),
                "workspace": self.workspace_path
            }

            stdout, stderr = proc.communicate(timeout=timeout)
            exit_code = proc.returncode

            return {
                "execution_id": exec_id,
                "exit_code": exit_code,
                "stdout": stdout or "",
                "stderr": stderr or "",
                "passed": (exit_code == 0),
                "os": self.os_type,
                "workspace": self.workspace_path
            }

        except subprocess.TimeoutExpired:
            if proc and proc.pid:
                kill_process_tree(proc.pid)
            return {
                "execution_id": exec_id,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout} seconds.",
                "passed": False,
                "os": self.os_type,
                "workspace": self.workspace_path
            }
        except Exception as e:
            if proc and proc.pid:
                kill_process_tree(proc.pid)
            err_msg = str(e) or repr(e) or type(e).__name__
            return {
                "execution_id": exec_id,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Process execution error: {err_msg}",
                "passed": False,
                "os": self.os_type,
                "workspace": self.workspace_path
            }
        finally:
            ACTIVE_TERMINAL_PROCESSES.pop(exec_id, None)

    async def run_command(
        self,
        command: str,
        timeout: int = 45,
        security_mode: str = "sandboxed",
        allow_network: bool = True,
        execution_id: Optional[str] = None
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
            allow_network,
            execution_id
        )

    @classmethod
    def abort_command(cls, execution_id: Optional[str] = None) -> Dict[str, Any]:
        """Aborts an active terminal execution by killing its process tree."""
        if execution_id:
            info = ACTIVE_TERMINAL_PROCESSES.get(execution_id)
            if info:
                pid = info.get("pid")
                killed = kill_process_tree(pid)
                ACTIVE_TERMINAL_PROCESSES.pop(execution_id, None)
                return {"status": "aborted", "execution_id": execution_id, "pid": pid, "killed": killed}
            return {"status": "not_found", "execution_id": execution_id}
        else:
            killed_count = 0
            for eid, info in list(ACTIVE_TERMINAL_PROCESSES.items()):
                pid = info.get("pid")
                if pid:
                    kill_process_tree(pid)
                    killed_count += 1
            ACTIVE_TERMINAL_PROCESSES.clear()
            return {"status": "all_aborted", "count": killed_count}

    @classmethod
    def get_active_processes(cls) -> List[Dict[str, Any]]:
        """Returns metadata for all currently running terminal commands."""
        now = time.time()
        results = []
        for eid, info in ACTIVE_TERMINAL_PROCESSES.items():
            results.append({
                "execution_id": eid,
                "pid": info.get("pid"),
                "command": info.get("command"),
                "workspace": info.get("workspace"),
                "duration_seconds": round(now - info.get("started_at", now), 2)
            })
        return results

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
