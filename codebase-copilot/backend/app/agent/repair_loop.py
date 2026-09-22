from typing import Dict, Any, List
from .tools.terminal_agent import TerminalAgent
from .patch_engine import PatchEngine
from ..llm.gemma_provider import OllamaGemmaProvider

MAX_REPAIR_ATTEMPTS = 3

class SelfHealingRepairLoop:
    """
    Automated Error-Repair Loop.
    Executes tests, analyzes tracebacks, proposes surgical patches,
    and iterates autonomously up to 3 bounded attempts.
    """

    def __init__(
        self,
        terminal_agent: TerminalAgent,
        patch_engine: PatchEngine,
        gemma_provider: OllamaGemmaProvider
    ):
        self.terminal = terminal_agent
        self.patcher = patch_engine
        self.gemma = gemma_provider

    async def execute_repair_loop(self, task_description: str) -> Dict[str, Any]:
        attempt = 0
        diagnostics = []

        while attempt < MAX_REPAIR_ATTEMPTS:
            attempt += 1

            # 1. Run test suite
            test_run = await self.terminal.run_tests()
            if test_run["passed"]:
                return {
                    "status": "SUCCESS",
                    "attempts": attempt,
                    "message": f"All tests passed on attempt {attempt}!",
                    "diagnostics": diagnostics
                }

            # 2. Extract error traceback
            error_trace = test_run["stderr"] or test_run["stdout"]
            diagnostics.append({
                "attempt": attempt,
                "error": error_trace[-500:] if error_trace else "Test exit code non-zero"
            })

            # 3. Ask Gemma 4 (31B) to diagnose failure and generate targeted fix
            diagnosis_prompt = (
                f"Task: {task_description}\n\n"
                f"Tests failed on attempt {attempt} with output:\n{error_trace}\n\n"
                "Diagnose the root cause and propose a corrective code modification."
            )

            response_tokens = []
            async for token in self.gemma.stream_reasoning(diagnosis_prompt):
                response_tokens.append(token)

            fix_proposal = "".join(response_tokens)

        return {
            "status": "FAILED",
            "attempts": attempt,
            "message": f"Reached maximum {MAX_REPAIR_ATTEMPTS} repair attempts without passing all tests.",
            "diagnostics": diagnostics
        }
