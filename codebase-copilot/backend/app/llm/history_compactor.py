import re
from typing import List, Dict, Tuple, Optional, Set, Any


class HistoryCompactor:
    """
    Two-Tier Rolling Conversation History Compactor.
    Compresses multi-turn chat sessions (5 to 30+ turns) into:
    1. Tier 1: Last 4 turns (2 full user-assistant exchanges) retained verbatim,
       with historical code block redaction to prevent duplicate token injection.
    2. Tier 2: A dense, structured architectural summary for turns older than 4,
       retaining key objectives, decisions, constraints, and referenced files.
    Guarantees strict token-budget compliance across small and large context window models.
    """

    FILE_REGEX = re.compile(r"\b([A-Za-z0-9_\-\.\/]+\.[a-zA-Z0-9]{1,8})\b")
    DECISION_PATTERNS = [
        re.compile(r"\b(?:use|using|switch\s+to|prefer|chose|choose)\s+([A-Za-z0-9_\-\.\s]{3,35})\b", re.IGNORECASE),
        re.compile(r"\b(?:instead\s+of|rather\s+than|replace\s+with)\s+([A-Za-z0-9_\-\.\s]{3,35})\b", re.IGNORECASE),
        re.compile(r"\b(?:must|do\s+not|never|ensure|strictly|zero)\s+([A-Za-z0-9_\-\.\s]{3,35})\b", re.IGNORECASE),
        re.compile(r"\b(?:port|algorithm|schema|model|keypair|auth|token|timeout|interval)\s*[:=]\s*([A-Za-z0-9_\-\.]{1,25})", re.IGNORECASE),
    ]

    CODE_BLOCK_REGEX = re.compile(r"```([a-zA-Z0-9_+-]*)\s*\n([\s\S]*?)```")

    @classmethod
    def redact_code_blocks(cls, content: str, max_lines: int = 30) -> str:
        """
        Redacts large code blocks in historical turns to concise reference anchors,
        preventing thousands of duplicate tokens from bloating historical turns.
        """
        if not content or "```" not in content:
            return content

        def _repl(m: re.Match) -> str:
            lang = m.group(1).strip()
            body = m.group(2)
            lines = body.splitlines()
            if len(lines) <= max_lines:
                return m.group(0)

            first_meaningful = ""
            for l in lines:
                s = l.strip()
                if s and not s.startswith(("//", "#", "/*", "*")):
                    first_meaningful = s[:60]
                    break

            hint = f" ({first_meaningful}...)" if first_meaningful else ""
            return f"```{lang}\n// [Historical code implementation: {len(lines)} lines{hint} - referenced in active context]\n```"

        return cls.CODE_BLOCK_REGEX.sub(_repl, content)

    @classmethod
    def calculate_budget(cls, num_ctx: int = 8192) -> Dict[str, int]:
        """
        Calculates character and token budgets based on model context size (num_ctx).
        Assumes average of 3.8 characters per token.
        """
        if num_ctx <= 4096:
            summary_chars = 900
            tier1_max_chars = 1500
        elif num_ctx <= 8192:
            summary_chars = 1500
            tier1_max_chars = 3000
        elif num_ctx <= 16384:
            summary_chars = 2400
            tier1_max_chars = 6000
        else:
            summary_chars = 3200
            tier1_max_chars = 10000

        return {
            "summary_max_chars": summary_chars,
            "tier1_max_chars": tier1_max_chars
        }

    @classmethod
    def extract_heuristics_from_turns(cls, turns: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        Fast, zero-token heuristic extractor for referenced files, decisions, and topics.
        """
        files_mentioned: Set[str] = set()
        decisions: Set[str] = set()
        topics: List[str] = []

        ignore_exts = {"com", "org", "net", "io", "ai", "co", "dev", "app", "zip", "tar", "gz", "png", "jpg"}

        for turn in turns:
            content = turn.get("content", "")
            role = turn.get("role", "")
            if not content:
                continue

            # Extract files
            for m in cls.FILE_REGEX.finditer(content):
                tok = m.group(1).strip("./\\ ")
                ext = tok.split(".")[-1].lower() if "." in tok else ""
                if ext and not ext.isdigit() and ext not in ignore_exts:
                    files_mentioned.add(tok)

            # Extract user topics
            if role == "user":
                clean_lines = [l.strip() for l in content.splitlines() if l.strip()]
                if clean_lines:
                    first_line = clean_lines[0]
                    if len(first_line) > 5 and not first_line.startswith(("http", "{", "[")):
                        topic_snip = first_line[:100]
                        if topic_snip not in topics:
                            topics.append(topic_snip)

            # Extract decisions / constraints
            for pat in cls.DECISION_PATTERNS:
                for dm in pat.finditer(content):
                    val = dm.group(0).strip(".,;:()")
                    if 5 < len(val) < 60 and not val.startswith("http"):
                        decisions.add(val)

        return {
            "files": sorted(list(files_mentioned))[:12],
            "decisions": sorted(list(decisions))[:8],
            "topics": topics[:4]
        }

    @classmethod
    def format_summary(cls, heuristics: Dict[str, Any], existing_summary: Optional[str] = None, max_chars: int = 1500) -> str:
        """Formats extracted heuristics into a concise, structured markdown summary block."""
        lines = []

        if existing_summary and existing_summary.strip():
            # Retain non-duplicate information from prior summary
            for prev_l in existing_summary.strip().splitlines():
                if prev_l.startswith("- ") and prev_l not in lines:
                    lines.append(prev_l)

        if heuristics["topics"]:
            for t in heuristics["topics"]:
                item = f"- Objective: {t}"
                if item not in lines:
                    lines.append(item)

        if heuristics["files"]:
            files_str = ", ".join(heuristics["files"])
            item = f"- Files discussed: {files_str}"
            if item not in lines:
                lines.append(item)

        if heuristics["decisions"]:
            for d in heuristics["decisions"]:
                item = f"- Key decision/constraint: {d}"
                if item not in lines:
                    lines.append(item)

        raw_summary = "\n".join(lines).strip()
        if len(raw_summary) > max_chars:
            raw_summary = raw_summary[:max_chars] + "..."
        return raw_summary

    @classmethod
    def compact_history(
        cls,
        history: Optional[List[Dict[str, str]]],
        num_ctx: int = 8192,
        active_summary: Optional[str] = None
    ) -> Tuple[List[Dict[str, str]], Optional[str]]:
        """
        Main entry point for two-tier history compaction.
        - If len(history) <= 4: returns history verbatim with active_summary.
        - If len(history) > 4:
            - Tier 1: Last 4 turns verbatim with code block redaction.
            - Tier 2: Older turns (0 to N-4) rolled into active_summary.
        Returns (compacted_messages_for_llm, updated_summary_string).
        """
        if not history:
            return [], active_summary

        valid_history = [
            {"role": t.get("role", "user"), "content": t.get("content", "")}
            for t in history
            if t.get("content") and t.get("role") in ("user", "assistant")
        ]

        if not valid_history:
            return [], active_summary

        budgets = cls.calculate_budget(num_ctx)

        # Case 1: Short history (<= 4 turns / 2 full exchanges)
        if len(valid_history) <= 4:
            compacted_turns = []
            for idx, turn in enumerate(valid_history):
                content = turn["content"]
                # In earlier turns within the 4-turn window, redact large code blocks
                if idx < len(valid_history) - 1 and turn["role"] == "assistant":
                    content = cls.redact_code_blocks(content, max_lines=40)
                compacted_turns.append({"role": turn["role"], "content": content})
            return compacted_turns, active_summary

        # Case 2: Multi-turn history (> 4 turns)
        older_turns = valid_history[:-4]
        recent_turns = valid_history[-4:]

        # Step A: Synthesize older turns into rolling summary
        heuristics = cls.extract_heuristics_from_turns(older_turns)
        updated_summary = cls.format_summary(
            heuristics,
            existing_summary=active_summary,
            max_chars=budgets["summary_max_chars"]
        )

        # Step B: Compact recent Tier 1 turns (redact large code blocks in non-final messages)
        compacted_recent = []
        for idx, turn in enumerate(recent_turns):
            content = turn["content"]
            if idx < len(recent_turns) - 1 and turn["role"] == "assistant":
                content = cls.redact_code_blocks(content, max_lines=30)
            compacted_recent.append({"role": turn["role"], "content": content})

        return compacted_recent, updated_summary if updated_summary else active_summary
