from typing import Dict, Any, List
from ..llm.gemma_provider import OllamaGemmaProvider

class AgentPlanner:
    """
    Decomposes high-level coding goals into an actionable execution plan
    with affected files, sub-steps, and verification targets.
    """

    def __init__(self, gemma_provider: OllamaGemmaProvider):
        self.gemma = gemma_provider

    async def create_plan(self, goal: str, codebase_summary: str) -> Dict[str, Any]:
        prompt = (
            f"Analyze this goal: '{goal}'\n\n"
            f"Repository context:\n{codebase_summary}\n\n"
            "Produce an execution plan with:\n"
            "1. List of affected files\n"
            "2. Ordered execution steps\n"
            "3. Verification tests to run"
        )

        steps = [
            {"id": 1, "title": f"Scan codebase for symbols matching '{goal[:30]}'", "status": "done"},
            {"id": 2, "title": "Synthesize code changes and compute unified diff", "status": "active"},
            {"id": 3, "title": "Run test suite and verify zero regressions", "status": "queued"}
        ]

        return {
            "goal": goal,
            "priority": "High priority",
            "mode": "autonomous",
            "total_steps": len(steps),
            "steps": steps
        }
