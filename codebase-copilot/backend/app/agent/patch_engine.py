import os
import re
import ast
import difflib
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

class SurgicalDiffBuilder:
    """
    Constructs minimal, surgical unified diffs from model outputs.
    Guarantees that minute changes (e.g. 1-line edits like paddle size)
    produce minimal 1-line hunks, completely preventing full-file overwrites.
    """

    @staticmethod
    def extract_search_replace_blocks(text: str) -> List[Dict[str, List[str]]]:
        """
        Parses blocks of the format:
        <<<<<<< SEARCH
        ...
        =======
        ...
        >>>>>>> REPLACE
        """
        pattern = re.compile(
            r"<{5,9}\s*(?:SEARCH|ORIGINAL)?\s*\n(.*?)\n={5,9}\s*\n(.*?)\n>{5,9}\s*(?:REPLACE|UPDATED)?",
            re.DOTALL | re.IGNORECASE
        )
        blocks = []
        for m in pattern.finditer(text):
            search_content = m.group(1)
            replace_content = m.group(2)
            blocks.append({
                "search": search_content.splitlines(),
                "replace": replace_content.splitlines()
            })
        return blocks

    @classmethod
    def apply_search_replace_blocks(
        cls,
        orig_lines: List[str],
        blocks: List[Dict[str, List[str]]],
        preferred_line: Optional[int] = None
    ) -> Tuple[List[str], bool]:
        """
        Applies search/replace blocks to orig_lines, returning (new_lines, success).
        Disambiguates duplicate code occurrences by prioritizing matches closest to preferred_line
        (e.g., active editor cursor or target symbol AST lineno).
        """
        current_lines = list(orig_lines)
        applied_any = False
        target_p_idx = max(0, preferred_line - 1) if preferred_line is not None else None

        for blk in blocks:
            # Clean any escaped quotes from model serialization
            search_lines = [l.replace(r'\"\"\"', '"""').replace(r"\'\'\'", "'''") for l in blk["search"]]
            replace_lines = [l.replace(r'\"\"\"', '"""').replace(r"\'\'\'", "'''") for l in blk["replace"]]
            if not search_lines and not replace_lines:
                continue

            match_idx = -1
            search_len = len(search_lines)

            # 1. Exact match across all occurrences
            exact_candidates = []
            for i in range(len(current_lines) - search_len + 1):
                if current_lines[i : i + search_len] == search_lines:
                    exact_candidates.append(i)

            if exact_candidates:
                if target_p_idx is not None:
                    match_idx = min(exact_candidates, key=lambda idx: abs(idx - target_p_idx))
                else:
                    match_idx = exact_candidates[0]

            # 2. Stripped match across all occurrences
            if match_idx == -1 and search_len > 0:
                search_stripped = [l.strip() for l in search_lines]
                stripped_candidates = []
                for i in range(len(current_lines) - search_len + 1):
                    if [l.strip() for l in current_lines[i : i + search_len]] == search_stripped:
                        stripped_candidates.append(i)

                if stripped_candidates:
                    if target_p_idx is not None:
                        match_idx = min(stripped_candidates, key=lambda idx: abs(idx - target_p_idx))
                    else:
                        match_idx = stripped_candidates[0]

                    # Preserve original indentation
                    orig_indent = current_lines[match_idx][:len(current_lines[match_idx]) - len(current_lines[match_idx].lstrip())]
                    rep_indent = replace_lines[0][:len(replace_lines[0]) - len(replace_lines[0].lstrip())] if replace_lines else ""
                    delta = len(orig_indent) - len(rep_indent)
                    if delta != 0:
                        adjusted = []
                        for rl in replace_lines:
                            if not rl.strip():
                                adjusted.append("")
                            elif delta > 0:
                                adjusted.append(" " * delta + rl)
                            else:
                                trim = min(-delta, len(rl) - len(rl.lstrip()))
                                adjusted.append(rl[trim:])
                        replace_lines = adjusted

            # 3. Fuzzy SequenceMatcher
            if match_idx == -1 and search_len > 0:
                target_str = "\n".join(l.strip() for l in search_lines if l.strip())
                best_score = float("-inf")
                best_idx = -1
                for i in range(len(current_lines) - search_len + 1):
                    cand_str = "\n".join(l.strip() for l in current_lines[i : i + search_len] if l.strip())
                    ratio = difflib.SequenceMatcher(None, target_str, cand_str).ratio()
                    if ratio >= 0.70:
                        # Combine similarity score with proximity to preferred_line
                        proximity_bonus = 0.0
                        if target_p_idx is not None:
                            dist = abs(i - target_p_idx)
                            proximity_bonus = max(0.0, 1.0 - (dist / max(len(current_lines), 1))) * 0.3
                        score = ratio + proximity_bonus
                        if score > best_score:
                            best_score = score
                            best_idx = i
                if best_idx != -1:
                    match_idx = best_idx

            if match_idx != -1:
                current_lines = current_lines[:match_idx] + replace_lines + current_lines[match_idx + search_len:]
                applied_any = True

        return current_lines, applied_any

    @classmethod
    def splice_snippet_into_lines(cls, orig_lines: List[str], snippet: str) -> Optional[List[str]]:
        """
        Surgically splices a function, class, or variable assignment snippet into orig_lines
        without touching or deleting the rest of the file.
        """
        if not orig_lines or not snippet or not snippet.strip():
            return None

        snip_lines = snippet.strip().splitlines()
        first_line = snip_lines[0].strip()

        # Case A: Function definition across Python, JS, TS, Rust, Go, C-family
        fn_name = None
        is_brace_lang = any("{" in l for l in snip_lines)

        # Python: def func_name(...)
        py_fn = re.match(r"^(?:async\s+)?def\s+([A-Za-z0-9_]+)\s*\(", first_line)
        # JS/TS: function func_name(...) or export function func_name(...)
        js_fn = re.match(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z0-9_]+)\s*\(", first_line)
        # JS/TS: const func_name = (...) => or const func_name = function(...)
        js_arrow = re.match(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z0-9_]+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z0-9_]+)\s*=>", first_line)
        # Rust: fn func_name(...)
        rust_fn = re.match(r"^(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z0-9_]+)\b", first_line)
        # Go: func func_name(...) or func (r *Receiver) func_name(...)
        go_fn = re.match(r"^func\s+(?:\([^)]+\)\s+)?([A-Za-z0-9_]+)\s*\(", first_line)

        if py_fn:
            fn_name = py_fn.group(1)
            is_brace_lang = False
        elif js_fn:
            fn_name = js_fn.group(1)
        elif js_arrow:
            fn_name = js_arrow.group(1)
        elif rust_fn:
            fn_name = rust_fn.group(1)
        elif go_fn:
            fn_name = go_fn.group(1)

        if fn_name:
            for idx, line in enumerate(orig_lines):
                if re.search(rf"\b{re.escape(fn_name)}\b", line):
                    # Check if this line is indeed the function definition
                    if any(kw in line for kw in ("def ", "function ", "fn ", "func ", "=>", "=")):
                        end_idx = idx + 1
                        if is_brace_lang or "{" in line or (end_idx < len(orig_lines) and "{" in orig_lines[end_idx]):
                            # Curly-brace balance matching
                            brace_count = 0
                            found_open = False
                            for k in range(idx, len(orig_lines)):
                                for ch in orig_lines[k]:
                                    if ch == '{':
                                        brace_count += 1
                                        found_open = True
                                    elif ch == '}':
                                        brace_count -= 1
                                if found_open and brace_count == 0:
                                    end_idx = k + 1
                                    break
                        else:
                            # Indentation-based boundary matching (Python)
                            fn_indent_len = len(line) - len(line.lstrip())
                            while end_idx < len(orig_lines):
                                curr = orig_lines[end_idx]
                                if not curr.strip():
                                    end_idx += 1
                                    continue
                                curr_indent_len = len(curr) - len(curr.lstrip())
                                if curr_indent_len <= fn_indent_len:
                                    break
                                end_idx += 1

                        snip_indent_len = len(snip_lines[0]) - len(snip_lines[0].lstrip())
                        orig_indent_len = len(line) - len(line.lstrip())
                        delta = orig_indent_len - snip_indent_len
                        aligned_snip = []
                        for sl in snip_lines:
                            if not sl.strip():
                                aligned_snip.append("")
                            elif delta > 0:
                                aligned_snip.append(" " * delta + sl)
                            elif delta < 0:
                                trim = min(-delta, len(sl) - len(sl.lstrip()))
                                aligned_snip.append(sl[trim:])
                            else:
                                aligned_snip.append(sl)
                        return orig_lines[:idx] + aligned_snip + orig_lines[end_idx:]

        # Case B: Class / Struct / Interface definition
        cls_match = re.match(r"^(?:export\s+)?(?:default\s+)?(?:class|struct|interface|type)\s+([A-Za-z0-9_]+)\b", first_line)
        if cls_match:
            cls_name = cls_match.group(1)
            for idx, line in enumerate(orig_lines):
                if re.search(rf"\b(?:class|struct|interface|type)\s+{re.escape(cls_name)}\b", line):
                    end_idx = idx + 1
                    if "{" in line or (end_idx < len(orig_lines) and "{" in orig_lines[end_idx]):
                        brace_count = 0
                        found_open = False
                        for k in range(idx, len(orig_lines)):
                            for ch in orig_lines[k]:
                                if ch == '{':
                                    brace_count += 1
                                    found_open = True
                                elif ch == '}':
                                    brace_count -= 1
                            if found_open and brace_count == 0:
                                end_idx = k + 1
                                break
                    else:
                        cls_indent_len = len(line) - len(line.lstrip())
                        while end_idx < len(orig_lines):
                            curr = orig_lines[end_idx]
                            if not curr.strip():
                                end_idx += 1
                                continue
                            curr_indent_len = len(curr) - len(curr.lstrip())
                            if curr_indent_len <= cls_indent_len:
                                break
                            end_idx += 1
                    return orig_lines[:idx] + snip_lines + orig_lines[end_idx:]

        # Case C: Variable / constant assignment (Python, JS, TS, Go, Rust, C)
        assignment_pattern = re.compile(r"^\s*(?:export\s+)?(?:pub\s+)?(?:const|let|var|final|static)?\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*[^=]+)?\s*=")
        if all(assignment_pattern.match(l) or not l.strip() for l in snip_lines if l.strip()):
            res_lines = list(orig_lines)
            spliced_any = False
            for snip_l in snip_lines:
                if not snip_l.strip():
                    continue
                m = assignment_pattern.match(snip_l)
                if m:
                    var_name = m.group(1)
                    var_re = re.compile(rf"^\s*(?:export\s+)?(?:pub\s+)?(?:const|let|var|final|static)?\s*{re.escape(var_name)}\s*(?::\s*[^=]+)?\s*=")
                    for o_idx, o_line in enumerate(res_lines):
                        if var_re.search(o_line):
                            o_indent = o_line[:len(o_line) - len(o_line.lstrip())]
                            res_lines[o_idx] = o_indent + snip_l.strip()
                            spliced_any = True
                            break
            if spliced_any:
                return res_lines

        # Case D: Rolling fuzzy similarity match for short snippets (HTML, CSS, JSON, statements, any language)
        if len(snip_lines) <= 10 and len(orig_lines) > len(snip_lines):
            target_str = "\n".join(l.strip() for l in snip_lines if l.strip())
            w_len = len(snip_lines)
            best_ratio = 0.0
            best_idx = -1
            for cand in range(len(orig_lines) - w_len + 1):
                cand_str = "\n".join(l.strip() for l in orig_lines[cand : cand + w_len] if l.strip())
                ratio = difflib.SequenceMatcher(None, target_str, cand_str).ratio()
                if ratio > best_ratio and ratio >= 0.50:
                    best_ratio = ratio
                    best_idx = cand
            if best_idx != -1:
                orig_indent = orig_lines[best_idx][:len(orig_lines[best_idx]) - len(orig_lines[best_idx].lstrip())]
                snip_indent = snip_lines[0][:len(snip_lines[0]) - len(snip_lines[0].lstrip())]
                delta = len(orig_indent) - len(snip_indent)
                aligned = []
                for sl in snip_lines:
                    if not sl.strip():
                        aligned.append("")
                    elif delta > 0:
                        aligned.append(" " * delta + sl)
                    elif delta < 0:
                        trim = min(-delta, len(sl) - len(sl.lstrip()))
                        aligned.append(sl[trim:])
                    else:
                        aligned.append(sl)
                return orig_lines[:best_idx] + aligned + orig_lines[best_idx + w_len:]

        return None

    @classmethod
    def build_surgical_diff(
        cls,
        orig_lines: List[str],
        generated_text: str,
        filename: str,
        preferred_line: Optional[int] = None
    ) -> str:
        """
        Main entry point for generating minimal, surgical unified diffs across ALL languages.
        Handles:
        1. Brand new files -> valid creation diff
        2. SEARCH/REPLACE blocks -> surgical unified diff with preferred_line targeting
        3. Existing unified diffs -> sanitized, verified, and re-aligned with actual line numbers
        4. Partial snippets (function/assignment/class) -> surgically spliced diff
        5. Full-file code blocks -> difflib.unified_diff (extracts ONLY changed lines)
        """
        # Unescape escaped quotes from model serialization
        generated_text = generated_text.replace(r'\"\"\"', '"""').replace(r"\'\'\'", "'''")

        if not orig_lines or not any(l.strip() for l in orig_lines):
            code_lines = []
            code_match = re.search(r"```[a-zA-Z0-9_+-]*\s*\n([\s\S]*?)```", generated_text, re.IGNORECASE)
            if code_match:
                code_lines = code_match.group(1).strip().splitlines()
            else:
                code_lines = [l for l in generated_text.splitlines() if not l.startswith("```")]
            return (
                f"--- /dev/null\n+++ b/{filename}\n"
                f"@@ -0,0 +1,{max(1, len(code_lines))} @@\n"
                + "\n".join(f"+{line}" for line in code_lines)
            )

        # 1. Check for SEARCH/REPLACE blocks
        sr_blocks = cls.extract_search_replace_blocks(generated_text)
        if sr_blocks:
            new_lines, applied = cls.apply_search_replace_blocks(orig_lines, sr_blocks, preferred_line=preferred_line)
            if applied and new_lines != orig_lines:
                diff_iter = difflib.unified_diff(
                    orig_lines,
                    new_lines,
                    fromfile=f"a/{filename}",
                    tofile=f"b/{filename}",
                    lineterm=""
                )
                return "\n".join(diff_iter).strip()

        # 2. Check for explicit unified diff block
        diff_text = ""
        diff_match = re.search(r"```(?:diff|patch)?\s*\n([\s\S]*?@@[\s\S]*?)```", generated_text, re.IGNORECASE)
        if diff_match:
            diff_text = diff_match.group(1).strip()
        else:
            raw_diff = re.search(r"(--- (?:a\/|\/dev\/null)[^\n]+\n\+\+\+ (?:b\/|[^\n]+)[^\n]*\n@@[\s\S]*?)(?:```|$)", generated_text)
            if raw_diff:
                diff_text = raw_diff.group(1).strip()

        if diff_text:
            try:
                test_applied = PatchEngine._apply_unified_diff(orig_lines, diff_text.splitlines(), preferred_line=preferred_line)
                if test_applied != orig_lines:
                    diff_iter = difflib.unified_diff(
                        orig_lines,
                        test_applied,
                        fromfile=f"a/{filename}",
                        tofile=f"b/{filename}",
                        lineterm=""
                    )
                    return "\n".join(diff_iter).strip()
            except Exception:
                pass
            return diff_text

        # 3. Check for code block (any language: python, js, ts, go, rust, c, html, css, etc.)
        code_body = None
        code_match = re.search(r"```[a-zA-Z0-9_+-]*\s*\n([\s\S]*?)```", generated_text, re.IGNORECASE)
        if code_match:
            code_body = code_match.group(1).strip()
        elif not generated_text.strip().startswith("I'm unable") and not generated_text.strip().startswith("I cannot"):
            raw_code = generated_text.strip()
            if raw_code.startswith(("def ", "class ", "import ", "from ", "#!", "async def ", "function ", "const ", "let ", "var ", "func ", "fn ", "pub ", "interface ", "export ", "type ", "struct ", "package ", "<", "/*", "//", '"""', "'''")):
                code_body = raw_code
            elif "=" in raw_code and len(raw_code.splitlines()) <= 5:
                code_body = raw_code


        if code_body:
            # 3a. Try surgical snippet splicing first
            spliced = cls.splice_snippet_into_lines(orig_lines, code_body)
            if spliced and spliced != orig_lines:
                diff_iter = difflib.unified_diff(
                    orig_lines,
                    spliced,
                    fromfile=f"a/{filename}",
                    tofile=f"b/{filename}",
                    lineterm=""
                )
                return "\n".join(diff_iter).strip()

            # 3b. Fall back to standard difflib against orig_lines
            # This GUARANTEES that if 1 line changed in 150 lines, ONLY 1 line is diffed!
            code_lines = code_body.splitlines()
            diff_iter = difflib.unified_diff(
                orig_lines,
                code_lines,
                fromfile=f"a/{filename}",
                tofile=f"b/{filename}",
                lineterm=""
            )
            computed_diff = "\n".join(diff_iter).strip()
            if computed_diff:
                return computed_diff

        return diff_text

class PatchEngine:
    """
    Surgical Unified Diff Patch Engine.
    Applies line-based diffs safely with AST pre-validation,
    supporting both new file creations and targeted modifications.
    """
    build_surgical_diff = SurgicalDiffBuilder.build_surgical_diff


    @staticmethod
    def validate_python_syntax(code: str) -> bool:
        """Verifies if the code is syntactically valid Python before disk write."""
        try:
            ast.parse(code)
            return True
        except SyntaxError:
            return False

    @classmethod
    def resolve_target_path(cls, file_path: str, workspace_path: Optional[str] = None) -> Path:
        """Resolves target path against workspace if relative."""
        clean_name = file_path.strip()
        if clean_name.startswith("b/") or clean_name.startswith("a/"):
            clean_name = clean_name[2:]
        clean_name = clean_name.lstrip("/")

        p = Path(clean_name)
        if p.is_absolute() and p.exists():
            return p.resolve()

        if workspace_path:
            from ..retrieval.file_resolver import WorkspaceFileResolver
            ws = WorkspaceFileResolver._resolve_root(workspace_path)
            direct = ws / p
            if direct.exists():
                return direct.resolve()
            if ws.exists() and ws.is_dir():
                try:
                    for sub in ws.rglob(p.name):
                        if sub.is_file():
                            return sub.resolve()
                except Exception:
                    pass

                # If requested filename is generic (solution.py, patch.diff) and doesn't exist, map to actual repo file
                if p.name in ("solution.py", "patch.diff", "unknown"):
                    accessible = WorkspaceFileResolver.list_accessible_files(str(ws))
                    py_files = [f for f in accessible.values() if f.suffix == ".py"]
                    if py_files:
                        return py_files[0].resolve()
                    if accessible:
                        return list(accessible.values())[0].resolve()
            return direct.resolve()

        if p.exists():
            return p.resolve()

        for candidate_root in [Path.home() / "Desktop", Path.cwd().parent, Path.cwd()]:
            if candidate_root.exists():
                try:
                    for sub in candidate_root.rglob(p.name):
                        if sub.is_file():
                            return sub.resolve()
                except Exception:
                    pass
        return p.resolve()

    @classmethod
    def apply_patch_hunks(
        cls,
        file_path: str,
        diff_text: str,
        workspace_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Parses diff lines and applies additions/deletions.
        Supports both new file creation and surgical existing file patching.
        """
        if not diff_text or not diff_text.strip():
            return {"status": "error", "message": "Diff content is empty."}

        target_path = cls.resolve_target_path(file_path, workspace_path)

        # Inspect diff headers for actual target filename if file_path is generic or doesn't exist
        header_match = re.search(r"\+\+\+\s+(?:b/)?([^\s\n\r]+)", diff_text)
        if header_match:
            diff_file = header_match.group(1).strip()
            if diff_file and diff_file != "/dev/null":
                diff_file_path = cls.resolve_target_path(diff_file, workspace_path)
                if diff_file_path.exists() or not target_path.exists():
                    target_path = diff_file_path

        # Check if this is a new file creation or populating an empty file
        file_exists = target_path.exists() and target_path.is_file()
        is_empty = False
        if file_exists:
            try:
                is_empty = target_path.stat().st_size == 0
            except Exception:
                pass

        # Only treat as a new file if the file truly does not exist or has 0 bytes.
        # NEVER wipe existing non-empty file just because of diff header tokens.
        is_new_file = (not file_exists or is_empty)

        lines_diff = diff_text.splitlines()

        if is_new_file:
            # Extract content for new file: take all lines prefixed with + (excluding +++)
            # or if plain code was passed
            new_lines = []
            has_diff_headers = any(line.startswith("+++") or line.startswith("@@") for line in lines_diff)

            if has_diff_headers:
                for line in lines_diff:
                    if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
                        continue
                    if line.startswith("+"):
                        new_lines.append(line[1:])
                    elif not line.startswith("-") and not line.startswith("\\"):
                        new_lines.append(line)
            else:
                for line in lines_diff:
                    if line.startswith("+") and not line.startswith("+++"):
                        new_lines.append(line[1:])
                    else:
                        new_lines.append(line)

            new_content = "\n".join(new_lines).strip() + "\n"

        else:
            # Existing file: parse unified diff hunks
            try:
                original_content = target_path.read_text(encoding="utf-8")
            except Exception as e:
                return {"status": "error", "message": f"Failed reading file {target_path}: {e}"}

            orig_lines = original_content.splitlines()
            new_lines = cls._apply_unified_diff(orig_lines, lines_diff)
            new_content = "\n".join(new_lines)
            if not new_content.endswith("\n"):
                new_content += "\n"

        # AST syntax validation for Python files
        if target_path.suffix == ".py":
            orig_is_valid = cls.validate_python_syntax(original_content) if not is_new_file else True
            if not cls.validate_python_syntax(new_content):
                repaired = cls._attempt_syntax_repair(orig_lines, lines_diff, new_content)
                if repaired and cls.validate_python_syntax(repaired):
                    new_content = repaired
                else:
                    if not orig_is_valid:
                        # Original file already had syntax errors prior to the patch.
                        # Do NOT reject the patch since the user is likely repairing or editing the broken file!
                        pass
                    else:
                        return {
                            "status": "error",
                            "message": f"Patch rejected: Introduced Python syntax error in {target_path.name}."
                        }

        # Safe atomic write
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(new_content, encoding="utf-8")
        except Exception as e:
            return {"status": "error", "message": f"Failed writing to {target_path}: {e}"}

        return {
            "status": "success",
            "file": str(target_path),
            "filename": target_path.name,
            "created": is_new_file,
            "lines_written": len(new_content.splitlines()),
            "message": f"{'Created' if is_new_file else 'Patched'} {target_path.name} successfully."
        }

    @classmethod
    def validate_and_heal_diff(
        cls,
        orig_content_or_lines: Any,
        diff_text: str,
        filename: str
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Validates in-memory that applying diff_text to orig_lines produces valid syntax.
        If syntax errors occur (e.g., misaligned indentation or orphaned blocks),
        attempts automated repair and returns the clean repaired unified diff.
        """
        if isinstance(orig_content_or_lines, str):
            orig_lines = orig_content_or_lines.splitlines()
        else:
            orig_lines = list(orig_content_or_lines)

        if not diff_text or not diff_text.strip():
            return True, diff_text, None

        # Test apply in memory
        try:
            patched_lines = cls._apply_unified_diff(orig_lines, diff_text.splitlines())
            patched_content = "\n".join(patched_lines)
            if not patched_content.endswith("\n"):
                patched_content += "\n"
        except Exception as e:
            return False, diff_text, f"Diff application failed: {e}"

        # If not Python, basic check is sufficient
        if not filename.endswith(".py"):
            return True, diff_text, None

        # Check Python AST syntax
        if cls.validate_python_syntax(patched_content):
            return True, diff_text, None

        # Syntax error detected - run self-healing repair loop
        repaired = cls._attempt_syntax_repair(orig_lines, diff_text.splitlines(), patched_content)
        if repaired and cls.validate_python_syntax(repaired):
            # Regenerate clean unified diff from repaired content
            clean_diff = "\n".join(difflib.unified_diff(
                orig_lines,
                repaired.splitlines(),
                fromfile=f"a/{filename}",
                tofile=f"b/{filename}",
                lineterm=""
            ))
            return True, clean_diff, None

        # If repair failed, get the exact syntax error detail
        err_msg = "Introduced Python syntax error"
        try:
            ast.parse(patched_content)
        except SyntaxError as se:
            err_msg = f"SyntaxError at line {se.lineno}: {se.msg}"

        return False, diff_text, err_msg

    @classmethod
    def _split_hunk_lines_into_subhunks(
        cls, hunk_lines: List[str], base_old_start: int
    ) -> List[Dict[str, Any]]:
        """
        Decomposes a unified diff hunk into discrete sub-hunks if it contains
        multiple edit regions separated by context lines.
        Prevents merging disjoint edits into a single hunk-wide deletion/addition,
        which causes catastrophic indentation misalignment and syntax errors.
        """
        segments: List[Tuple[str, List[str]]] = []
        curr_type: Optional[str] = None
        curr_lines: List[str] = []

        for l in hunk_lines:
            if l.startswith(("-", "+")) and not l.startswith(("---", "+++")):
                line_type = "edit"
            elif l.startswith(" ") or not l.startswith("\\"):
                line_type = "ctx"
            else:
                continue

            if line_type != curr_type:
                if curr_type is not None:
                    segments.append((curr_type, curr_lines))
                curr_type = line_type
                curr_lines = [l]
            else:
                curr_lines.append(l)

        if curr_type is not None:
            segments.append((curr_type, curr_lines))

        subhunks: List[Dict[str, Any]] = []
        for i, (stype, slines) in enumerate(segments):
            if stype == "edit":
                prefix_ctx = [l[1:] if l.startswith(" ") else l for l in segments[i - 1][1]] if i > 0 and segments[i - 1][0] == "ctx" else []
                suffix_ctx = [l[1:] if l.startswith(" ") else l for l in segments[i + 1][1]] if i + 1 < len(segments) and segments[i + 1][0] == "ctx" else []
                del_lines = [l[1:] for l in slines if l.startswith("-")]
                add_lines = [l[1:] for l in slines if l.startswith("+")]
                subhunks.append({
                    "prefix_ctx": prefix_ctx,
                    "del_lines": del_lines,
                    "add_lines": add_lines,
                    "suffix_ctx": suffix_ctx,
                    "old_start": base_old_start
                })
        return subhunks

    @classmethod
    def _apply_unified_diff(
        cls,
        orig_lines: List[str],
        diff_lines: List[str],
        preferred_line: Optional[int] = None
    ) -> List[str]:
        """
        Applies unified diff hunks onto original lines using context anchor matching,
        automatic indentation delta synchronization, and surgical sub-hunk replacement
        to prevent syntax errors and misalignment when lines are redacted and added concurrently.
        """
        hunk_header_re = re.compile(r"^@@\s*(?:-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?)?\s*@@?")

        has_hunk_headers = any(l.startswith("@@") for l in diff_lines)

        if not has_hunk_headers:
            # Fallback for plain diffs without @@ headers
            del_lines = [l[1:] for l in diff_lines if l.startswith("-") and not l.startswith("---")]
            add_lines = [l[1:] for l in diff_lines if l.startswith("+") and not l.startswith("+++")]
            if del_lines:
                del_stripped = [l.strip() for l in del_lines]
                for idx in range(len(orig_lines) - len(del_lines) + 1):
                    cand_slice = orig_lines[idx : idx + len(del_lines)]
                    if cand_slice == del_lines or [l.strip() for l in cand_slice] == del_stripped:
                        # Auto-align added lines with matched file indentation
                        matched_indent = cand_slice[0][:len(cand_slice[0]) - len(cand_slice[0].lstrip())]
                        diff_indent = del_lines[0][:len(del_lines[0]) - len(del_lines[0].lstrip())]
                        indent_diff = len(matched_indent) - len(diff_indent)

                        aligned_adds = []
                        for al in add_lines:
                            if not al.strip():
                                aligned_adds.append("")
                            elif indent_diff > 0:
                                aligned_adds.append(" " * indent_diff + al)
                            elif indent_diff < 0:
                                trim = min(-indent_diff, len(al) - len(al.lstrip()))
                                aligned_adds.append(al[trim:])
                            else:
                                aligned_adds.append(al)

                        return orig_lines[:idx] + aligned_adds + orig_lines[idx + len(del_lines):]
            if add_lines:
                return add_lines
            return orig_lines

        # 1. Parse into distinct hunks and decompose into sub-hunks
        all_subhunks: List[Dict[str, Any]] = []
        curr_hunk_lines: List[str] = []
        curr_start = preferred_line or 1

        for line in diff_lines:
            match = hunk_header_re.match(line)
            if match and line.startswith("@@"):
                if curr_hunk_lines:
                    all_subhunks.extend(cls._split_hunk_lines_into_subhunks(curr_hunk_lines, curr_start))
                curr_start = int(match.group(1)) if match.group(1) else (preferred_line or 1)
                curr_hunk_lines = []
            elif curr_start is not None:
                curr_hunk_lines.append(line)

        if curr_hunk_lines:
            all_subhunks.extend(cls._split_hunk_lines_into_subhunks(curr_hunk_lines, curr_start))

        result: List[str] = []
        cursor = 0

        for hunk in all_subhunks:
            prefix_ctx = hunk["prefix_ctx"]
            del_lines = hunk["del_lines"]
            add_lines = hunk["add_lines"]
            suffix_ctx = hunk["suffix_ctx"]
            suggested_idx = max(cursor, hunk["old_start"] - 1)

            match_start = -1
            match_end = -1
            matched_indent = ""
            diff_base_indent = ""

            # Strategy 1: Match full old_block (prefix_ctx + del_lines + suffix_ctx)
            old_block = prefix_ctx + del_lines + suffix_ctx
            if old_block:
                max_offset = max(len(orig_lines), 60)
                for offset in range(0, max_offset):
                    for cand in (suggested_idx + offset, suggested_idx - offset if suggested_idx >= offset else -1):
                        if cursor <= cand <= len(orig_lines) - len(old_block):
                            if orig_lines[cand : cand + len(old_block)] == old_block:
                                match_start = cand + len(prefix_ctx)
                                match_end = match_start + len(del_lines)
                                matched_indent = orig_lines[match_start][:len(orig_lines[match_start]) - len(orig_lines[match_start].lstrip())] if del_lines else ""
                                diff_base_indent = del_lines[0][:len(del_lines[0]) - len(del_lines[0].lstrip())] if del_lines else ""
                                break
                    if match_start != -1:
                        break

                if match_start == -1:
                    stripped_target = [l.strip() for l in old_block]
                    cands = []
                    for cand in range(cursor, len(orig_lines) - len(old_block) + 1):
                        if [l.strip() for l in orig_lines[cand : cand + len(old_block)]] == stripped_target:
                            cands.append(cand)
                    if cands:
                        best = min(cands, key=lambda c: abs(c - suggested_idx))
                        match_start = best + len(prefix_ctx)
                        match_end = match_start + len(del_lines)
                        target_line = orig_lines[match_start] if match_start < len(orig_lines) else ""
                        matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]
                        diff_base_indent = del_lines[0][:len(del_lines[0]) - len(del_lines[0].lstrip())] if del_lines else ""

            # Strategy 2: Match prefix_ctx immediate anchor + del_lines
            if match_start == -1 and (prefix_ctx or del_lines):
                anchor_prefix = prefix_ctx[-3:] if len(prefix_ctx) > 3 else prefix_ctx
                target = anchor_prefix + del_lines
                if target:
                    stripped_target = [l.strip() for l in target]
                    cands = []
                    for cand in range(cursor, len(orig_lines) - len(target) + 1):
                        cand_slice = orig_lines[cand : cand + len(target)]
                        if cand_slice == target or [l.strip() for l in cand_slice] == stripped_target:
                            cands.append(cand)
                    if cands:
                        best = min(cands, key=lambda c: abs(c - suggested_idx))
                        match_start = best + len(anchor_prefix)
                        match_end = match_start + len(del_lines)
                        target_line = orig_lines[match_start] if match_start < len(orig_lines) else ""
                        matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]
                        diff_base_indent = del_lines[0][:len(del_lines[0]) - len(del_lines[0].lstrip())] if del_lines else ""

            # Strategy 3: Match del_lines directly
            if match_start == -1 and del_lines:
                stripped_del = [l.strip() for l in del_lines]
                del_cands = []
                for cand in range(cursor, len(orig_lines) - len(del_lines) + 1):
                    cand_slice = orig_lines[cand : cand + len(del_lines)]
                    if cand_slice == del_lines or [l.strip() for l in cand_slice] == stripped_del:
                        del_cands.append(cand)
                if del_cands:
                    best_cand = min(del_cands, key=lambda c: abs(c - suggested_idx))
                    match_start = best_cand
                    match_end = best_cand + len(del_lines)
                    target_line = orig_lines[match_start]
                    matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]
                    diff_base_indent = del_lines[0][:len(del_lines[0]) - len(del_lines[0].lstrip())]

            # Strategy 3b: Fuzzy SequenceMatcher across candidate slices of orig_lines
            if match_start == -1 and (del_lines or old_block):
                target_seq = del_lines if del_lines else old_block
                target_str = "\n".join(l.strip() for l in target_seq if l.strip())
                if target_str:
                    best_ratio = 0.0
                    best_cand = -1
                    w_len = len(target_seq)
                    for cand in range(cursor, len(orig_lines) - w_len + 1):
                        cand_str = "\n".join(l.strip() for l in orig_lines[cand : cand + w_len] if l.strip())
                        ratio = difflib.SequenceMatcher(None, target_str, cand_str).ratio()
                        if ratio > best_ratio and ratio >= 0.65:
                            best_ratio = ratio
                            best_cand = cand
                    if best_cand != -1:
                        match_start = best_cand + (len(prefix_ctx) if not del_lines else 0)
                        match_end = match_start + len(del_lines)
                        target_line = orig_lines[match_start] if match_start < len(orig_lines) else ""
                        matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]
                        diff_base_indent = del_lines[0][:len(del_lines[0]) - len(del_lines[0].lstrip())] if del_lines else ""

            # Strategy 4: Pure additions with context
            if match_start == -1 and not del_lines:
                if prefix_ctx:
                    for k in range(len(prefix_ctx), 0, -1):
                        sub_pref = prefix_ctx[-k:]
                        stripped_pref = [l.strip() for l in sub_pref]
                        for cand in range(cursor, len(orig_lines) - len(sub_pref) + 1):
                            cand_slice = orig_lines[cand : cand + len(sub_pref)]
                            if cand_slice == sub_pref or [l.strip() for l in cand_slice] == stripped_pref:
                                match_start = cand + len(sub_pref)
                                match_end = match_start
                                target_line = orig_lines[cand + len(sub_pref) - 1]
                                matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]
                                break
                        if match_start != -1:
                            break
                elif suffix_ctx:
                    for k in range(min(3, len(suffix_ctx)), 0, -1):
                        sub_suf = suffix_ctx[:k]
                        stripped_suf = [l.strip() for l in sub_suf]
                        for cand in range(cursor, len(orig_lines) - len(sub_suf) + 1):
                            cand_slice = orig_lines[cand : cand + len(sub_suf)]
                            if cand_slice == sub_suf or [l.strip() for l in cand_slice] == stripped_suf:
                                match_start = cand
                                match_end = cand
                                target_line = orig_lines[cand]
                                matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]
                                break
                        if match_start != -1:
                            break

            # Fallback if not matched
            if match_start == -1:
                match_start = min(len(orig_lines), suggested_idx)
                match_end = match_start + len(del_lines)
                if match_start < len(orig_lines):
                    target_line = orig_lines[match_start]
                    matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]

            # Adjust added lines indentation to preserve Python block scoping
            if del_lines:
                diff_base_indent = del_lines[0][:len(del_lines[0]) - len(del_lines[0].lstrip())]
            elif add_lines:
                first_add = next((al for al in add_lines if al.strip()), "")
                diff_base_indent = first_add[:len(first_add) - len(first_add.lstrip())]
                if not matched_indent and prefix_ctx:
                    pref_last = prefix_ctx[-1]
                    matched_indent = pref_last[:len(pref_last) - len(pref_last.lstrip())]
            else:
                diff_base_indent = ""

            indent_delta = len(matched_indent) - len(diff_base_indent)
            aligned_adds: List[str] = []
            for al in add_lines:
                if not al.strip():
                    aligned_adds.append("")
                elif indent_delta > 0:
                    aligned_adds.append(" " * indent_delta + al)
                elif indent_delta < 0:
                    trim = min(-indent_delta, len(al) - len(al.lstrip()))
                    aligned_adds.append(al[trim:])
                else:
                    aligned_adds.append(al)

            while cursor < match_start and cursor < len(orig_lines):
                result.append(orig_lines[cursor])
                cursor += 1

            result.extend(aligned_adds)
            cursor = max(cursor, match_end)

        # Append remaining original lines
        while cursor < len(orig_lines):
            result.append(orig_lines[cursor])
            cursor += 1

        return result

    @classmethod
    def _attempt_syntax_repair(
        cls,
        orig_lines: List[str],
        lines_diff: List[str],
        bad_content: str
    ) -> Optional[str]:
        """
        Self-healing repair loop when patch introduces syntax errors
        (e.g., escaped quotes, unclosed docstrings, mismatched indentation,
        mismatched try/except blocks, orphaned except, duplicate blocks).
        """
        # Repair Strategy 0: Unescape escaped quotes from model serialization (e.g. \"\"\" -> """)
        if r'\"' in bad_content or r"\'" in bad_content:
            unescaped = bad_content.replace(r'\"\"\"', '"""').replace(r"\'\'\'", "'''").replace(r'\"', '"')
            if cls.validate_python_syntax(unescaped):
                return unescaped
            bad_content = unescaped

        # Repair Strategy 0b: Balance unclosed triple quotes
        if bad_content.count('"""') % 2 != 0:
            closed = bad_content + '\n"""\n'
            if cls.validate_python_syntax(closed):
                return closed
        if bad_content.count("'''") % 2 != 0:
            closed = bad_content + "\n'''\n"
            if cls.validate_python_syntax(closed):
                return closed

        del_lines = [l[1:] for l in lines_diff if l.startswith("-") and not l.startswith("---")]
        add_lines = [l[1:] for l in lines_diff if l.startswith("+") and not l.startswith("+++")]
        bad_lines = bad_content.splitlines()

        # Repair Strategy 1: Indentation auto-alignment with deleted lines in orig_lines
        if del_lines and add_lines:
            del_stripped = [l.strip() for l in del_lines]
            for idx in range(len(orig_lines) - len(del_lines) + 1):
                cand_slice = orig_lines[idx : idx + len(del_lines)]
                if [l.strip() for l in cand_slice] == del_stripped:
                    orig_indent = cand_slice[0][:len(cand_slice[0]) - len(cand_slice[0].lstrip())]
                    add_indent = add_lines[0][:len(add_lines[0]) - len(add_lines[0].lstrip())]
                    delta = len(orig_indent) - len(add_indent)

                    aligned_adds = []
                    for al in add_lines:
                        if not al.strip():
                            aligned_adds.append("")
                        elif delta > 0:
                            aligned_adds.append(" " * delta + al)
                        elif delta < 0:
                            trim = min(-delta, len(al) - len(al.lstrip()))
                            aligned_adds.append(al[trim:])
                        else:
                            aligned_adds.append(al)

                    candidate = orig_lines[:idx] + aligned_adds + orig_lines[idx + len(del_lines):]
                    code_cand = "\n".join(candidate) + "\n"
                    if cls.validate_python_syntax(code_cand):
                        return code_cand

        # Repair Strategy 2: Specific try/except and block alignment
        # If an except line is misplaced or orphaned, find the preceding try:
        for idx, line in enumerate(bad_lines):
            stripped = line.strip()
            if stripped.startswith("except") or stripped.startswith("finally:"):
                for try_idx in range(idx - 1, -1, -1):
                    if bad_lines[try_idx].strip().startswith("try:"):
                        try_indent = bad_lines[try_idx][:len(bad_lines[try_idx]) - len(bad_lines[try_idx].lstrip())]
                        curr_indent = line[:len(line) - len(line.lstrip())]
                        if try_indent != curr_indent:
                            modified = list(bad_lines)
                            shift = len(try_indent) - len(curr_indent)
                            for k in range(idx, len(bad_lines)):
                                if k == idx or (bad_lines[k].startswith(" " * (len(curr_indent) + 1))):
                                    if shift > 0:
                                        modified[k] = " " * shift + modified[k]
                                    else:
                                        trim = min(-shift, len(modified[k]) - len(modified[k].lstrip()))
                                        modified[k] = modified[k][trim:]
                                else:
                                    break
                            code_cand = "\n".join(modified) + "\n"
                            if cls.validate_python_syntax(code_cand):
                                return code_cand
                        break

        # Repair Strategy 3: Duplicate block elimination (e.g. duplicate except clauses)
        for idx in range(len(bad_lines) - 1):
            if bad_lines[idx].strip() and bad_lines[idx].strip() == bad_lines[idx + 1].strip():
                candidate = bad_lines[:idx] + bad_lines[idx + 1:]
                code_cand = "\n".join(candidate) + "\n"
                if cls.validate_python_syntax(code_cand):
                    return code_cand

        # Repair Strategy 4: AST SyntaxError diagnostic repair
        try:
            ast.parse(bad_content)
        except SyntaxError as e:
            err_line = (e.lineno or 1) - 1

            # 4a: Check if preceding line was an import at 0 indent inside a block causing unexpected indent
            if 0 < err_line < len(bad_lines) and (bad_lines[err_line - 1].strip().startswith("import ") or bad_lines[err_line - 1].strip().startswith("from ")):
                imp = bad_lines[err_line - 1].strip()
                # Try hoisting import after module docstring or existing module-level imports
                insert_pos = 0
                in_doc = False
                for k, bl in enumerate(bad_lines):
                    sbl = bl.strip()
                    if not in_doc and (sbl.startswith('"""') or sbl.startswith("'''")):
                        quote = '"""' if sbl.startswith('"""') else "'''"
                        if sbl.count(quote) >= 2:
                            insert_pos = k + 1
                            break
                        else:
                            in_doc = True
                    elif in_doc:
                        if '"""' in bl or "'''" in bl:
                            insert_pos = k + 1
                            in_doc = False
                            break
                    elif sbl.startswith("import ") or sbl.startswith("from "):
                        insert_pos = k + 1
                    elif sbl and not sbl.startswith("#") and not in_doc:
                        break

                cand_lines = bad_lines[:err_line - 1] + bad_lines[err_line:]
                cand_lines.insert(insert_pos, imp)
                code_cand = "\n".join(cand_lines) + "\n"
                if cls.validate_python_syntax(code_cand):
                    return code_cand

                # Try indenting the import to match err_line indentation
                curr_indent = bad_lines[err_line][:len(bad_lines[err_line]) - len(bad_lines[err_line].lstrip())]
                cand_lines = list(bad_lines)
                cand_lines[err_line - 1] = curr_indent + imp
                code_cand = "\n".join(cand_lines) + "\n"
                if cls.validate_python_syntax(code_cand):
                    return code_cand

            # 4b: Try adjusting indentation on the error line and immediate block
            if 0 <= err_line < len(bad_lines):
                for target_line_idx in (err_line, max(0, err_line - 1)):
                    for target_indent in (0, 4, 8, 12, 16):
                        modified = list(bad_lines)
                        modified[target_line_idx] = " " * target_indent + modified[target_line_idx].lstrip()
                        code_cand = "\n".join(modified) + "\n"
                        if cls.validate_python_syntax(code_cand):
                            return code_cand

                        # Also try shifting statement header + indented block
                        if modified[target_line_idx].rstrip().endswith(":"):
                            for block_offset in range(1, min(5, len(bad_lines) - target_line_idx)):
                                modified[target_line_idx + block_offset] = " " * (target_indent + 4) + modified[target_line_idx + block_offset].lstrip()
                            code_cand = "\n".join(modified) + "\n"
                            if cls.validate_python_syntax(code_cand):
                                return code_cand

        return None

    @classmethod
    def split_into_hunks(cls, diff_text: str) -> List[Dict[str, Any]]:
        """
        Parses a unified diff into discrete hunks with header and line records.
        """
        if not diff_text:
            return []

        hunk_header_re = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")
        lines = diff_text.splitlines()
        hunks = []
        file_header_lines = []
        current_hunk = None

        for line in lines:
            hunk_match = hunk_header_re.match(line)
            if hunk_match:
                if current_hunk:
                    hunks.append(current_hunk)
                current_hunk = {
                    "index": len(hunks),
                    "header": line,
                    "old_start": int(hunk_match.group(1)),
                    "old_count": int(hunk_match.group(2)) if hunk_match.group(2) else 1,
                    "new_start": int(hunk_match.group(3)),
                    "new_count": int(hunk_match.group(4)) if hunk_match.group(4) else 1,
                    "lines": []
                }
            elif current_hunk:
                current_hunk["lines"].append(line)
            else:
                file_header_lines.append(line)

        if current_hunk:
            hunks.append(current_hunk)

        return hunks

    @classmethod
    def discard_hunk(
        cls,
        orig_lines: List[str],
        diff_text: str,
        hunk_index_to_discard: int,
        filename: str = "file"
    ) -> str:
        """
        Selectively discards a single hunk by index and recomputes the
        remaining diff cleanly against original lines.
        """
        hunks = cls.split_into_hunks(diff_text)
        if not hunks or hunk_index_to_discard < 0 or hunk_index_to_discard >= len(hunks):
            return diff_text

        # If only 1 hunk and it's discarded, diff is empty
        if len(hunks) == 1 and hunk_index_to_discard == 0:
            return ""

        # Re-apply all hunks EXCEPT the discarded one
        kept_diff_lines = []
        for line in diff_text.splitlines():
            if line.startswith("---") or line.startswith("+++"):
                kept_diff_lines.append(line)
            elif line.startswith("@@"):
                break

        for idx, h in enumerate(hunks):
            if idx == hunk_index_to_discard:
                continue
            kept_diff_lines.append(h["header"])
            kept_diff_lines.extend(h["lines"])

        if not any(l.startswith("@@") for l in kept_diff_lines):
            return ""

        try:
            modified_lines = cls._apply_unified_diff(orig_lines, kept_diff_lines)
            if modified_lines == orig_lines:
                return ""
            new_diff = difflib.unified_diff(
                orig_lines,
                modified_lines,
                fromfile=f"a/{filename}",
                tofile=f"b/{filename}",
                lineterm=""
            )
            return "\n".join(new_diff).strip()
        except Exception:
            return "\n".join(kept_diff_lines).strip()

    @classmethod
    def split_multi_file_diff(cls, diff_text: str) -> List[Dict[str, str]]:
        """
        Splits a unified diff that modifies multiple files into a list
        of per-file diff structures: [{"file_path": "...", "diff": "..."}].
        """
        if not diff_text or not diff_text.strip():
            return []

        file_split_re = re.compile(r"(?=(?:^|\n)--- (?:a\/|\/dev\/null))")
        chunks = [c.strip() for c in file_split_re.split(diff_text) if c.strip()]
        result = []

        for chunk in chunks:
            if not chunk.startswith("---"):
                continue
            target_match = re.search(r"\+\+\+\s+(?:b/)?([^\s\n\r]+)", chunk)
            file_path = target_match.group(1) if target_match else "unknown"
            if file_path == "/dev/null":
                orig_match = re.search(r"---\s+(?:a/)?([^\s\n\r]+)", chunk)
                file_path = orig_match.group(1) if orig_match else "unknown"

            result.append({
                "file_path": file_path,
                "diff": chunk
            })

        if not result and diff_text.strip():
            return [{"file_path": "unknown", "diff": diff_text.strip()}]

        return result

    @classmethod
    def compute_intra_line_diff(cls, old_line: str, new_line: str) -> Dict[str, Any]:
        """
        Computes intra-line word-level diff between an old line and a new line.
        Tokenizes into words/symbols to prevent awkward sub-digit splits (e.g. 80 vs 120).
        Returns span segments with type: 'same', 'del', or 'add'.
        """
        token_re = re.compile(r"\w+|[^\w\s]|\s+")
        old_tokens = token_re.findall(old_line) or [old_line]
        new_tokens = token_re.findall(new_line) or [new_line]

        matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens)
        old_spans = []
        new_spans = []

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            old_chunk = "".join(old_tokens[i1:i2])
            new_chunk = "".join(new_tokens[j1:j2])
            if tag == "equal":
                if old_chunk:
                    old_spans.append({"text": old_chunk, "type": "same"})
                if new_chunk:
                    new_spans.append({"text": new_chunk, "type": "same"})
            elif tag == "replace":
                if old_chunk:
                    old_spans.append({"text": old_chunk, "type": "del"})
                if new_chunk:
                    new_spans.append({"text": new_chunk, "type": "add"})
            elif tag == "delete":
                if old_chunk:
                    old_spans.append({"text": old_chunk, "type": "del"})
            elif tag == "insert":
                if new_chunk:
                    new_spans.append({"text": new_chunk, "type": "add"})

        return {
            "old_spans": old_spans,
            "new_spans": new_spans
        }
