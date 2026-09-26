import re
import ast
import difflib
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set

class InstructionClassifier:
    """
    Two-pass intent classifier for agent coding goals.
    Categorizes tasks into:
    - EDIT_EXISTING: modifying logic/constants/functions in existing files
    - CREATE_NEW: authoring a brand new file/component/class
    - MULTI_FILE_REFACTOR: cross-file modifications or renames
    - FIX_TEST: addressing failing tests or tracebacks
    - EXPLAIN: informational or architectural inquiries without code edits
    """

    PATTERNS: Dict[str, List[re.Pattern]] = {
        "EXPLAIN": [
            re.compile(r"^\s*(?:explain|what\s+is|what\s+does|how\s+does|why\s+is|walk\s*through|describe|show\s+how)\b", re.IGNORECASE),
            re.compile(r"\b(?:help\s+me\s+understand|can\s+you\s+explain)\b", re.IGNORECASE),
        ],
        "FIX_TEST": [
            re.compile(r"\b(?:fix|repair|solve|debug)\s+(?:the\s+)?(?:failing\s+)?tests?\b", re.IGNORECASE),
            re.compile(r"\b(?:pytest|unittest|test\s+failure|assertionerror|traceback|tests\s+are\s+failing)\b", re.IGNORECASE),
            re.compile(r"\b(?:make\s+tests?\s+pass|failing\s+assertions?)\b", re.IGNORECASE),
        ],
        "CREATE_NEW": [
            re.compile(r"\b(?:create|scaffold|add|generate|make|author|bootstrap)\s+(?:a\s+)?(?:new\s+)?(?:file|module|script|component|endpoint|class|interface|helper)\b", re.IGNORECASE),
            re.compile(r"\bcreate\s+[A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+\b", re.IGNORECASE),
            re.compile(r"\bnew\s+file\b", re.IGNORECASE),
        ],
        "MULTI_FILE_REFACTOR": [
            re.compile(r"\b(?:rename|refactor|update)\s+across\s+(?:all|multiple|both)\s+files\b", re.IGNORECASE),
            re.compile(r"\b(?:both|all|and)\s+[A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+\s+and\s+[A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+\b", re.IGNORECASE),
            re.compile(r"\b(?:backend\s+and\s+frontend|client\s+and\s+server|api\s+and\s+client)\b", re.IGNORECASE),
        ],
        "EDIT_EXISTING": [
            re.compile(r"\b(?:update|change|increase|decrease|modify|fix|tweak|replace|adjust|set|add\s+to|remove\s+from|refactor|speed\s*up|slow\s*down|faster|slower|smoother|make\s+.*\s+(?:faster|slower|bigger|smaller))\b", re.IGNORECASE),
            re.compile(r"\b(?:food|score|points|lives|paddle|snake|ball|player|interval|delay|fps|height|width)\b", re.IGNORECASE),
        ]
    }

    @classmethod
    def classify(cls, goal: str) -> Dict[str, Any]:
        goal_strip = goal.strip()
        if not goal_strip:
            return {"intent": "EDIT_EXISTING", "confidence": 0.5, "reason": "empty goal"}

        for intent, patterns in cls.PATTERNS.items():
            for pat in patterns:
                m = pat.search(goal_strip)
                if m:
                    return {
                        "intent": intent,
                        "confidence": 0.92,
                        "matched_pattern": pat.pattern,
                        "reason": f"Matched pattern '{m.group(0)}'"
                    }

        # Default fallback is EDIT_EXISTING for typical developer requests
        return {
            "intent": "EDIT_EXISTING",
            "confidence": 0.70,
            "reason": "Default modification intent"
        }


class SymbolEntityResolver:
    """
    Extracts referenced filenames, symbols (classes, functions, variables),
    and change specifications (e.g. 80 -> 120) from instructions.
    """

    CHANGE_SPEC_PATTERNS = [
        re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:from|was)?\s*([0-9]+(?:\.[0-9]+)?|'[^']+'|\"[^\"]+\")?\s*(?:to|->|=|is\s+now|as)\s*([0-9]+(?:\.[0-9]+)?|'[^']+'|\"[^\"]+\")", re.IGNORECASE),
        re.compile(r"(?:increase|decrease|change|set|update)\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:to|by|=|as)\s+([0-9]+(?:\.[0-9]+)?|'[^']+'|\"[^\"]+\")", re.IGNORECASE),
        re.compile(r"(?:paddle\s*size|paddle\s*height|paddle\s*width|speed|interval|delay|timeout|port|retries|fps|food|lives|score|radius)\s*(?:to|->|=|is|as)?\s*([0-9]+(?:\.[0-9]+)?|'[^']+'|\"[^\"]+\")", re.IGNORECASE),
    ]

    @classmethod
    def extract_change_specs(cls, goal: str) -> List[Dict[str, Any]]:
        specs = []
        for pat in cls.CHANGE_SPEC_PATTERNS:
            for m in pat.finditer(goal):
                groups = [g for g in m.groups() if g is not None]
                if len(groups) == 3:
                    specs.append({"target": groups[0], "from": groups[1], "to": groups[2], "raw": m.group(0)})
                elif len(groups) == 2:
                    specs.append({"target": groups[0], "to": groups[1], "raw": m.group(0)})
                elif len(groups) == 1:
                    specs.append({"target": "parameter", "to": groups[0], "raw": m.group(0)})
        return specs

    @classmethod
    def resolve_target_files(
        cls,
        goal: str,
        workspace_dir: str,
        accessible_files: Optional[Dict[str, Path]] = None
    ) -> List[str]:
        """
        Resolves mentioned filenames against workspace files using exact,
        basename, and fuzzy sequence matching.
        """
        if accessible_files is None:
            accessible_files = {}
            wpath = Path(workspace_dir)
            if wpath.exists():
                for p in wpath.rglob("*"):
                    if p.is_file() and not any(part.startswith(".") or part in ("node_modules", "venv", "__pycache__", "dist", "out") for part in p.parts):
                        rel = str(p.relative_to(wpath))
                        accessible_files[rel] = p

        matched_files: List[Tuple[float, str]] = []

        # 1. Exact token matches with extensions (e.g. game.py, main.py, styles.css)
        file_token_re = re.compile(r"\b([A-Za-z0-9_\-\.\/]+\.[a-zA-Z0-9]{1,6})\b")
        for match in file_token_re.finditer(goal):
            token = match.group(1).lstrip("./")
            token_name = Path(token).name.lower()
            for rel, path_obj in accessible_files.items():
                if rel.lower() == token.lower() or path_obj.name.lower() == token_name:
                    matched_files.append((1.0, rel))

        if matched_files:
            # Sort by score descending and return unique files
            seen = set()
            res = []
            for score, f in sorted(matched_files, key=lambda x: -x[0]):
                if f not in seen:
                    seen.add(f)
                    res.append(f)
            return res

        # 2. Match stem or keywords against known file stems (e.g. "game" -> "game.py" or "pingpong" -> "pingpong.py")
        words = [w.strip(".,;:?!'\"()[]{}<>`").lower() for w in goal.split()]
        for w in words:
            if len(w) < 3 or w in ("the", "and", "for", "with", "into", "file", "code", "size", "make"):
                continue
            for rel, path_obj in accessible_files.items():
                stem = path_obj.stem.lower()
                name = path_obj.name.lower()
                if w == stem or w == name:
                    matched_files.append((0.9, rel))
                elif w in stem:
                    ratio = difflib.SequenceMatcher(None, w, stem).ratio()
                    if ratio > 0.65:
                        matched_files.append((0.7 + ratio * 0.2, rel))

        # 3. Content-aware keyword fallback: If no direct filename matched, scan files for distinctive keywords
        if not matched_files:
            goal_keywords = [w for w in words if len(w) >= 4 and w not in ("increase", "decrease", "update", "change", "modify", "should", "could", "would")]
            if goal_keywords:
                for rel, path_obj in accessible_files.items():
                    if path_obj.suffix.lower() in (".py", ".js", ".ts", ".html", ".css", ".json"):
                        try:
                            sample = path_obj.read_text(encoding="utf-8", errors="ignore")[:8000].lower()
                            hits = sum(1 for kw in goal_keywords if kw in sample)
                            if hits > 0:
                                matched_files.append((0.65 + min(0.3, hits * 0.1), rel))
                        except Exception:
                            pass

        seen = set()
        res = []
        for score, f in sorted(matched_files, key=lambda x: -x[0]):
            if f not in seen:
                seen.add(f)
                res.append(f)
        return res

    @classmethod
    def resolve_target_symbols(cls, goal: str, file_content: str) -> List[str]:
        """
        Scans file content for function, class, and variable definitions,
        and checks if any are referenced in the user's goal.
        """
        symbol_names: Set[str] = set()

        # Try AST for Python
        try:
            tree = ast.parse(file_content)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    symbol_names.add(node.name)
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            symbol_names.add(target.id)
        except Exception:
            # Fallback regex for non-Python or syntax-partial files
            for m in re.finditer(r"\b(?:def|class|function|var|let|const|func|pub\s+fn)\s+([A-Za-z_][A-Za-z0-9_]*)", file_content):
                symbol_names.add(m.group(1))
            for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*=", file_content):
                symbol_names.add(m.group(1))

        matched_symbols = []
        goal_lower = goal.lower()
        for sym in symbol_names:
            sym_clean = sym.lower().replace("_", " ")
            if sym.lower() in goal_lower:
                matched_symbols.append(sym)
            elif sym_clean in goal_lower:
                matched_symbols.append(sym)
            else:
                # Check for snake_case/camelCase sub-parts (e.g. "paddle" in "paddle_height")
                parts = [p.lower() for p in re.split(r"[_]+|(?<=[a-z])(?=[A-Z])", sym) if len(p) >= 3]
                if parts and all(p in goal_lower for p in parts if p not in ("get", "set", "update", "is")):
                    matched_symbols.append(sym)

        return matched_symbols

    @classmethod
    def resolve_target_symbols_with_positions(cls, goal: str, file_content: str) -> List[Dict[str, Any]]:
        """
        Scans file content for function, class, and variable definitions,
        and returns list of dicts: {"name": str, "lineno": int, "end_lineno": int}
        for all symbols referenced in the goal.
        """
        symbols: List[Dict[str, Any]] = []
        try:
            tree = ast.parse(file_content)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    symbols.append({
                        "name": node.name,
                        "lineno": node.lineno,
                        "end_lineno": getattr(node, "end_lineno", node.lineno)
                    })
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            symbols.append({
                                "name": target.id,
                                "lineno": node.lineno,
                                "end_lineno": getattr(node, "end_lineno", node.lineno)
                            })
        except Exception:
            for idx, line in enumerate(file_content.splitlines()):
                m = re.search(r"\b(?:def|class|function|var|let|const|func|pub\s+fn)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
                if m:
                    symbols.append({"name": m.group(1), "lineno": idx + 1, "end_lineno": idx + 1})
                m2 = re.search(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
                if m2:
                    symbols.append({"name": m2.group(1), "lineno": idx + 1, "end_lineno": idx + 1})

        matched = []
        goal_lower = goal.lower()
        for s in symbols:
            name = s["name"]
            sym_clean = name.lower().replace("_", " ")
            is_match = False
            if name.lower() in goal_lower or sym_clean in goal_lower:
                is_match = True
            else:
                parts = [p.lower() for p in re.split(r"[_]+|(?<=[a-z])(?=[A-Z])", name) if len(p) >= 3]
                if parts and all(p in goal_lower for p in parts if p not in ("get", "set", "update", "is")):
                    is_match = True

            if is_match:
                matched.append(s)

        return matched


class AmbiguityDetector:
    """
    Detects if an instruction is ambiguous and presents candidate files
    as interactive clarification chips instead of blindly guessing.
    """

    @classmethod
    def check_ambiguity(
        cls,
        goal: str,
        candidate_files: List[str]
    ) -> Optional[Dict[str, Any]]:
        """
        Returns ambiguity details if 2+ candidates match with similar relevance
        and the goal doesn't explicitly name a single file.
        """
        if len(candidate_files) <= 1:
            return None

        # If goal contains an exact filename with extension, it's NOT ambiguous
        for f in candidate_files:
            fname = Path(f).name
            if fname.lower() in goal.lower():
                return None

        # If candidates share the same extension or similar purpose (e.g. game.py vs game_multiplayer.py)
        clean_candidates = [Path(f).name for f in candidate_files[:4]]
        return {
            "status": "ambiguous",
            "message": f"Multiple potential files match your request '{goal[:40]}...'",
            "question": "Which file would you like to update?",
            "candidates": clean_candidates,
            "full_paths": candidate_files[:4]
        }


class ContextWindowBuilder:
    """
    Builds an AST-aware 'Skeleton + Target' context window.
    For small to medium files (<= 600 lines), preserves the FULL verbatim content
    with exact 1-indexed line numbers to prevent any hallucination.
    For massive files (> 600 lines), preserves:
    1. Module imports and top-level constants
    2. Structural skeleton of non-target functions & classes with docstrings
    3. Verbatim body and generous windows around the targeted function/class/variable/cursor
    """

    @classmethod
    def build_ast_skeleton_context(
        cls,
        file_content: str,
        filename: str,
        target_symbols: Optional[List[str]] = None,
        max_lines: int = 600,
        preferred_line: Optional[int] = None
    ) -> Tuple[str, List[int]]:
        """
        Returns (formatted_context_string, included_line_numbers).
        Guarantees that files of any size fit tightly within LLM context
        while highlighting the exact symbol to edit with original line numbers.
        """
        if not file_content:
            return "", []

        lines = file_content.splitlines()
        total_lines = len(lines)

        # For small files (<= 120 lines), or if no specific target symbols were identified in files <= max_lines,
        # return the entire file verbatim with real 1-indexed line numbers to prevent hallucination.
        if total_lines <= 120 or (not target_symbols and total_lines <= max_lines):
            numbered = [f"{i+1:3d}: {line}" for i, line in enumerate(lines)]
            return (
                f"\n--- Verbatim content of '{filename}' with EXACT 1-Indexed Line Numbers (all {total_lines} lines) ---\n"
                f"[IMPORTANT: Use the exact line numbers shown before the colons. Do NOT make up line numbers.]\n"
                + "\n".join(numbered) + "\n",
                list(range(1, total_lines + 1))
            )

        target_symbols_set = set(target_symbols or [])

        # Python-specific AST extraction for large files
        if filename.endswith(".py"):
            try:
                return cls._build_python_ast_skeleton(lines, target_symbols_set, filename, preferred_line=preferred_line)
            except Exception:
                pass

        # Universal fallback for non-Python or unparseable large files
        return cls._build_generic_skeleton(lines, target_symbols_set, filename, preferred_line=preferred_line)

    @classmethod
    def _build_python_ast_skeleton(
        cls,
        lines: List[str],
        target_symbols: Set[str],
        filename: str,
        preferred_line: Optional[int] = None
    ) -> Tuple[str, List[int]]:
        tree = ast.parse("\n".join(lines))
        keep_lines: Set[int] = set()

        # 1. Always keep imports and module docstring
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for l in range(node.lineno, getattr(node, "end_lineno", node.lineno) + 1):
                    keep_lines.add(l)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) and node.lineno <= 5:
                # Module docstring
                for l in range(node.lineno, getattr(node, "end_lineno", node.lineno) + 1):
                    keep_lines.add(l)
            elif isinstance(node, ast.Assign):
                # Keep top-level constants / config variables
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if any(n.isupper() or n in target_symbols for n in names):
                    for l in range(node.lineno, getattr(node, "end_lineno", node.lineno) + 1):
                        keep_lines.add(l)

        # 2. Check functions and classes
        matched_target_node = None
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                is_target = node.name in target_symbols
                if is_target:
                    matched_target_node = node
                    # Keep full verbatim body of target node plus generous surrounding context
                    start_l = max(1, node.lineno - 20)
                    end_line = min(len(lines), getattr(node, "end_lineno", len(lines)) + 20)
                    for l in range(start_l, end_line + 1):
                        keep_lines.add(l)
                else:
                    # Keep signature and docstring only
                    keep_lines.add(node.lineno)
                    if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                        keep_lines.add(node.body[0].lineno)

        # 3. If preferred_line (cursor or vicinity) is provided, include 40 lines around it
        if preferred_line is not None and 1 <= preferred_line <= len(lines):
            for l in range(max(1, preferred_line - 30), min(len(lines) + 1, preferred_line + 30)):
                keep_lines.add(l)

        # 4. If no target symbol matched, keep first 120 lines + last 60 lines
        if not matched_target_node and not any(l > 120 for l in keep_lines):
            for l in range(1, min(140, len(lines) + 1)):
                keep_lines.add(l)
            for l in range(max(1, len(lines) - 60), len(lines) + 1):
                keep_lines.add(l)

        # Build output with line number annotations and '... [N lines collapsed] ...' markers
        sorted_lines = sorted(list(keep_lines))
        out_chunks = []
        last_l = 0

        out_chunks.append(f"\n--- AST Outline & Target Context for '{filename}' with REAL Line Numbers ({len(lines)} total lines) ---")
        out_chunks.append("[IMPORTANT: Use the exact line numbers shown before the colons. Do NOT make up line numbers.]")
        for l in sorted_lines:
            if l <= 0 or l > len(lines):
                continue
            if last_l > 0 and l > last_l + 1:
                omitted = l - last_l - 1
                out_chunks.append(f"     ... [{omitted} lines collapsed for brevity] ...")
            out_chunks.append(f"{l:3d}: {lines[l-1]}")
            last_l = l

        if last_l < len(lines):
            out_chunks.append(f"     ... [{len(lines) - last_l} lines collapsed] ...")

        return "\n".join(out_chunks) + "\n", sorted_lines

    @classmethod
    def _build_generic_skeleton(
        cls,
        lines: List[str],
        target_symbols: Set[str],
        filename: str,
        preferred_line: Optional[int] = None
    ) -> Tuple[str, List[int]]:
        keep_lines: Set[int] = set()

        # Keep first 50 lines (imports, headers, configs)
        for i in range(1, min(51, len(lines) + 1)):
            keep_lines.add(i)

        # Search for lines containing target symbols
        for i, line in enumerate(lines):
            line_no = i + 1
            for sym in target_symbols:
                if sym and sym in line:
                    # Add generous window around symbol
                    for win in range(max(1, line_no - 15), min(len(lines) + 1, line_no + 35)):
                        keep_lines.add(win)

        if preferred_line is not None and 1 <= preferred_line <= len(lines):
            for l in range(max(1, preferred_line - 25), min(len(lines) + 1, preferred_line + 25)):
                keep_lines.add(l)

        sorted_lines = sorted(list(keep_lines))
        out_chunks = [
            f"\n--- Targeted Context for '{filename}' with REAL Line Numbers ({len(lines)} total lines) ---",
            "[IMPORTANT: Use the exact line numbers shown before the colons. Do NOT make up line numbers.]"
        ]
        last_l = 0
        for l in sorted_lines:
            if l <= 0 or l > len(lines):
                continue
            if last_l > 0 and l > last_l + 1:
                out_chunks.append(f"     ... [{l - last_l - 1} lines collapsed] ...")
            out_chunks.append(f"{l:3d}: {lines[l-1]}")
            last_l = l

        if last_l < len(lines):
            out_chunks.append(f"     ... [{len(lines) - last_l} lines collapsed] ...")

        return "\n".join(out_chunks) + "\n", sorted_lines
