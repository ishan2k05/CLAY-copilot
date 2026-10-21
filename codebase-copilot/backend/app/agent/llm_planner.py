import json
import re
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional
try:
    from .instruction_parser import (
        InstructionClassifier,
        SymbolEntityResolver,
        DynamicTaskPlanner,
        TopicDomainResolver,
        normalize_prompt_text
    )
except ImportError:
    from app.agent.instruction_parser import (
        InstructionClassifier,
        SymbolEntityResolver,
        DynamicTaskPlanner,
        TopicDomainResolver,
        normalize_prompt_text
    )

class LLMInstructionDecomposer:
    """
    Two-Tier LLM-Majority Task Decomposer for Autonomous Coding Agent.
    Leverages high-capacity LLM reasoning to decompose natural language developer
    directives into structured, multi-step execution plans. Transparently falls back
    to deterministic heuristic rules if offline or if the LLM response is malformed.
    """

    JSON_CLEAN_RE = re.compile(r",\s*([\]\}])")

    @classmethod
    def _extract_json(cls, raw_text: str) -> Optional[Dict[str, Any]]:
        """Extracts and parses JSON object from model output."""
        if not raw_text or not raw_text.strip():
            return None

        text = raw_text.strip()
        # Strip markdown fences
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if fence_match:
            text = fence_match.group(1).strip()

        # Find outer braces
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            json_str = text[start:end+1]
            # Remove trailing commas before closing braces/brackets
            json_str = cls.JSON_CLEAN_RE.sub(r"\1", json_str)
            try:
                parsed = json.loads(json_str)
                if isinstance(parsed, dict) and "steps" in parsed:
                    return parsed
            except Exception:
                pass

        return None

    @classmethod
    def _build_prompt(
        cls,
        goal: str,
        workspace_dir: str,
        active_file: Optional[str] = None,
        accessible_files: Optional[Dict[str, Path]] = None,
        target_files: Optional[List[str]] = None
    ) -> str:
        """Constructs a strict JSON schema prompt for task decomposition."""
        existing_folders = []
        try:
            ws_p = Path(workspace_dir)
            if ws_p.exists() and ws_p.is_dir():
                existing_folders = [d.name for d in ws_p.iterdir() if d.is_dir() and not d.name.startswith((".", "node_modules", "venv", "__pycache__"))][:10]
        except Exception:
            existing_folders = []

        candidate_files = []
        if accessible_files:
            candidate_files = list(accessible_files.keys())[:15]

        folders_str = ", ".join(existing_folders) if existing_folders else "None (root only)"
        files_str = ", ".join(candidate_files) if candidate_files else "None"
        active_str = active_file or "None"
        targets_str = ", ".join(target_files) if target_files else "None"

        return (
            "You are the Task Planning Intelligence for an autonomous coding agent.\n"
            "Decompose the user's coding directive into a structured, step-by-step JSON execution plan.\n\n"
            f"Workspace Context:\n"
            f"- Existing Folders: {folders_str}\n"
            f"- Active Open File: {active_str}\n"
            f"- Accessible Files: {files_str}\n"
            f"- Target Files in Scope: {targets_str}\n\n"
            f"User Directive:\n\"{goal}\"\n\n"
            "You MUST respond ONLY with a raw JSON object matching this exact schema:\n"
            "{\n"
            "  \"primary_intent\": \"CREATE_FOLDER_AND_FILE\" | \"CREATE_NEW\" | \"EDIT_EXISTING\" | \"CREATE_FOLDER\" | \"MULTI_FILE_REFACTOR\" | \"FIX_TEST\" | \"DELETE_CODE\" | \"ALGORITHM_SYNTHESIS\",\n"
            "  \"topic\": \"Concise topic (e.g. Documentation Scaffolding, A* Pathfinding)\",\n"
            "  \"complexity\": \"normal\" | \"high\" | \"huge\",\n"
            "  \"target_folders\": [\"docs\"],\n"
            "  \"target_files\": [\"docs/script.py\"],\n"
            "  \"is_new_file\": true,\n"
            "  \"change_specs\": [],\n"
            "  \"steps\": [\n"
            "    {\n"
            "      \"id\": 1,\n"
            "      \"action\": \"scan\" | \"prep\" | \"synthesize\" | \"validate\" | \"verify\" | \"delete\",\n"
            "      \"title\": \"1. Descriptive title for step\",\n"
            "      \"progress\": \"Progress log for step...\",\n"
            "      \"detail\": \"Completion detail log for step.\"\n"
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Planning Rules:\n"
            "1. If user asks to create both a folder and a file (e.g. 'create a new folder docs, and make a placeholder file inside it script.py'):\n"
            "   - primary_intent MUST be 'CREATE_FOLDER_AND_FILE'\n"
            "   - target_folders MUST be ['docs']\n"
            "   - target_files MUST be ['docs/script.py']\n"
            "   - Steps MUST be 4 steps: 1. scan, 2. prep (create folder), 3. synthesize (write file code), 4. verify (stage diff in Diff tab)\n"
            "2. If user asks ONLY to create a folder (e.g. 'create folder algorithms'):\n"
            "   - primary_intent MUST be 'CREATE_FOLDER'\n"
            "   - target_folders MUST be ['algorithms']\n"
            "   - target_files MUST be empty []\n"
            "   - Steps MUST be 2 steps: 1. scan, 2. prep (create folder)\n"
            "3. If user asks to create a brand new file (e.g. 'create auth.py'):\n"
            "   - primary_intent MUST be 'CREATE_NEW'\n"
            "   - Steps MUST be 3 steps: 1. scan, 2. synthesize, 3. verify\n"
            "4. If user asks to edit or modify existing file(s):\n"
            "   - primary_intent MUST be 'EDIT_EXISTING'\n"
            "   - If multiple files are involved, include all of them in \"target_files\": [\"calc.py\", \"app.py\"]\n"
            "   - Steps MUST be 3 steps: 1. scan, 2. synthesize, 3. verify\n"
            "5. If user asks to implement algorithms or complex data structures:\n"
            "   - primary_intent MUST be 'ALGORITHM_SYNTHESIS'\n"
            "   - Steps MUST be 5 steps: 1. scan, 2. prep, 3. synthesize, 4. validate, 5. verify\n"
            "6. Output raw JSON only. Zero markdown fences, zero explanations, zero emojis."
        )

    @classmethod
    async def decompose_goal(
        cls,
        goal: str,
        workspace_dir: str,
        active_file: Optional[str] = None,
        accessible_files: Optional[Dict[str, Path]] = None,
        target_files: Optional[List[str]] = None,
        change_specs: Optional[List[Dict[str, Any]]] = None,
        model: str = "gpt-oss:120b-cloud",
        provider: Optional[Any] = None,
        timeout_seconds: float = 6.0
    ) -> Dict[str, Any]:
        """
        Decomposes goal into a structured plan using LLM first, falling back to
        deterministic DynamicTaskPlanner if offline or on parsing error.
        """
        clean_goal = (goal or "").strip()
        if not clean_goal:
            return DynamicTaskPlanner.plan_tasks(
                goal=clean_goal,
                workspace_dir=workspace_dir,
                target_file=active_file,
                target_files=target_files,
                change_specs=change_specs,
                model=model
            )

        # Attempt Tier 1: LLM Reasoning
        if provider and hasattr(provider, "generate_text"):
            try:
                planning_prompt = cls._build_prompt(
                    goal=clean_goal,
                    workspace_dir=workspace_dir,
                    active_file=active_file,
                    accessible_files=accessible_files,
                    target_files=target_files
                )

                raw_output = await asyncio.wait_for(
                    provider.generate_text(prompt=planning_prompt, code_context=None),
                    timeout=timeout_seconds
                )

                parsed_plan = cls._extract_json(raw_output)
                if parsed_plan and "steps" in parsed_plan and len(parsed_plan["steps"]) >= 2:
                    # Sanitize and validate steps
                    steps = []
                    for idx, s in enumerate(parsed_plan["steps"], start=1):
                        steps.append({
                            "id": idx,
                            "action": s.get("action", "synthesize"),
                            "title": s.get("title", f"{idx}. Step {idx}"),
                            "progress": s.get("progress", f"Executing step {idx}..."),
                            "detail": s.get("detail", f"Step {idx} completed.")
                        })

                    resolved_target_files = parsed_plan.get("target_files", []) or []
                    if target_files:
                        for tf in target_files:
                            if tf not in resolved_target_files:
                                resolved_target_files.append(tf)

                    primary_target = resolved_target_files[0] if resolved_target_files else (active_file or "test.py")
                    target_folders = parsed_plan.get("target_folders", [])
                    folder_name = target_folders[0] if target_folders else None

                    intent = parsed_plan.get("primary_intent") or "EDIT_EXISTING"
                    complexity = parsed_plan.get("complexity", "huge" if len(steps) >= 5 else ("high" if len(steps) == 4 else "normal"))

                    return {
                        "source": "llm",
                        "goal": clean_goal,
                        "intent": intent,
                        "topic": parsed_plan.get("topic", "Code Synthesis"),
                        "complexity": complexity,
                        "total_steps": len(steps),
                        "target_file": primary_target,
                        "target_files": resolved_target_files,
                        "folder_name": folder_name,
                        "target_folders": target_folders,
                        "change_specs": parsed_plan.get("change_specs") or change_specs or [],
                        "steps": steps
                    }
            except Exception:
                # LLM timeout or connection error: proceed transparently to deterministic fallback
                pass

        # Tier 2: Deterministic Heuristic Fallback
        fallback_plan = DynamicTaskPlanner.plan_tasks(
            goal=clean_goal,
            workspace_dir=workspace_dir,
            target_file=active_file,
            target_files=target_files,
            change_specs=change_specs,
            model=model
        )
        fallback_plan["source"] = "heuristic_fallback"
        return fallback_plan
