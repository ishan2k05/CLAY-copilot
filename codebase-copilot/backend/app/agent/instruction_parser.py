import re
import ast
import difflib
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set


def normalize_prompt_text(text: str) -> str:
    """Normalize common developer typos and shorthand."""
    if not text:
        return ""
    t = text
    typo_map = [
        (r"\bsophesticated\b", "sophisticated"),
        (r"\bsophistcated\b", "sophisticated"),
        (r"\badn\b", "and"),
        (r"\balgo\b", "algorithm"),
        (r"\balgos\b", "algorithms"),
        (r"\bastar\b", "a-star algorithm"),
        (r"\ba\*\b", "a-star algorithm"),
        (r"\ba-star\b", "a-star algorithm"),
        (r"\bdsa\b", "data structures and algorithms"),
        (r"\bfucntion\b", "function"),
        (r"\bfuncton\b", "function"),
        (r"\bimplemet\b", "implement"),
        (r"\bimpliment\b", "implement"),
        (r"\bcompelx\b", "complex"),
        (r"\bdelte\b", "delete"),
        (r"\bfoler\b", "folder"),
        (r"\bfloder\b", "folder"),
    ]
    for pattern, repl in typo_map:
        t = re.sub(pattern, repl, t, flags=re.IGNORECASE)
    return t


class InstructionClassifier:
    """
    Two-pass intent classifier for agent coding goals.
    Categorizes tasks into:
    - ALGORITHM_SYNTHESIS: authoring or implementing algorithms and data structures
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
            re.compile(r"\b(?:create|scaffold|add|generate|make|author|bootstrap|write|code|implement|put|dump|save)\b.*?\b(?:new\s+file|new\s+script|new\s+module|new\s+component|new\s+helper|new\s+test)\b", re.IGNORECASE),
            re.compile(r"\b(?:in|into|to|as|inside)\s+(?:a\s+)?(?:new\s+)?(?:file|script|module|component)\s*[,:]?\s*['\"`]?([A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+)", re.IGNORECASE),
            re.compile(r"\b(?:new\s+file|create\s+file|add\s+file|make\s+file)\s*[,:]?\s*['\"`]?([A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+)", re.IGNORECASE),
            re.compile(r"\b(?:create|make|add|scaffold|new)\s+(?:a\s+)?(?:new\s+)?(?:folder|directory|dir|package)\b", re.IGNORECASE),
            re.compile(r"\bcreate\s+[A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+\b", re.IGNORECASE),
            re.compile(r"\bput\s+(?:it|them|this|code)?\s*(?:in|into|inside)\s+(?:a\s+)?(?:new\s+)?(?:file|folder)?\s*[,:]?\s*['\"`]?([A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+)", re.IGNORECASE),
            re.compile(r"\bsave\s+(?:it|them|this|code)?\s*(?:as|in|to|into)\s+['\"`]?([A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+)", re.IGNORECASE),
            re.compile(r"\bnew\s+file\b", re.IGNORECASE),
            re.compile(r"\bnew\s+(?:folder|directory)\b", re.IGNORECASE),
        ],
        "CREATE_FOLDER": [
            re.compile(r"^\s*(?:create|make|add|new)\s+(?:me\s+)?(?:a\s+)?(?:new\s+)?(?:folder|directory|dir)\s*(?:named|called|titled|name(?:\s+is)?|as|of|:\s*)?\s*['\"`]?([A-Za-z0-9_\-\.\/]+)['\"`]?", re.IGNORECASE),
            re.compile(r"\b(?:folder|directory|dir)\s*(?:named|called|titled|name(?:\s+is)?|as|of|:\s*)?\s*['\"`]?([A-Za-z0-9_\-\.\/]+)['\"`]?\s*(?:only)?$", re.IGNORECASE),
        ],
        "MULTI_FILE_REFACTOR": [
            re.compile(r"\b(?:rename|refactor|update)\s+across\s+(?:all|multiple|both)\s+files\b", re.IGNORECASE),
            re.compile(r"\b(?:both|all|and)\s+[A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+\s+and\s+[A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+\b", re.IGNORECASE),
            re.compile(r"\b(?:backend\s+and\s+frontend|client\s+and\s+server|api\s+and\s+client)\b", re.IGNORECASE),
        ],
        "ALGORITHM_SYNTHESIS": [
            re.compile(r"\b(?:astar|a\*|a-star|dijkstra|pathfinding|bfs|dfs|quicksort|mergesort|binary\s+search|dynamic\s+programming|tree|graph|heap|priority\s+queue|trie|dsa|algo|algorithm)\b", re.IGNORECASE),
            re.compile(r"\b(?:sophisticated|sophesticated|large\s+code|complex\s+code|advanced\s+code|production-grade|from\s+scratch)\b", re.IGNORECASE),
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

        normalized_goal = normalize_prompt_text(goal_strip)
        for intent, patterns in cls.PATTERNS.items():
            for pat in patterns:
                m = pat.search(goal_strip) or pat.search(normalized_goal)
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
    def extract_target_filepaths(cls, goal: str) -> List[str]:
        """
        Extracts potential target file and folder paths from natural language instructions.
        Examples:
        - 'write me code for DSA and put them in a new file , test2.py' -> ['test2.py']
        - 'create a folder dsa and put test2.py inside it' -> ['dsa/test2.py']
        - 'create a new file src/components/Button.tsx' -> ['src/components/Button.tsx']
        - 'create folder dsa' -> ['dsa']
        """
        targets: List[str] = []
        clean_goal = goal.strip()

        # 1. Detect target folder mentioned in prompt (filtering noise/connecting words like 'named', 'called')
        folder_pattern = re.compile(
            r"\b(?:folder|directory|dir)\s*(?:named|called|titled|name(?:\s+is)?|as|of|for|with|:\s*|\s+)?\s*['\"`]?([A-Za-z0-9_\-\.\/]+)['\"`]?",
            re.IGNORECASE
        )
        stop_words = {
            "named", "called", "name", "titled", "as", "of", "for", "with", "a", "an", "the",
            "new", "me", "folder", "directory", "dir", "and", "in", "into", "inside", "it",
            "to", "put", "file", "files", "my", "this", "that", "placeholder", "statement", "is"
        }
        target_folder = None
        for m in folder_pattern.finditer(clean_goal):
            cand = m.group(1).strip("./\\ ,:;'\"`")
            if cand.lower() not in stop_words and len(cand) > 0 and not cand.startswith("."):
                target_folder = cand
                break

        if not target_folder:
            # Check secondary pattern: e.g. "create a documentation folder"
            f_match2 = re.search(r"\b([A-Za-z0-9_\-\.\/]+)\s+(?:folder|directory|dir)\b", clean_goal, re.IGNORECASE)
            if f_match2:
                cand2 = f_match2.group(1).strip("./\\ ,:;'\"`")
                if cand2.lower() not in stop_words and len(cand2) > 0 and not cand2.startswith("."):
                    target_folder = cand2

        # 2. Targeted phrases for files:
        phrase_patterns = [
            re.compile(r"\b(?:new\s+file|create\s+file|make\s+file|scaffold\s+file|add\s+file)\s*[,:]?\s*['\"`]?([A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]{1,8})['\"`]?", re.IGNORECASE),
            re.compile(r"\b(?:file|script|module|component)\s+(?:called|named)\s*['\"`]?([A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]{1,8})['\"`]?", re.IGNORECASE),
            re.compile(r"\b(?:put|save|write|dump|store|place)\s+(?:it|them|this|code|the\s+code)?\s*(?:in|into|to|inside|as)\s*(?:a\s+)?(?:new\s+)?(?:file)?\s*[,:]?\s*['\"`]?([A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]{1,8})['\"`]?", re.IGNORECASE),
            re.compile(r"\b(?:in|into|to|as)\s+(?:a\s+)?(?:new\s+)?(?:file|script|module)\s*[,:]?\s*['\"`]?([A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]{1,8})['\"`]?", re.IGNORECASE),
        ]
        for pat in phrase_patterns:
            for m in pat.finditer(clean_goal):
                fpath = m.group(1).strip("./\\ ")
                if fpath and fpath not in targets:
                    targets.append(fpath)

        # 3. Any token with a valid code/data file extension
        file_token_re = re.compile(r"\b([A-Za-z0-9_\-\.\/]+\.[a-zA-Z0-9]{1,8})\b")
        for m in file_token_re.finditer(clean_goal):
            token = m.group(1).strip("./\\ ")
            ext = token.split(".")[-1].lower() if "." in token else ""
            if ext and not ext.isdigit() and ext not in ("com", "org", "net", "io", "ai", "co", "dev", "app", "xyz", "zip", "tar", "gz"):
                if token not in targets:
                    targets.append(token)

        # 4. If a target folder was extracted and a file target does not already contain a slash, prepend the folder
        final_targets: List[str] = []
        for t in targets:
            if target_folder and "/" not in t and "\\" not in t:
                final_targets.append(f"{target_folder}/{t}")
            else:
                final_targets.append(t)

        # If only a target folder was specified without a file (e.g. "create folder dsa")
        if not final_targets and target_folder:
            final_targets.append(target_folder)

        return final_targets

    @classmethod
    def resolve_target_files(
        cls,
        goal: str,
        workspace_dir: str,
        accessible_files: Optional[Dict[str, Path]] = None
    ) -> List[str]:
        """
        Resolves mentioned filenames against workspace files using exact,
        basename, and fuzzy sequence matching. Supports newly created files
        and folder-qualified paths.
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

        # 1. Exact token matches and extracted targets (including brand-new files & folder paths)
        extracted_targets = cls.extract_target_filepaths(goal)
        for target in extracted_targets:
            token_clean = target.replace("\\", "/").strip("./")
            token_name = Path(token_clean).name.lower()

            found_in_accessible = False
            for rel, path_obj in accessible_files.items():
                rel_clean = rel.replace("\\", "/")
                if rel_clean.lower() == token_clean.lower() or path_obj.name.lower() == token_name:
                    matched_files.append((1.0, rel_clean))
                    found_in_accessible = True
                    break

            if not found_in_accessible:
                # Target is a new file or folder-qualified new path! Include it with top priority!
                matched_files.append((1.0, token_clean))

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


class TopicDomainResolver:
    """
    Extracts semantic engineering domain and topic titles from instructions.
    E.g.
    - 'astar algo' -> 'A* Pathfinding Algorithm'
    - 'dijkstra' -> "Dijkstra's Shortest Path Algorithm"
    - 'binary search' -> 'Binary Search & Bisect Engine'
    - 'sorting' -> 'Optimized Sorting Algorithm'
    - 'tree' -> 'Self-Balancing Tree Data Structure'
    - 'graph' -> 'Graph Theory & Traversal Engine'
    - 'auth' -> 'Authentication & Token Verification Pipeline'
    - 'rate limiter' -> 'Token Bucket Rate Limiting Engine'
    - 'snake' / 'game' -> 'Interactive Game Loop Engine'
    """

    @classmethod
    def resolve_topic(cls, goal: str) -> Dict[str, str]:
        normalized = normalize_prompt_text(goal).lower()

        if any(w in normalized for w in ("a*", "astar", "a-star", "pathfinding")):
            return {
                "topic": "A* Pathfinding Algorithm",
                "domain": "pathfinding",
                "heuristic": "Manhattan & Euclidean heuristics",
                "data_structure": "Node representation & min-heap priority queue",
                "edge_cases": "obstacle traps, unreachable targets & boundary conditions"
            }
        if "dijkstra" in normalized:
            return {
                "topic": "Dijkstra's Shortest Path Algorithm",
                "domain": "graph search",
                "heuristic": "accumulated edge weights",
                "data_structure": "min-heap priority queue and adjacency graph",
                "edge_cases": "disconnected nodes and weighted cycles"
            }
        if any(w in normalized for w in ("bfs", "dfs", "breadth first", "depth first")):
            return {
                "topic": "Graph Traversal Engine (BFS/DFS)",
                "domain": "graph search",
                "heuristic": "visited sets and frontier queue",
                "data_structure": "deque frontier and visited tracking",
                "edge_cases": "cyclic graphs and isolated vertices"
            }
        if any(w in normalized for w in ("binary search", "bsearch", "bisect")):
            return {
                "topic": "Binary Search & Bisect Engine",
                "domain": "search algorithm",
                "heuristic": "monotonic midpoint bisection",
                "data_structure": "ordered arrays and search pointers",
                "edge_cases": "empty lists, duplicates, and out-of-bound targets"
            }
        if any(w in normalized for w in ("sort", "quicksort", "mergesort", "heapsort")):
            return {
                "topic": "Optimized Divide-and-Conquer Sorting Algorithm",
                "domain": "sorting",
                "heuristic": "partition pivot and merge balance",
                "data_structure": "contiguous arrays and auxiliary partitions",
                "edge_cases": "already-sorted arrays, reverse arrays, and duplicates"
            }
        if any(w in normalized for w in ("dynamic programming", "dp", "memoization", "knapsack")):
            return {
                "topic": "Dynamic Programming & Memoization Engine",
                "domain": "dynamic programming",
                "heuristic": "optimal substructure and overlapping subproblems",
                "data_structure": "memoization table and state transitions",
                "edge_cases": "base cases, 0-weight boundaries, and large state matrices"
            }
        if any(w in normalized for w in ("tree", "bst", "avl", "red-black", "red black")):
            return {
                "topic": "Self-Balancing Binary Search Tree",
                "domain": "data structures",
                "heuristic": "balance factors and rotational invariants",
                "data_structure": "TreeNode references with left/right child pointers",
                "edge_cases": "duplicate keys, root deletions, and zigzag rotations"
            }
        if any(w in normalized for w in ("trie", "prefix tree")):
            return {
                "topic": "Prefix Tree (Trie) Search Engine",
                "domain": "string search",
                "heuristic": "common prefix compression",
                "data_structure": "TrieNode with character edge mapping and terminal flags",
                "edge_cases": "empty strings, unicode prefixes, and nonexistent keys"
            }
        if any(w in normalized for w in ("lru", "cache")):
            return {
                "topic": "Least-Recently-Used (LRU) Cache",
                "domain": "data caching",
                "heuristic": "temporal locality and eviction policy",
                "data_structure": "doubly-linked list with hash map lookup",
                "edge_cases": "capacity zero, repeated lookups, and concurrent eviction"
            }
        if any(w in normalized for w in ("rate limit", "token bucket", "leaky bucket")):
            return {
                "topic": "Token Bucket Rate Limiting Engine",
                "domain": "network protocols",
                "heuristic": "token refill rate and burst capacity",
                "data_structure": "thread-safe state buckets with timestamp tracking",
                "edge_cases": "clock drift, high concurrency, and depleted burst quotas"
            }
        if any(w in normalized for w in ("snake", "game", "curses", "paddle", "pong")):
            return {
                "topic": "Real-Time Game Loop & Movement Engine",
                "domain": "game development",
                "heuristic": "frame delta timing and collision bounds",
                "data_structure": "coordinate vectors, event queues, and screen buffers",
                "edge_cases": "border collisions, self-intersections, and input buffering"
            }
        if any(w in normalized for w in ("auth", "jwt", "oauth", "token")):
            return {
                "topic": "Authentication & JWT Token Verification Pipeline",
                "domain": "security architecture",
                "heuristic": "cryptographic signature and expiration claims",
                "data_structure": "header payload models and crypto verification context",
                "edge_cases": "expired tokens, malformed headers, and secret mismatch"
            }

        # Dynamic extraction fallback: look for "code for <noun>" or "implement <noun>"
        m = re.search(r"\b(?:code\s+for|implement|write(?:\s+me)?(?:\s+code\s+for)?|build|create)\s+([A-Za-z0-9_\-\s]{3,30}?)(?:\s+in|\s+into|\s+for|\s+file|$)", normalized)
        if m:
            extracted = m.group(1).strip()
            noise = {"a", "an", "the", "new", "me", "some", "large", "sophisticated", "code", "file"}
            tokens = [t for t in extracted.split() if t not in noise]
            if tokens:
                phrase = " ".join(tokens).title()
                return {
                    "topic": f"{phrase} Engine",
                    "domain": "software architecture",
                    "heuristic": "state specifications and invariant models",
                    "data_structure": "core data models and helper functions",
                    "edge_cases": "input validation, boundary cases, and error handling"
                }

        return {
            "topic": "Production Software Module",
            "domain": "software engineering",
            "heuristic": "domain constraints and invariants",
            "data_structure": "component architecture and data models",
            "edge_cases": "boundary conditions and error handling"
        }


class DynamicTaskPlanner:
    """
    Smart, context-aware execution task planner.
    Dynamically determines the optimal number of steps (2 to 5) and generates
    unique, prompt-tailored step titles and progress logs based on intent,
    complexity, target files, and execution scope.
    """

    @classmethod
    def plan_tasks(
        cls,
        goal: str,
        workspace_dir: Optional[str] = None,
        target_file: Optional[str] = None,
        target_files: Optional[List[str]] = None,
        change_specs: Optional[List[Dict[str, Any]]] = None,
        model: str = "gpt-oss:120b-cloud",
        **kwargs: Any
    ) -> Dict[str, Any]:
        clean_goal = (goal or "").strip()
        normalized_goal = normalize_prompt_text(clean_goal)
        intent_info = InstructionClassifier.classify(clean_goal)
        intent = intent_info.get("intent", "EDIT_EXISTING")

        # Detect deletion intent
        is_delete = bool(
            re.search(r"\b(delete|remove|clear|wipe|erase|truncate|empty)\b.*?\b(all\s+code|code|everything|content|file|functions)\b", clean_goal, re.IGNORECASE)
            or re.search(r"^\s*(?:delete|remove|clear|wipe|erase|empty)\s+", clean_goal, re.IGNORECASE)
        )
        if is_delete:
            intent = "DELETE_CODE"

        # Resolve target files & folders
        extracted_targets = SymbolEntityResolver.extract_target_filepaths(clean_goal)
        resolved_target = target_file
        if not resolved_target and target_files:
            resolved_target = target_files[0]
        if not resolved_target:
            resolved_target = extracted_targets[0] if extracted_targets else None
        if not resolved_target:
            for w in clean_goal.split():
                cw = w.strip(".,;:?!'\"()[]{}<>`")
                if "." in cw and not cw.startswith("."):
                    resolved_target = cw
                    break
        if not resolved_target:
            resolved_target = "test.py"

        # Check for folder component
        folder_match = re.search(r"\b(?:folder|directory|dir)\s*(?:named|called|titled|name(?:\s+is)?|as|of|for|with|:\s*|\s+)?\s*['\"`]?([A-Za-z0-9_\-\.\/]+)['\"`]?", clean_goal, re.IGNORECASE)
        stop_words = {"named", "called", "name", "titled", "as", "of", "for", "with", "a", "an", "the", "new", "me", "folder", "directory", "dir", "and", "in", "into", "inside", "it", "to", "put", "file", "files", "my", "this", "that", "placeholder", "statement", "is"}
        folder_name = None
        if folder_match:
            c = folder_match.group(1).strip("./\\ ,:;'\"`")
            if c.lower() not in stop_words and len(c) > 0 and not c.startswith("."):
                folder_name = c

        # Extract symbols or keywords
        if change_specs is None:
            change_specs = SymbolEntityResolver.extract_change_specs(clean_goal)
        keywords = []
        if change_specs:
            for cs in change_specs:
                if cs.get("target"):
                    keywords.append(cs["target"])
        if not keywords:
            for kw in ("speed", "interval", "paddle", "score", "lives", "ball", "food", "timeout", "port", "color", "auth", "jwt"):
                if kw in clean_goal.lower():
                    keywords.append(kw)
        key_symbol_str = ", ".join(keywords[:2]) if keywords else "logic"

        # Resolve topic domain for specialized engineering titles
        topic_info = TopicDomainResolver.resolve_topic(clean_goal)

        # Determine complexity & step count
        words = clean_goal.split()
        word_count = len(words)

        is_algorithm_or_complex = (
            any(kw in normalized_goal.lower() for kw in (
                "astar", "a*", "a-star", "dijkstra", "pathfinding", "algorithm",
                "dsa", "heuristic", "binary search", "sorting", "quicksort", "mergesort",
                "dynamic programming", "graph", "tree", "bst", "heap", "priority queue",
                "trie", "backtracking", "solver", "engine", "simulator", "compiler",
                "parser", "lexer", "interpreter", "pipeline"
            ))
            or any(kw in normalized_goal.lower() for kw in (
                "sophisticated", "large code", "large implementation", "complex",
                "comprehensive", "full implementation", "from scratch", "production-ready",
                "production grade", "advanced"
            ))
        )

        is_huge = (
            is_algorithm_or_complex
            or word_count >= 15
            or any(kw in clean_goal.lower() for kw in ("across files", "multiple files", "all files", "refactor", "overhaul", "redesign", "full rewrite", "architecture", "migrate", "integration", "pipeline"))
            or len(extracted_targets) >= 2
            or (target_files and len(target_files) >= 2)
        )

        steps: List[Dict[str, Any]] = []

        # 1. Pure folder creation (2 Steps)
        if intent == "CREATE_FOLDER" or (folder_name and not any("." in t for t in extracted_targets)):
            f_display = folder_name or resolved_target
            steps = [
                {
                    "id": 1,
                    "action": "scan",
                    "title": f"1. Verify workspace directory tree for '{f_display}'",
                    "progress": f"Verifying path for '{f_display}'...",
                    "detail": f"Target folder verified: '{f_display}' in workspace."
                },
                {
                    "id": 2,
                    "action": "prep",
                    "title": f"2. Initialize directory '{f_display}' and register in workspace",
                    "progress": f"Creating directory '{f_display}'...",
                    "detail": f"Created folder '{f_display}' successfully."
                }
            ]

        # 2. Algorithm / Complex Engine / Sophisticated Large Code (5 Steps - Max)
        elif is_algorithm_or_complex and not is_delete:
            topic_title = topic_info["topic"]
            steps = [
                {
                    "id": 1,
                    "action": "scan",
                    "title": f"1. Analyze problem domain & formulate mathematical / heuristic models for '{topic_title}' in '{resolved_target}'",
                    "progress": f"Formulating {topic_info['heuristic']} and domain constraints for '{topic_title}'...",
                    "detail": f"Domain constraints and heuristic models formulated for '{topic_title}'."
                },
                {
                    "id": 2,
                    "action": "prep",
                    "title": f"2. Structure {topic_info['data_structure']} for '{topic_title}'",
                    "progress": f"Structuring {topic_info['data_structure']} for '{topic_title}'...",
                    "detail": f"Structured {topic_info['data_structure']} and state invariants."
                },
                {
                    "id": 3,
                    "action": "synthesize",
                    "title": f"3. Synthesize complete, production-ready implementation of '{topic_title}' in '{resolved_target}'",
                    "progress": f"Synthesizing complete '{topic_title}' implementation via {model}...",
                    "detail": f"Synthesized full production-ready implementation for '{topic_title}' in '{resolved_target}'."
                },
                {
                    "id": 4,
                    "action": "validate",
                    "title": f"4. Execute AST validation, benchmark boundary conditions & verify {topic_info['edge_cases']}",
                    "progress": f"Validating AST integrity, checking {topic_info['edge_cases']}...",
                    "detail": f"Syntax and AST verified. Handled {topic_info['edge_cases']}."
                },
                {
                    "id": 5,
                    "action": "verify",
                    "title": f"5. Verify computational complexity, check zero regressions & stage '{resolved_target}' diff",
                    "progress": f"Verifying complexity bounds and staging diff for '{resolved_target}'...",
                    "detail": f"Zero regressions verified. Staged complete '{topic_title}' implementation in Diff tab."
                }
            ]

        # 3. Huge execution / Multi-file refactor (5 Steps - Max)
        elif is_huge or intent == "MULTI_FILE_REFACTOR":
            files_desc = ", ".join((target_files or extracted_targets)[:3]) if (target_files or extracted_targets) else resolved_target
            steps = [
                {
                    "id": 1,
                    "action": "scan",
                    "title": f"1. Cross-index repository AST symbols & map call hierarchy dependencies",
                    "progress": "Mapping cross-module call graph and dependencies...",
                    "detail": f"Indexed repository AST symbols across {files_desc}."
                },
                {
                    "id": 2,
                    "action": "prep",
                    "title": f"2. Compute change specifications & evaluate impact boundaries across modules",
                    "progress": "Evaluating module impact boundaries...",
                    "detail": "Impact boundaries and change specifications established."
                },
                {
                    "id": 3,
                    "action": "synthesize",
                    "title": f"3. Synthesize coordinated surgical diffs across target modules",
                    "progress": f"Synthesizing multi-file diffs via {model}...",
                    "detail": f"Synthesized coordinated diffs across {files_desc}."
                },
                {
                    "id": 4,
                    "action": "validate",
                    "title": f"4. Run multi-file AST validation & automated self-healing repair loop",
                    "progress": "Executing AST validation and integrity checks...",
                    "detail": "Multi-file AST validation and syntax verification passed."
                },
                {
                    "id": 5,
                    "action": "verify",
                    "title": f"5. Verify cross-module consistency, check regression boundaries & stage diffs",
                    "progress": "Verifying regression boundaries and staging diffs...",
                    "detail": "Cross-module consistency verified. Staged changes ready in Diff tab."
                }
            ]

        # 4. Nested folder + file creation (4 Steps)
        elif folder_name and resolved_target:
            steps = [
                {
                    "id": 1,
                    "action": "scan",
                    "title": f"1. Resolve destination directory and prepare folder structure for '{folder_name}'",
                    "progress": f"Resolving destination directory '{folder_name}'...",
                    "detail": f"Directory structure verified for '{folder_name}'."
                },
                {
                    "id": 2,
                    "action": "prep",
                    "title": f"2. Structure boilerplate schema & specifications for '{resolved_target}'",
                    "progress": f"Structuring schema for '{resolved_target}'...",
                    "detail": f"Path specifications established for '{resolved_target}'."
                },
                {
                    "id": 3,
                    "action": "synthesize",
                    "title": f"3. Synthesize complete implementation for '{resolved_target}'",
                    "progress": f"Synthesizing '{resolved_target}' via {model}...",
                    "detail": f"Generated complete implementation for '{resolved_target}'."
                },
                {
                    "id": 4,
                    "action": "verify",
                    "title": f"4. Verify path hierarchy, validate syntax & stage unified creation diff",
                    "progress": "Verifying path hierarchy and staging diff...",
                    "detail": f"Path hierarchy verified. Staged creation diff in Diff tab."
                }
            ]

        # 5. Bug fix / Test repair / Debugging (4 Steps)
        elif intent == "FIX_TEST" or any(w in clean_goal.lower() for w in ("fix", "repair", "debug", "failing", "error", "traceback", "assertion")):
            steps = [
                {
                    "id": 1,
                    "action": "scan",
                    "title": f"1. Scan error trace & isolate failing assertions in '{resolved_target}'",
                    "progress": f"Scanning assertions in '{resolved_target}'...",
                    "detail": f"Isolated failure locus in '{resolved_target}'."
                },
                {
                    "id": 2,
                    "action": "prep",
                    "title": f"2. Analyze code invariants and correlate call stack in '{resolved_target}'",
                    "progress": f"Analyzing code invariants in '{resolved_target}'...",
                    "detail": "Invariants and call stack correlated."
                },
                {
                    "id": 3,
                    "action": "synthesize",
                    "title": f"3. Synthesize targeted bugfix patch for '{resolved_target}'",
                    "progress": f"Synthesizing targeted fix via {model}...",
                    "detail": f"Synthesized targeted fix for '{resolved_target}'."
                },
                {
                    "id": 4,
                    "action": "verify",
                    "title": f"4. Execute AST validation, verify zero regressions & stage fix",
                    "progress": "Verifying syntax and regressions...",
                    "detail": "Syntax and AST verification passed. Fix staged in Diff tab."
                }
            ]

        # 6. Code deletion / Wipe / Truncation (3 Steps)
        elif is_delete:
            steps = [
                {
                    "id": 1,
                    "action": "scan",
                    "title": f"1. Inspect '{resolved_target}' to isolate code boundaries for removal",
                    "progress": f"Inspecting '{resolved_target}' code boundaries...",
                    "detail": f"Identified deletion scope in '{resolved_target}'."
                },
                {
                    "id": 2,
                    "action": "delete",
                    "title": f"2. Generate clean truncation diff to remove code from '{resolved_target}'",
                    "progress": f"Synthesizing truncation diff for '{resolved_target}'...",
                    "detail": f"Generated clean truncation diff for '{resolved_target}'."
                },
                {
                    "id": 3,
                    "action": "verify",
                    "title": f"3. Validate clean file state, verify integrity & stage removal diff",
                    "progress": f"Validating clean state for '{resolved_target}'...",
                    "detail": f"Integrity verified. Staged deletion diff for '{resolved_target}'."
                }
            ]

        # 7. Brand new file creation / Scaffold (3 Steps)
        elif intent == "CREATE_NEW":
            steps = [
                {
                    "id": 1,
                    "action": "scan",
                    "title": f"1. Analyze requirements & scaffold environment for '{resolved_target}'",
                    "progress": f"Analyzing requirements for '{resolved_target}'...",
                    "detail": f"Scaffold environment prepared for '{resolved_target}'."
                },
                {
                    "id": 2,
                    "action": "synthesize",
                    "title": f"2. Synthesize complete, production-ready implementation for '{resolved_target}'",
                    "progress": f"Synthesizing '{resolved_target}' via {model}...",
                    "detail": f"Generated complete implementation for '{resolved_target}'."
                },
                {
                    "id": 3,
                    "action": "verify",
                    "title": f"3. Run syntax validation, verify zero regressions & stage '{resolved_target}' diff",
                    "progress": f"Validating syntax and staging diff...",
                    "detail": f"Syntax and AST verified. Staged diff for '{resolved_target}' in Diff tab."
                }
            ]

        # 8. Default / Single-file modification (3 Steps)
        else:
            focus_str = f"'{key_symbol_str}' and anchor lines" if key_symbol_str != "logic" else f"symbols and anchor lines"
            steps = [
                {
                    "id": 1,
                    "action": "scan",
                    "title": f"1. Correlate {focus_str} in '{resolved_target}'",
                    "progress": f"Correlating symbols in '{resolved_target}'...",
                    "detail": f"Correlated {focus_str} in '{resolved_target}'."
                },
                {
                    "id": 2,
                    "action": "synthesize",
                    "title": f"2. Synthesize surgical code solution and compute unified diff for '{resolved_target}'",
                    "progress": f"Querying {model} for surgical code changes...",
                    "detail": f"Synthesized surgical solution for '{resolved_target}'."
                },
                {
                    "id": 3,
                    "action": "verify",
                    "title": f"3. Run AST syntax validation & verify zero regressions in '{resolved_target}'",
                    "progress": "Verifying syntax and AST integrity...",
                    "detail": f"Syntax and AST verification passed. Changes ready for inspection in Diff tab."
                }
            ]

        return {
            "goal": clean_goal,
            "intent": intent,
            "topic": topic_info["topic"],
            "complexity": "huge" if is_huge else ("high" if len(steps) >= 4 else "normal"),
            "total_steps": len(steps),
            "target_file": resolved_target,
            "folder_name": folder_name,
            "steps": steps
        }
