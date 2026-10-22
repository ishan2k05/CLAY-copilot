import os
import re
import ast
import json
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
        # Strip leading file-name comment if orig_lines doesn't start with it
        if snip_lines and re.match(r"^(?:#|//|/\*)\s*[A-Za-z0-9_\-./\\]+\.[a-zA-Z0-9]+", snip_lines[0].strip()):
            if not (orig_lines and orig_lines[0].strip() == snip_lines[0].strip()):
                snip_lines = snip_lines[1:]
                while snip_lines and not snip_lines[0].strip():
                    snip_lines = snip_lines[1:]
        if not snip_lines:
            return None

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
        if len(snip_lines) <= 10:
            target_str = "\n".join(l.strip() for l in snip_lines if l.strip())
            w_len = min(len(snip_lines), len(orig_lines))
            if w_len > 0 and len(orig_lines) >= w_len:
                best_ratio = 0.0
                best_idx = -1
                for cand in range(len(orig_lines) - w_len + 1):
                    cand_str = "\n".join(l.strip() for l in orig_lines[cand : cand + w_len] if l.strip())
                    ratio = difflib.SequenceMatcher(None, target_str, cand_str).ratio()
                    # Also boost if common statement prefix (e.g. print, return, assignment)
                    cand_first = orig_lines[cand].strip()
                    snip_first = snip_lines[0].strip()
                    if cand_first.split("(")[0] == snip_first.split("(")[0] and "(" in cand_first:
                        ratio = max(ratio, 0.60)
                    elif cand_first.split("=")[0].strip() == snip_first.split("=")[0].strip() and "=" in cand_first:
                        ratio = max(ratio, 0.60)

                    if ratio > best_ratio and ratio >= 0.35:
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

        # Case E: For small files (<= 5 lines), replace directly if snippet length matches or is single statement
        if len(orig_lines) <= 5 and len(snip_lines) <= 5:
            if len(orig_lines) == 1:
                return snip_lines
            if preferred_line is not None and 1 <= preferred_line <= len(orig_lines):
                p_idx = preferred_line - 1
                return orig_lines[:p_idx] + snip_lines + orig_lines[p_idx + 1:]

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
            # Target extension to guide fence preference
            target_ext = Path(filename).suffix.lstrip(".").lower()
            ext_lang_map = {
                "py": ("python", "py"),
                "js": ("javascript", "js", "node"),
                "ts": ("typescript", "ts"),
                "html": ("html", "htm"),
                "css": ("css",),
                "json": ("json",),
                "md": ("markdown", "md"),
                "sh": ("bash", "sh", "shell"),
                "sql": ("sql",),
                "cpp": ("cpp", "c++", "c"),
                "c": ("c",),
                "java": ("java",),
                "rs": ("rust", "rs"),
                "go": ("go",)
            }
            preferred_tags = ext_lang_map.get(target_ext, (target_ext,)) if target_ext else ()

            code_lines = []
            matches = list(re.finditer(r"```([a-zA-Z0-9_+-]*)\s*\n([\s\S]*?)```", generated_text, re.IGNORECASE))
            if matches:
                chosen_block = None
                # 1. Match preferred language tag
                for m in matches:
                    tag = (m.group(1) or "").strip().lower()
                    if tag in preferred_tags:
                        chosen_block = m.group(2)
                        break
                # 2. If no direct tag match, pick the largest non-shell code block
                if chosen_block is None:
                    non_shell = [m.group(2) for m in matches if (m.group(1) or "").strip().lower() not in ("bash", "sh", "shell", "cmd", "powershell")]
                    if non_shell:
                        chosen_block = max(non_shell, key=lambda b: len(b.strip().splitlines()))
                    else:
                        chosen_block = max((m.group(2) for m in matches), key=lambda b: len(b.strip().splitlines()))
                if chosen_block:
                    code_lines = chosen_block.strip().splitlines()
            else:
                # No code fence: filter conversational lead-in and sign-off lines
                lead_in_patterns = (
                    "here is", "sure", "certainly", "hope this", "below is", "this code", "to run",
                    "usage:", "output:", "note:", "let me know", "in this implementation",
                    "i have created", "feel free", "as requested", "here's"
                )
                filtered_lines = []
                in_content = False
                for raw_l in generated_text.splitlines():
                    stripped = raw_l.strip()
                    if not stripped:
                        if in_content:
                            filtered_lines.append(raw_l)
                        continue
                    low = stripped.lower()
                    if not in_content and any(low.startswith(p) for p in lead_in_patterns):
                        continue
                    if not in_content and (stripped.endswith(":") and not stripped.startswith(("def ", "class ", "if ", "for ", "while ", "try", "except", "with ", "else:", "elif "))):
                        continue
                    if stripped.startswith("```"):
                        continue
                    if any(low.startswith(p) for p in ("hope this helps", "let me know if", "feel free to", "save this file")):
                        break
                    in_content = True
                    filtered_lines.append(raw_l)

                while filtered_lines and not filtered_lines[-1].strip():
                    filtered_lines.pop()
                code_lines = filtered_lines

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
        diff_match = re.search(r"```(?:diff|patch)\s*\n([\s\S]*?)```", generated_text, re.IGNORECASE)
        if diff_match:
            diff_text = diff_match.group(1).strip()
        elif re.search(r"```[a-zA-Z0-9_+-]*\s*\n([\s\S]*?@@[\s\S]*?)```", generated_text):
            diff_text = re.search(r"```[a-zA-Z0-9_+-]*\s*\n([\s\S]*?@@[\s\S]*?)```", generated_text).group(1).strip()
        else:
            raw_diff = re.search(r"((?:--- (?:a\/|\/dev\/null)[^\n]+\n\+\+\+ (?:b\/|[^\n]+)[^\n]*\n)?@@[\s\S]*?)(?:```|$)", generated_text)
            if raw_diff:
                diff_text = raw_diff.group(1).strip()
            elif re.search(r"^--- (?:a\/|\/dev\/null)[^\n]+\n\+\+\+ (?:b\/|[^\n]+)", generated_text, re.MULTILINE):
                raw_diff2 = re.search(r"(--- (?:a\/|\/dev\/null)[^\n]+\n\+\+\+ (?:b\/|[^\n]+)[\s\S]*?)(?:```|$)", generated_text)
                if raw_diff2:
                    diff_text = raw_diff2.group(1).strip()

        if diff_text:
            try:
                test_applied = PatchEngine._apply_unified_diff(orig_lines, diff_text.splitlines(), preferred_line=preferred_line)
                if test_applied and test_applied != orig_lines:
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
            if any(l.startswith(("+", "-")) and not l.startswith(("+++", "---")) for l in diff_text.splitlines()):
                return diff_text

        # 3. Check for code block (any language: python, js, ts, go, rust, c, html, css, etc.)
        code_body = None
        code_match = re.search(r"```[a-zA-Z0-9_+-]*\s*\n([\s\S]*?)```", generated_text, re.IGNORECASE)
        if code_match:
            code_body = code_match.group(1).strip()
        elif not generated_text.strip().startswith("I'm unable") and not generated_text.strip().startswith("I cannot"):
            raw_code = generated_text.strip()
            prefixes = ("def ", "class ", "import ", "from ", "#", "//", "/*", "--", ";", "@", "#!", "async def ", "function ", "const ", "let ", "var ", "func ", "fn ", "pub ", "interface ", "export ", "type ", "struct ", "package ", "use ", "include ", "<", '"""', "'''", "print", "return ", "console.", "echo ")
            if any(raw_code.startswith(p) for p in prefixes):
                code_body = raw_code
            elif any(kw in raw_code for kw in ("def ", "class ", "import ", "from ", "return ", "function ", "const ", "var ", "let ", "=")):
                code_body = raw_code
            elif len(raw_code.splitlines()) <= 5 and not any(raw_code.lower().startswith(p) for p in ("here is", "sure", "i have", "the following", "note:", "sorry", "i cannot")):
                code_body = raw_code

        if code_body:
            # Check if code_body is actually a diff inside a generic code fence
            if any(l.startswith(("@@", "--- a/", "+++ b/")) for l in code_body.splitlines()):
                try:
                    test_applied = PatchEngine._apply_unified_diff(orig_lines, code_body.splitlines(), preferred_line=preferred_line)
                    if test_applied and test_applied != orig_lines:
                        diff_iter = difflib.unified_diff(
                            orig_lines,
                            test_applied,
                            fromfile=f"a/{filename}" if orig_lines else "/dev/null",
                            tofile=f"b/{filename}",
                            lineterm=""
                        )
                        return "\n".join(diff_iter).strip()
                except Exception:
                    pass

            # 3a. Try surgical snippet splicing first
            spliced = cls.splice_snippet_into_lines(orig_lines, code_body)
            if spliced and spliced != orig_lines:
                diff_iter = difflib.unified_diff(
                    orig_lines,
                    spliced,
                    fromfile=f"a/{filename}" if orig_lines else "/dev/null",
                    tofile=f"b/{filename}",
                    lineterm=""
                )
                return "\n".join(diff_iter).strip()

            # 3b. Fall back to standard difflib against orig_lines
            code_lines = code_body.splitlines()
            diff_iter = difflib.unified_diff(
                orig_lines,
                code_lines,
                fromfile=f"a/{filename}" if orig_lines else "/dev/null",
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
    def is_syntax_supported(cls, filename_or_path: str) -> bool:
        """Returns True if the file extension is supported for syntax validation."""
        ext = Path(filename_or_path).suffix.lower()
        return ext in {
            ".py", ".json",
            ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
            ".rs", ".go",
            ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
            ".java", ".cs", ".css", ".scss", ".less"
        }

    @classmethod
    def _check_bracket_balance(
        cls, code: str, filename: str
    ) -> Tuple[bool, Optional[str], List[str]]:
        """
        Lexical delimiter balance checker for C-style and bracketed languages (TS, JS, Rust, Go, C++, etc.).
        Accurately parses comments (//, /* */) and string literals (", ', `, r#"..."#),
        tracking balance of {}, (), [].
        Returns (is_valid, error_message, list_of_missing_closing_delimiters).
        """
        ext = Path(filename).suffix.lower()
        is_rust = ext == ".rs"

        stack: List[Tuple[str, int, int]] = []

        i = 0
        n = len(code)
        line = 1
        col = 1

        in_line_comment = False
        in_block_comment = False
        in_string: Optional[str] = None
        rust_raw_hashes = 0
        template_depth = 0

        pairs = {"{": "}", "(": ")", "[": "]"}
        closers = {"}", ")", "]"}

        while i < n:
            ch = code[i]

            if ch == "\n":
                line += 1
                col = 1
                if in_line_comment:
                    in_line_comment = False
                i += 1
                continue

            if in_line_comment:
                i += 1
                col += 1
                continue

            if in_block_comment:
                if ch == "*" and i + 1 < n and code[i + 1] == "/":
                    in_block_comment = False
                    i += 2
                    col += 2
                else:
                    i += 1
                    col += 1
                continue

            if in_string == "rust_raw":
                if ch == '"':
                    hashes = 0
                    while i + 1 + hashes < n and code[i + 1 + hashes] == "#":
                        hashes += 1
                    if hashes >= rust_raw_hashes:
                        in_string = None
                        adv = 1 + rust_raw_hashes
                        i += adv
                        col += adv
                        continue
                i += 1
                col += 1
                continue

            if in_string is not None:
                if ch == "\\" and in_string != "`":
                    i += 2
                    col += 2
                    continue
                elif in_string == "`" and ch == "\\":
                    i += 2
                    col += 2
                    continue
                elif in_string == "`" and ch == "$" and i + 1 < n and code[i + 1] == "{":
                    stack.append(("}", line, col))
                    template_depth += 1
                    in_string = None
                    i += 2
                    col += 2
                    continue
                elif ch == in_string:
                    in_string = None
                    i += 1
                    col += 1
                    continue
                else:
                    i += 1
                    col += 1
                    continue

            if ch == "/" and i + 1 < n:
                next_ch = code[i + 1]
                if next_ch == "/":
                    in_line_comment = True
                    i += 2
                    col += 2
                    continue
                elif next_ch == "*":
                    in_block_comment = True
                    i += 2
                    col += 2
                    continue

            if is_rust and ch == "r" and i + 1 < n and (code[i + 1] == '"' or code[i + 1] == "#"):
                h_count = 0
                k = i + 1
                while k < n and code[k] == "#":
                    h_count += 1
                    k += 1
                if k < n and code[k] == '"':
                    in_string = "rust_raw"
                    rust_raw_hashes = h_count
                    adv = (k + 1) - i
                    i += adv
                    col += adv
                    continue

            if ch in ('"', '`'):
                in_string = ch
                i += 1
                col += 1
                continue
            elif ch == "'":
                if is_rust:
                    is_char = False
                    if i + 2 < n and code[i + 2] == "'":
                        is_char = True
                    elif i + 3 < n and code[i + 1] == "\\" and code[i + 3] == "'":
                        is_char = True
                    elif i + 4 < n and code[i + 1] == "\\" and code[i + 4] == "'":
                        is_char = True
                    if is_char:
                        in_string = "'"
                else:
                    in_string = "'"
                i += 1
                col += 1
                continue

            if ch in pairs:
                stack.append((pairs[ch], line, col))
                i += 1
                col += 1
                continue

            if ch in closers:
                if not stack:
                    return False, f"Unmatched '{ch}' at line {line}:{col}", []
                expected, open_line, open_col = stack.pop()
                if ch != expected:
                    return False, f"Mismatched delimiter at line {line}:{col} (expected '{expected}', got '{ch}')", [expected]
                if template_depth > 0 and expected == "}" and not any(s[0] == "}" for s in stack):
                    in_string = "`"
                    template_depth -= 1
                i += 1
                col += 1
                continue

            i += 1
            col += 1

        if in_block_comment:
            return False, "Unclosed block comment (/*) at EOF", []
        if in_string is not None:
            return False, f"Unclosed string literal ({in_string}) at EOF", []
        if stack:
            missing = [s[0] for s in reversed(stack)]
            last_expected, last_line, last_col = stack[-1]
            return False, f"Unclosed block (missing '{last_expected}') opened at line {last_line}:{last_col}", missing

        return True, None, []

    @classmethod
    def validate_syntax_detailed(cls, code: str, filename: str) -> Tuple[bool, Optional[str]]:
        """Validates code syntax across languages and returns (is_valid, error_detail)."""
        ext = Path(filename).suffix.lower()
        if ext == ".py":
            try:
                ast.parse(code)
                return True, None
            except SyntaxError as se:
                return False, f"Python SyntaxError at line {se.lineno}: {se.msg}"
        elif ext == ".json":
            try:
                json.loads(code)
                return True, None
            except json.JSONDecodeError as jde:
                return False, f"JSONDecodeError at line {jde.lineno}, col {jde.colno}: {jde.msg}"
        elif ext in {
            ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
            ".rs", ".go",
            ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
            ".java", ".cs", ".css", ".scss", ".less"
        }:
            valid, err, _ = cls._check_bracket_balance(code, filename)
            return valid, err
        return True, None

    @classmethod
    def validate_syntax(cls, code: str, filename: str) -> bool:
        """Verifies if the code is syntactically valid for its language."""
        valid, _ = cls.validate_syntax_detailed(code, filename)
        return valid

    @classmethod
    def get_syntax_error_detail(cls, code: str, filename: str) -> str:
        """Returns descriptive error details for rejected or broken syntax."""
        _, err = cls.validate_syntax_detailed(code, filename)
        return err or "Syntax validation failed."

    GENERIC_PLACEHOLDERS = {
        "file", "unknown", "filename", "patch", "diff", "solution.py",
        "patch.diff", "code", "script", "untitled", "path/to/file",
        "new_file", "test", "target", "example"
    }

    @classmethod
    def resolve_target_path(cls, file_path: str, workspace_path: Optional[str] = None) -> Path:
        """Resolves target path against workspace if relative."""
        clean_name = file_path.strip()
        if clean_name.startswith("b/") or clean_name.startswith("a/"):
            clean_name = clean_name[2:]
        clean_name = clean_name.lstrip("/")

        p_raw = Path(clean_name)
        is_generic = (
            clean_name.lower() in cls.GENERIC_PLACEHOLDERS
            or p_raw.name.lower() in cls.GENERIC_PLACEHOLDERS
            or (not p_raw.suffix and not p_raw.exists() and not (workspace_path and (Path(workspace_path) / p_raw).exists()))
        )

        if workspace_path:
            from ..retrieval.file_resolver import WorkspaceFileResolver
            ws = WorkspaceFileResolver._resolve_root(workspace_path)

            if not is_generic:
                p = Path(clean_name)
                if p.is_absolute():
                    if p.exists() or p.parent.exists():
                        return p.resolve()
                    try:
                        p.resolve().relative_to(ws.resolve())
                        return p.resolve()
                    except ValueError:
                        p = Path(p.name)

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

                p_clean = Path(clean_name)
                if p_clean.exists():
                    return p_clean.resolve()
                p_orig = Path(file_path)
                if p_orig.exists():
                    return p_orig.resolve()
                return (ws / p).resolve()

            # Map generic placeholder or unlocated extensionless target to actual repo file
            if ws.exists() and ws.is_dir():
                accessible = WorkspaceFileResolver.list_accessible_files(str(ws))
                py_files = [f for f in accessible.values() if f.suffix == ".py"]
                if py_files:
                    return py_files[0].resolve()
                src_files = [f for f in accessible.values() if f.suffix in (".ts", ".js", ".jsx", ".tsx", ".go", ".rs", ".java", ".cpp", ".c", ".html", ".css")]
                if src_files:
                    return src_files[0].resolve()
                if accessible:
                    return list(accessible.values())[0].resolve()

            return (ws / clean_name).resolve()

        p = Path(clean_name)
        if not is_generic and p.exists():
            return p.resolve()

        return (Path.cwd() / p).resolve()

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
            if (diff_file and diff_file != "/dev/null" 
                    and diff_file.lower() not in cls.GENERIC_PLACEHOLDERS 
                    and Path(diff_file).name.lower() not in cls.GENERIC_PLACEHOLDERS):
                diff_file_path = cls.resolve_target_path(diff_file, workspace_path)
                if diff_file_path.exists() or not target_path.exists():
                    target_path = diff_file_path

        # Guard against writing to an unresolvable generic placeholder without extension
        if (target_path.name.lower() in cls.GENERIC_PLACEHOLDERS 
                or (not target_path.suffix and not target_path.exists())):
            return {
                "status": "error",
                "message": f"Target file '{target_path.name}' is an unrecognized generic placeholder. Refusing to create an unassociated extensionless file. Please select or open an existing source file in the editor."
            }

        # Check if this is a new file creation or populating an empty file
        file_exists = target_path.exists() and target_path.is_file()
        is_empty = False
        if file_exists:
            try:
                is_empty = target_path.stat().st_size == 0
            except Exception:
                pass

        # Only treat as a new file if the file truly does not exist or has 0 bytes.
        is_new_file = (not file_exists or is_empty)

        lines_diff = diff_text.splitlines()
        orig_lines: List[str] = []
        original_content = ""

        if is_new_file:
            new_lines = []
            has_diff_headers = any(line.startswith("+++") or line.startswith("@@") for line in lines_diff)

            if has_diff_headers:
                for line in lines_diff:
                    if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
                        continue
                    if line.startswith("+"):
                        content_line = line[1:]
                        if content_line.startswith(("@@", "---", "+++")):
                            continue
                        if content_line.startswith("+"):
                            new_lines.append(content_line[1:])
                        elif content_line.startswith("-"):
                            continue
                        else:
                            new_lines.append(content_line)
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
                orig_lines = original_content.splitlines()
            except Exception as e:
                return {"status": "error", "message": f"Failed reading file {target_path}: {e}"}

            new_lines = cls._apply_unified_diff(orig_lines, lines_diff)
            new_content = "\n".join(new_lines)
            if not new_content.endswith("\n"):
                new_content += "\n"

        # Multi-language syntax validation and self-healing
        if cls.is_syntax_supported(target_path.name):
            orig_is_valid = cls.validate_syntax(original_content, target_path.name) if not is_new_file else True
            if not cls.validate_syntax(new_content, target_path.name):
                if not is_new_file and orig_lines:
                    repaired = cls._attempt_syntax_repair(orig_lines, lines_diff, new_content, filename=target_path.name)
                    if repaired and cls.validate_syntax(repaired, target_path.name):
                        new_content = repaired
                    elif orig_is_valid:
                        err_detail = cls.get_syntax_error_detail(new_content, target_path.name)
                        return {
                            "status": "error",
                            "message": f"Patch rejected: Introduced syntax error in {target_path.name}. {err_detail}".strip()
                        }
                elif not orig_is_valid:
                    pass
                else:
                    err_detail = cls.get_syntax_error_detail(new_content, target_path.name)
                    return {
                        "status": "error",
                        "message": f"Patch rejected: Introduced syntax error in {target_path.name}. {err_detail}".strip()
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

        # If language is not supported for syntax checking, basic check is sufficient
        if not cls.is_syntax_supported(filename):
            return True, diff_text, None

        # Check syntax across supported languages
        if cls.validate_syntax(patched_content, filename):
            return True, diff_text, None

        # Syntax error detected - run self-healing repair loop
        repaired = cls._attempt_syntax_repair(orig_lines, diff_text.splitlines(), patched_content, filename=filename)
        if repaired and cls.validate_syntax(repaired, filename):
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
        err_msg = cls.get_syntax_error_detail(patched_content, filename)
        return False, diff_text, err_msg

    @classmethod
    def _split_hunk_lines_into_subhunks(
        cls, hunk_lines: List[str], base_old_start: Optional[int] = None
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
        curr_start = preferred_line or None

        for line in diff_lines:
            match = hunk_header_re.match(line)
            if match and line.startswith("@@"):
                if curr_hunk_lines:
                    all_subhunks.extend(cls._split_hunk_lines_into_subhunks(curr_hunk_lines, curr_start))
                curr_start = int(match.group(1)) if match.group(1) else (preferred_line or None)
                curr_hunk_lines = []
            elif curr_start is not None or not all_subhunks:
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
            suggested_idx = cursor if hunk.get("old_start") is None else max(cursor, hunk["old_start"] - 1)

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

            # Strategy 3c: Leading line anchor search from cursor forward
            if match_start == -1 and del_lines:
                first_del = next((l.strip() for l in del_lines if l.strip()), None)
                if first_del:
                    for cand in range(cursor, len(orig_lines)):
                        if orig_lines[cand].strip() == first_del:
                            matched_count = 0
                            while (cand + matched_count < len(orig_lines) and 
                                   matched_count < len(del_lines)):
                                orig_trimmed = orig_lines[cand + matched_count].strip()
                                del_trimmed = del_lines[matched_count].strip()
                                if not orig_trimmed and not del_trimmed:
                                    matched_count += 1
                                elif orig_trimmed == del_trimmed:
                                    matched_count += 1
                                else:
                                    break
                            if matched_count >= 1:
                                match_start = cand
                                match_end = cand + matched_count
                                target_line = orig_lines[match_start]
                                matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]
                                diff_base_indent = del_lines[0][:len(del_lines[0]) - len(del_lines[0].lstrip())]
                                break

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
                if add_lines:
                    match_start = min(len(orig_lines), suggested_idx)
                    match_end = match_start
                    if match_start < len(orig_lines):
                        target_line = orig_lines[match_start]
                        matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]
                else:
                    # Pure deletion that could not be matched anywhere.
                    # CRITICAL SAFETY: Never delete arbitrary lines from innocent code!
                    continue
                if match_start < len(orig_lines):
                    target_line = orig_lines[match_start]
                    matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]

            # Adjust added lines indentation to preserve Python block scoping
            if del_lines:
                diff_base_indent = del_lines[0][:len(del_lines[0]) - len(del_lines[0].lstrip())]
                indent_delta = len(matched_indent) - len(diff_base_indent)
            elif add_lines:
                # Context-relative indentation synchronization:
                # Align added lines relative to matched surrounding context rather than
                # falsely equating added clause keywords (e.g. except:) to inner block statements.
                ctx_delta: Optional[int] = None
                if prefix_ctx:
                    non_empty_prefs = [(idx, l) for idx, l in enumerate(prefix_ctx) if l.strip()]
                    if non_empty_prefs:
                        p_rel_idx, pref_diff = non_empty_prefs[-1]
                        orig_p_idx = match_start - len(prefix_ctx) + p_rel_idx
                        if 0 <= orig_p_idx < len(orig_lines) and orig_lines[orig_p_idx].strip() == pref_diff.strip():
                            diff_ctx_indent = len(pref_diff) - len(pref_diff.lstrip())
                            orig_ctx_indent = len(orig_lines[orig_p_idx]) - len(orig_lines[orig_p_idx].lstrip())
                            ctx_delta = orig_ctx_indent - diff_ctx_indent

                if ctx_delta is None and suffix_ctx:
                    non_empty_suffs = [(idx, l) for idx, l in enumerate(suffix_ctx) if l.strip()]
                    if non_empty_suffs:
                        s_rel_idx, suff_diff = non_empty_suffs[0]
                        orig_s_idx = match_end + s_rel_idx
                        if 0 <= orig_s_idx < len(orig_lines) and orig_lines[orig_s_idx].strip() == suff_diff.strip():
                            diff_ctx_indent = len(suff_diff) - len(suff_diff.lstrip())
                            orig_ctx_indent = len(orig_lines[orig_s_idx]) - len(orig_lines[orig_s_idx].lstrip())
                            ctx_delta = orig_ctx_indent - diff_ctx_indent

                if ctx_delta is not None:
                    indent_delta = ctx_delta
                elif matched_indent:
                    first_add = next((al for al in add_lines if al.strip()), "")
                    diff_base_indent = first_add[:len(first_add) - len(first_add.lstrip())]
                    indent_delta = len(matched_indent) - len(diff_base_indent)
                else:
                    indent_delta = 0
            else:
                indent_delta = 0
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
        bad_content: str,
        filename: str = ""
    ) -> Optional[str]:
        """
        Self-healing repair loop when patch introduces syntax errors across all supported languages
        (e.g., escaped quotes, unclosed docstrings, mismatched indentation,
        missing braces in TS/Rust/Go, trailing commas in JSON, mismatched try/except blocks).
        """
        def _is_valid(cand: str) -> bool:
            return cls.validate_syntax(cand, filename) if filename else cls.validate_python_syntax(cand)

        ext = Path(filename).suffix.lower() if filename else ".py"
        is_python = ext == ".py" or not filename
        is_json = ext == ".json"
        is_bracket_lang = ext in {
            ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
            ".rs", ".go",
            ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
            ".java", ".cs", ".css", ".scss", ".less"
        }

        # Repair Strategy 0: Unescape escaped quotes from model serialization (e.g. \"\"\" -> """)
        if r'\"' in bad_content or r"\'" in bad_content:
            unescaped = bad_content.replace(r'\"\"\"', '"""').replace(r"\'\'\'", "'''").replace(r'\"', '"')
            if _is_valid(unescaped):
                return unescaped
            bad_content = unescaped

        # Repair Strategy 0b: Balance unclosed triple quotes (Python) or backticks (JS/TS)
        if is_python:
            if bad_content.count('"""') % 2 != 0:
                closed = bad_content + '\n"""\n'
                if _is_valid(closed):
                    return closed
            if bad_content.count("'''") % 2 != 0:
                closed = bad_content + "\n'''\n"
                if _is_valid(closed):
                    return closed
        elif ext in {".ts", ".tsx", ".js", ".jsx"}:
            if bad_content.count("`") % 2 != 0:
                closed = bad_content.rstrip() + "`;\n"
                if _is_valid(closed):
                    return closed

        # Repair Strategy JSON: JSON specific repairs
        if is_json:
            # Trailing comma removal before } or ]
            cleaned = re.sub(r',\s*([}\]])', r'\1', bad_content)
            if _is_valid(cleaned):
                return cleaned
            # Balance unclosed brackets at EOF
            open_c = cleaned.count('{') - cleaned.count('}')
            open_b = cleaned.count('[') - cleaned.count(']')
            cand = cleaned.rstrip()
            for _ in range(open_b):
                cand += "\n]"
            for _ in range(open_c):
                cand += "\n}"
            cand += "\n"
            if _is_valid(cand):
                return cand
            # Quote unquoted property keys: e.g. { foo: "bar" } -> { "foo": "bar" }
            quoted_keys = re.sub(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', cleaned)
            if _is_valid(quoted_keys):
                return quoted_keys

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
                    if _is_valid(code_cand):
                        return code_cand

        # Repair Strategy Non-Python Delimiter Balance: (TS, JS, Rust, Go, C++, etc.)
        if is_bracket_lang:
            valid_b, err_msg, missing_stack = cls._check_bracket_balance(bad_content, filename)
            if not valid_b:
                if missing_stack:
                    # Candidate A: Append missing closers at the end of file
                    cand_tail = bad_content.rstrip() + "\n" + "\n".join(missing_stack) + "\n"
                    if _is_valid(cand_tail):
                        return cand_tail

                    # Candidate B: Insert missing closer right after the added block
                    if add_lines:
                        last_add = add_lines[-1].strip()
                        for idx in range(len(bad_lines) - 1, -1, -1):
                            if bad_lines[idx].strip() == last_add:
                                cand_lines = bad_lines[:idx + 1] + missing_stack + bad_lines[idx + 1:]
                                cand = "\n".join(cand_lines) + "\n"
                                if _is_valid(cand):
                                    return cand
                                break

                    # Candidate C: Insert missing closers before subsequent top-level declarations
                    top_level_prefixes = (
                        "function ", "export ", "class ", "interface ", "type ",
                        "fn ", "pub fn ", "pub ", "impl ", "struct ", "enum "
                    )
                    for idx, line in enumerate(bad_lines):
                        stripped = line.lstrip()
                        if any(stripped.startswith(p) for p in top_level_prefixes) and idx > 0:
                            cand_lines = bad_lines[:idx] + missing_stack + bad_lines[idx:]
                            cand = "\n".join(cand_lines) + "\n"
                            if _is_valid(cand):
                                return cand

                # Candidate D: If unexpected closer reported, remove orphaned closer line
                if err_msg and "Unmatched" in err_msg:
                    line_m = re.search(r'line (\d+)', err_msg)
                    if line_m:
                        u_line = int(line_m.group(1)) - 1
                        if 0 <= u_line < len(bad_lines):
                            if bad_lines[u_line].strip() in ("}", ")", "]", "};", ");"):
                                cand_lines = bad_lines[:u_line] + bad_lines[u_line + 1:]
                                cand = "\n".join(cand_lines) + "\n"
                                if _is_valid(cand):
                                    return cand

        # Python-specific AST Diagnostic Repair Strategies
        if is_python:
            # Repair Strategy 2: Multi-clause try/except and finally block alignment
            # Aligns ALL sibling except/finally clauses and their bodies to match their parent try: block.
            for try_idx, t_line in enumerate(bad_lines):
                if t_line.strip().startswith("try:"):
                    try_indent = t_line[:len(t_line) - len(t_line.lstrip())]
                    modified = list(bad_lines)
                    changed_any = False
                    k = try_idx + 1

                    while k < len(modified):
                        k_line = modified[k]
                        k_stripped = k_line.strip()
                        if not k_stripped:
                            k += 1
                            continue

                        k_indent = k_line[:len(k_line) - len(k_line.lstrip())]

                        if k_stripped.startswith(("except", "finally:")):
                            clause_shift = len(try_indent) - len(k_indent)
                            if clause_shift != 0:
                                changed_any = True
                                if clause_shift > 0:
                                    modified[k] = " " * clause_shift + modified[k]
                                else:
                                    trim = min(-clause_shift, len(modified[k]) - len(modified[k].lstrip()))
                                    modified[k] = modified[k][trim:]

                            # Shift all body lines under this except/finally clause
                            b_idx = k + 1
                            while b_idx < len(modified):
                                b_line = modified[b_idx]
                                b_stripped = b_line.strip()
                                if not b_stripped:
                                    b_idx += 1
                                    continue
                                b_indent = b_line[:len(b_line) - len(b_line.lstrip())]
                                if b_stripped.startswith(("except", "finally:")) or len(b_indent) <= len(k_indent):
                                    break
                                if clause_shift != 0:
                                    if clause_shift > 0:
                                        modified[b_idx] = " " * clause_shift + modified[b_idx]
                                    else:
                                        trim = min(-clause_shift, len(modified[b_idx]) - len(modified[b_idx].lstrip()))
                                        modified[b_idx] = modified[b_idx][trim:]
                                b_idx += 1
                            k = b_idx
                        elif len(k_indent) <= len(try_indent) and not k_stripped.startswith(("#", '"""', "'''")):
                            break
                        else:
                            k += 1

                    if changed_any:
                        code_cand = "\n".join(modified) + "\n"
                        if _is_valid(code_cand):
                            return code_cand

            # Repair Strategy 3: Duplicate block elimination
            for idx in range(len(bad_lines) - 1):
                if bad_lines[idx].strip() and bad_lines[idx].strip() == bad_lines[idx + 1].strip():
                    candidate = bad_lines[:idx] + bad_lines[idx + 1:]
                    code_cand = "\n".join(candidate) + "\n"
                    if _is_valid(code_cand):
                        return code_cand

            # Repair Strategy 4: AST SyntaxError diagnostic repair
            try:
                ast.parse(bad_content)
            except SyntaxError as e:
                err_line = (e.lineno or 1) - 1

                # 4a: Check if preceding line was an import at 0 indent inside a block causing unexpected indent
                if 0 < err_line < len(bad_lines) and (bad_lines[err_line - 1].strip().startswith("import ") or bad_lines[err_line - 1].strip().startswith("from ")):
                    imp = bad_lines[err_line - 1].strip()
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
                    if _is_valid(code_cand):
                        return code_cand

                    curr_indent = bad_lines[err_line][:len(bad_lines[err_line]) - len(bad_lines[err_line].lstrip())]
                    cand_lines = list(bad_lines)
                    cand_lines[err_line - 1] = curr_indent + imp
                    code_cand = "\n".join(cand_lines) + "\n"
                    if _is_valid(code_cand):
                        return code_cand

                # 4b: Try adjusting indentation on the error line and immediate block
                if 0 <= err_line < len(bad_lines):
                    for target_line_idx in (err_line, max(0, err_line - 1)):
                        for target_indent in (0, 4, 8, 12, 16):
                            modified = list(bad_lines)
                            modified[target_line_idx] = " " * target_indent + modified[target_line_idx].lstrip()
                            code_cand = "\n".join(modified) + "\n"
                            if _is_valid(code_cand):
                                return code_cand

                            if modified[target_line_idx].rstrip().endswith(":"):
                                for block_offset in range(1, min(5, len(bad_lines) - target_line_idx)):
                                    modified[target_line_idx + block_offset] = " " * (target_indent + 4) + modified[target_line_idx + block_offset].lstrip()
                                code_cand = "\n".join(modified) + "\n"
                                if _is_valid(code_cand):
                                    return code_cand

                # 4c: Check for orphaned indented block
                if 0 <= err_line < len(bad_lines):
                    line_text = bad_lines[err_line]
                    if line_text and line_text[:1].isspace():
                        block_lines = list(bad_lines)
                        orig_err_indent = len(line_text) - len(line_text.lstrip())
                        for b_idx in range(err_line, len(block_lines)):
                            bl = block_lines[b_idx]
                            if not bl.strip():
                                continue
                            bl_indent = len(bl) - len(bl.lstrip())
                            if bl_indent >= orig_err_indent:
                                block_lines[b_idx] = bl[orig_err_indent:]
                            else:
                                break
                        code_cand = "\n".join(block_lines) + "\n"
                        if _is_valid(code_cand):
                            return code_cand

        return None

    @classmethod
    def split_into_hunks(cls, diff_text: str) -> List[Dict[str, Any]]:
        """
        Parses a unified diff into discrete hunks with header and line records.
        """
        if not diff_text:
            return []

        hunk_header_re = re.compile(r"^@@\s*(?:-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?)?\s*@@?")
        lines = diff_text.splitlines()
        hunks = []
        file_header_lines = []
        current_hunk = None

        for line in lines:
            hunk_match = hunk_header_re.match(line)
            if hunk_match and line.startswith("@@"):
                if current_hunk:
                    hunks.append(current_hunk)
                current_hunk = {
                    "index": len(hunks),
                    "header": line,
                    "old_start": int(hunk_match.group(1)) if hunk_match.group(1) else 1,
                    "old_count": int(hunk_match.group(2)) if hunk_match.group(2) else 1,
                    "new_start": int(hunk_match.group(3)) if hunk_match.group(3) else 1,
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
            file_path = target_match.group(1).strip() if target_match else ""
            if file_path == "/dev/null" or not file_path:
                orig_match = re.search(r"---\s+(?:a/)?([^\s\n\r]+)", chunk)
                file_path = orig_match.group(1).strip() if orig_match else ""

            if file_path.lower() in cls.GENERIC_PLACEHOLDERS:
                file_path = ""

            result.append({
                "file_path": file_path,
                "diff": chunk
            })

        if not result and diff_text.strip():
            header_match = re.search(r"\+\+\+\s+(?:b/)?([^\s\n\r]+)", diff_text)
            extracted = header_match.group(1).strip() if header_match else ""
            if extracted.lower() in cls.GENERIC_PLACEHOLDERS or extracted == "/dev/null":
                extracted = ""
            return [{"file_path": extracted, "diff": diff_text.strip()}]

        return result

    @classmethod
    def build_multi_surgical_diff(
        cls,
        file_orig_lines_map: Dict[str, List[str]],
        generated_text: str,
        default_filename: str = "",
        allow_multi_file: bool = True,
        authorized_files: Optional[List[str]] = None,
        preferred_line: Optional[int] = None
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Coordinates surgical diff generation across one or more files based on scope authorization.
        
        If allow_multi_file is False:
            Strictly restricts diff generation to default_filename. If the model emitted changes
            for other files, they are pruned to ensure single-file scope safety.
            
        If allow_multi_file is True:
            Detects all file targets from unified diff headers, SEARCH/REPLACE blocks with File: markers,
            or file-tagged code blocks. Validates each file against its corresponding orig_lines.
            
        Returns:
            (combined_diff_text, list_of_staged_files)
        """
        clean_gen = (generated_text or "").strip()
        if not clean_gen:
            return "", []

        auth_set = set()
        if authorized_files:
            for af in authorized_files:
                auth_set.add(af.replace("\\", "/").lower())
                auth_set.add(Path(af).name.lower())

        def is_file_authorized(fpath: str) -> bool:
            if not fpath:
                return False
            clean_name = Path(fpath.replace("\\", "/")).name.lower()
            if not clean_name or clean_name in (".", "..", "file", "filename", "unknown", "none"):
                return False
            # Must look like a valid source or config filename
            if "." not in clean_name:
                return False
            return True

        # 1. Try unified diff multi-file split
        split_chunks = cls.split_multi_file_diff(clean_gen)
        valid_chunks = [c for c in split_chunks if c.get("diff") and (c.get("diff").startswith("---") or "@@" in c.get("diff"))]

        if valid_chunks and (len(valid_chunks) > 1 or (valid_chunks[0].get("file_path") and valid_chunks[0].get("file_path") != default_filename)):
            filtered_chunks = []
            for chunk in valid_chunks:
                fpath = chunk.get("file_path") or default_filename
                if is_file_authorized(fpath):
                    filtered_chunks.append(chunk)

            if filtered_chunks:
                staged = []
                for chunk in filtered_chunks:
                    fpath = chunk.get("file_path") or default_filename
                    cdiff = chunk.get("diff", "")
                    orig = file_orig_lines_map.get(fpath)
                    if orig is None:
                        orig = file_orig_lines_map.get(Path(fpath).name, [])

                    valid, healed_diff, _ = cls.validate_and_heal_diff(orig, cdiff, fpath)
                    final_diff = healed_diff if (valid and healed_diff) else cdiff

                    adds = sum(1 for l in final_diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
                    dels = sum(1 for l in final_diff.splitlines() if l.startswith("-") and not l.startswith("---"))

                    staged.append({
                        "file_path": fpath,
                        "diff": final_diff,
                        "valid": valid,
                        "adds": adds,
                        "dels": dels,
                        "applied": False
                    })

                combined = "\n\n".join(s["diff"] for s in staged if s.get("diff"))
                return combined, staged

        # 2. Extract multiple markdown code blocks with file indicators
        code_block_pattern = re.compile(r"```([a-zA-Z0-9_\-\.:/]+)?\s*\n([\s\S]*?)```")
        matches = list(code_block_pattern.finditer(clean_gen))
        if matches:
            code_sections: List[Dict[str, str]] = []
            seen_files = set()
            for m in matches:
                header_info = (m.group(1) or "").strip()
                header_clean = header_info.lower().strip()
                header_first_word = re.split(r"[:\s]+", header_clean)[0] if header_clean else ""
                ignored_langs = {
                    "mermaid", "json", "bash", "sh", "terminal", "output", "text",
                    "powershell", "ps1", "cmd", "bat", "shell", "zsh", "console", "dos"
                }
                if not block_content or header_clean in ignored_langs or (header_first_word in ignored_langs and "." not in header_clean):
                    continue

                found_filename = None
                # Check header e.g. ```python:calc.py, ```python calc.py, or ```calc.py
                if header_info:
                    parts = re.split(r"[:\s]+", header_info)
                    for p in parts:
                        p_cand = p.strip().strip("`'\"")
                        if "." in p_cand and not p_cand.startswith(".") and not re.match(r"^\d", p_cand):
                            found_filename = p_cand
                            break

                # Check first 5 non-empty lines of block content: e.g. # calc.py, // app.py, or File: calc.py
                if not found_filename and block_content:
                    non_empty = [l.strip() for l in block_content.splitlines() if l.strip()]
                    for check_line in non_empty[:5]:
                        fn_match = re.search(r"^(?:#|\/\/|\/\*|<!--|--|;)\s*(?:File:?|filepath:?|path:?|filename:?)?\s*([A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+)", check_line, re.IGNORECASE)
                        if fn_match:
                            cand = fn_match.group(1).strip()
                            if "." in cand and not cand.startswith("."):
                                found_filename = cand
                                break

                # Check preceding text before the code block (up to 300 chars back)
                if not found_filename:
                    pre_text = clean_gen[max(0, m.start() - 300):m.start()].strip()
                    pre_lines = [l.strip() for l in pre_text.splitlines() if l.strip()]
                    if pre_lines:
                        last_line = pre_lines[-1]
                        pre_fn_match = re.search(r"(?:###|\*\*|File:?|Target:?|Path:?|In|Update|Create)?\s*[`'\"]?([A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+)[`'\"]?", last_line, re.IGNORECASE)
                        if pre_fn_match:
                            cand = pre_fn_match.group(1).strip()
                            if "." in cand and not cand.startswith("."):
                                found_filename = cand

                # Positional fallback to authorized_files if model omitted filename header on a block
                if not found_filename and authorized_files:
                    remaining_auth = [af for af in authorized_files if af.replace("\\", "/") not in seen_files and Path(af).name not in seen_files]
                    if remaining_auth:
                        found_filename = remaining_auth[0]

                if found_filename and is_file_authorized(found_filename):
                    norm_fn = found_filename.replace("\\", "/")
                    if norm_fn not in seen_files:
                        seen_files.add(norm_fn)
                        code_sections.append({
                            "file_path": found_filename,
                            "content": block_content
                        })

            if len(code_sections) >= 2 or (len(code_sections) == 1 and default_filename and Path(code_sections[0]["file_path"]).name.lower() != Path(default_filename).name.lower()):
                staged = []
                for sec in code_sections:
                    fpath = sec["file_path"]
                    s_content = sec["content"]
                    orig = file_orig_lines_map.get(fpath)
                    if orig is None:
                        orig = file_orig_lines_map.get(Path(fpath).name, [])

                    wrapped_content = s_content if s_content.startswith("```") else f"```{Path(fpath).suffix.lstrip('.') or 'python'}\n{s_content}\n```"
                    file_diff = cls.build_surgical_diff(orig, wrapped_content, fpath, preferred_line=preferred_line)
                    valid, healed_diff, _ = cls.validate_and_heal_diff(orig, file_diff, fpath)
                    final_diff = healed_diff if (valid and healed_diff) else file_diff

                    adds = sum(1 for l in final_diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
                    dels = sum(1 for l in final_diff.splitlines() if l.startswith("-") and not l.startswith("---"))

                    staged.append({
                        "file_path": fpath,
                        "diff": final_diff,
                        "valid": valid,
                        "adds": adds,
                        "dels": dels,
                        "applied": False
                    })

                if staged:
                    combined = "\n\n".join(s["diff"] for s in staged if s.get("diff"))
                    return combined, staged

        # 3. Check for File: or comment-delimited sections in raw text
        file_delim_re = re.compile(
            r"(?:^|\n)(?:###\s*(?:File|Target|Path):?|File:?|//\s*(?:File|filepath):?|#\s*(?:File|filepath)?:?|<!--\s*File:?)\s*([A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+)(?:\s*-->)?",
            re.IGNORECASE
        )
        splits = list(file_delim_re.finditer(clean_gen))
        if len(splits) >= 2:
            staged = []
            for i in range(len(splits)):
                match_file = splits[i].group(1).strip()
                start_pos = splits[i].end()
                end_pos = splits[i + 1].start() if i + 1 < len(splits) else len(clean_gen)
                section_text = clean_gen[start_pos:end_pos].strip()

                if not is_file_authorized(match_file):
                    continue

                orig = file_orig_lines_map.get(match_file) or file_orig_lines_map.get(Path(match_file).name, [])
                wrapped_sec = section_text if section_text.startswith("```") else f"```{Path(match_file).suffix.lstrip('.') or 'python'}\n{section_text}\n```"
                file_diff = cls.build_surgical_diff(orig, wrapped_sec, match_file, preferred_line=preferred_line)
                valid, healed_diff, _ = cls.validate_and_heal_diff(orig, file_diff, match_file)
                final_diff = healed_diff if (valid and healed_diff) else file_diff

                adds = sum(1 for l in final_diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
                dels = sum(1 for l in final_diff.splitlines() if l.startswith("-") and not l.startswith("---"))

                staged.append({
                    "file_path": match_file,
                    "diff": final_diff,
                    "valid": valid,
                    "adds": adds,
                    "dels": dels,
                    "applied": False
                })

            if staged:
                combined = "\n\n".join(s["diff"] for s in staged if s.get("diff"))
                return combined, staged

        # 4. Fallback: single file diff against default_filename
        target_f = default_filename or (authorized_files[0] if authorized_files else "solution.py")
        orig = file_orig_lines_map.get(target_f) or file_orig_lines_map.get(Path(target_f).name, [])
        single_diff = cls.build_surgical_diff(orig, clean_gen, target_f, preferred_line=preferred_line)
        valid, healed_diff, _ = cls.validate_and_heal_diff(orig, single_diff, target_f)
        final_diff = healed_diff if (valid and healed_diff) else single_diff

        adds = sum(1 for l in final_diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        dels = sum(1 for l in final_diff.splitlines() if l.startswith("-") and not l.startswith("---"))

        staged_item = {
            "file_path": target_f,
            "diff": final_diff,
            "valid": valid,
            "adds": adds,
            "dels": dels,
            "applied": False
        }
        return final_diff, [staged_item]

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

    @classmethod
    def rebase_staged_diff(
        cls,
        file_path: str,
        base_content: str,
        diff_text: str,
        current_content: str,
        workspace_path: Optional[str] = None,
        force_resolution: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Executes a 3-way merge rebase:
        Base (O): File state when diff was generated.
        Agent (A): Proposed patched file (O + Agent Diff).
        Current (B): Current active editor buffer state.

        If user edits and agent diff hunks are disjoint:
          Re-anchors hunk headers against current buffer and emits clean shifted diff.
        If user edits collide with agent diff hunks:
          Pinpoints collision spans and returns conflict markers for user resolution.
          If force_resolution == 'agent_wins', allows agent hunks to overwrite collisions.
        """
        target_path = cls.resolve_target_path(file_path, workspace_path)

        # Fallback for base_content if client snapshot was empty but file exists on disk
        if (not base_content or not base_content.strip()) and target_path.exists() and target_path.is_file():
            try:
                base_content = target_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                pass

        # Normalize line endings to avoid CRLF desync
        base_content = (base_content or "").replace("\r\n", "\n").replace("\r", "\n")
        current_content = (current_content or "").replace("\r\n", "\n").replace("\r", "\n")

        orig_lines = base_content.splitlines()
        curr_lines = current_content.splitlines()
        lines_diff = diff_text.splitlines()

        filename = Path(file_path).name

        # If current editor buffer is identical to base snapshot, diff is fully valid as-is
        if curr_lines == orig_lines and not force_resolution:
            return {
                "status": "clean",
                "diff": diff_text,
                "has_collision": False,
                "conflicts": [],
                "message": "Buffer is in sync with base snapshot."
            }

        # Apply diff to base lines to get full proposed Agent version A
        try:
            agent_lines = cls._apply_unified_diff(orig_lines, lines_diff)
        except Exception as e:
            agent_lines = [l[1:] for l in lines_diff if l.startswith("+") and not l.startswith("+++")]
            if not agent_lines:
                return {
                    "status": "error",
                    "diff": diff_text,
                    "has_collision": False,
                    "conflicts": [],
                    "message": f"Could not apply base diff to original lines: {e}"
                }

        # Check if the user's current editor already matches the agent's target state
        if curr_lines == agent_lines:
            return {
                "status": "already_applied",
                "diff": "",
                "has_collision": False,
                "conflicts": [],
                "message": "Changes already incorporated into active editor buffer."
            }

        # Compute opcodes between Base (O) and Agent (A)
        matcher_oa = difflib.SequenceMatcher(None, orig_lines, agent_lines)
        agent_ops = [
            (i1, i2, agent_lines[j1:j2], "agent")
            for tag, i1, i2, j1, j2 in matcher_oa.get_opcodes()
            if tag != "equal"
        ]

        # Compute opcodes between Base (O) and Current Editor (B)
        matcher_ob = difflib.SequenceMatcher(None, orig_lines, curr_lines)
        user_ops = [
            (i1, i2, curr_lines[j1:j2], "user")
            for tag, i1, i2, j1, j2 in matcher_ob.get_opcodes()
            if tag != "equal"
        ]

        # Check for collision between Agent edits and User edits
        conflicts = []
        for a_i1, a_i2, a_repl, _ in agent_ops:
            for u_i1, u_i2, u_repl, _ in user_ops:
                # Disjoint check: intervals [a_i1, a_i2] and [u_i1, u_i2]
                is_overlap = False
                if a_i1 < a_i2 and u_i1 < u_i2:
                    is_overlap = max(a_i1, u_i1) < min(a_i2, u_i2)
                elif a_i1 == a_i2 and u_i1 == u_i2:
                    # Both are pure insertions at the same line
                    if len(orig_lines) == 0:
                        # For an empty / new file, user code and agent code are complementary
                        is_overlap = False
                    else:
                        is_overlap = (a_repl != u_repl and a_i1 == u_i1)
                elif a_i1 == a_i2:
                    # Agent inserts: only overlaps if strictly inside user replacement range
                    is_overlap = (u_i1 < a_i1 < u_i2)
                elif u_i1 == u_i2:
                    # User inserts: only overlaps if strictly inside agent replacement range
                    is_overlap = (a_i1 < u_i1 < a_i2)

                if is_overlap:
                    conflicts.append({
                        "base_start": min(a_i1, u_i1),
                        "base_end": max(a_i2, u_i2),
                        "agent_lines": a_repl,
                        "user_lines": u_repl
                    })

        if conflicts:
            if force_resolution == "agent_wins":
                # Agent wins: drop user operations that collide with agent hunks
                filtered_user_ops = []
                for u_i1, u_i2, u_repl, u_tag in user_ops:
                    collides = False
                    for a_i1, a_i2, _, _ in agent_ops:
                        if a_i1 < a_i2 and u_i1 < u_i2:
                            if max(a_i1, u_i1) < min(a_i2, u_i2):
                                collides = True
                                break
                        elif a_i1 == u_i1 and a_i2 == u_i2:
                            collides = True
                            break
                        elif a_i1 == a_i2 and (u_i1 <= a_i1 <= u_i2):
                            collides = True
                            break
                        elif u_i1 == u_i2 and (a_i1 <= u_i1 <= a_i2):
                            collides = True
                            break
                    if not collides:
                        filtered_user_ops.append((u_i1, u_i2, u_repl, u_tag))
                user_ops = filtered_user_ops
            else:
                # Overlapping edits: construct conflict view diff
                conflict_diff = cls._build_conflict_diff(conflicts, filename)
                return {
                    "status": "conflict",
                    "diff": conflict_diff,
                    "has_collision": True,
                    "conflicts": conflicts,
                    "message": f"Detected {len(conflicts)} collision(s) between editor edits and agent diff."
                }

        # Non-overlapping or resolved: perform clean 3-way merge onto Base
        # Preserve user insertions first if tie at insertion point
        all_ops = sorted(agent_ops + user_ops, key=lambda op: (op[0], op[1], 0 if op[3] == "user" else 1))
        merged_lines = []
        curr_idx = 0
        for i1, i2, repl, _ in all_ops:
            if curr_idx < i1:
                merged_lines.extend(orig_lines[curr_idx:i1])
            merged_lines.extend(repl)
            curr_idx = i2
        if curr_idx < len(orig_lines):
            merged_lines.extend(orig_lines[curr_idx:])

        if curr_lines == merged_lines:
            return {
                "status": "already_applied",
                "diff": "",
                "has_collision": False,
                "conflicts": [],
                "message": "All diff changes are already present in active editor buffer."
            }

        # Generate rebased unified diff between Current Editor Buffer and Merged
        rebased_diff = "\n".join(difflib.unified_diff(
            curr_lines,
            merged_lines,
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            lineterm=""
        ))

        if not rebased_diff.strip():
            return {
                "status": "already_applied",
                "diff": "",
                "has_collision": False,
                "conflicts": [],
                "message": "All diff changes are already present in active editor buffer."
            }

        # Validate syntax if Python
        if filename.endswith(".py"):
            val_ok, healed_diff, val_err = cls.validate_and_heal_diff(current_content, rebased_diff, filename)
            if val_ok and healed_diff:
                rebased_diff = healed_diff

        return {
            "status": "rebased",
            "diff": rebased_diff,
            "has_collision": False,
            "conflicts": [],
            "message": "Rebased diff cleanly against current editor buffer."
        }

    @classmethod
    def _build_conflict_diff(
        cls,
        conflicts: List[Dict[str, Any]],
        filename: str
    ) -> str:
        """
        Builds a unified diff representation containing Git-style conflict markers
        for user review when editor edits collide with agent hunks.
        """
        conflict_blocks = []
        conflict_blocks.append(f"--- a/{filename}")
        conflict_blocks.append(f"+++ b/{filename} (CONFLICT)")

        for c in conflicts:
            a_lines = c.get("agent_lines", [])
            u_lines = c.get("user_lines", [])
            b_start = c.get("base_start", 0)
            b_end = c.get("base_end", 0)
            conflict_blocks.append(f"@@ -{b_start+1},{max(1, b_end - b_start)} +{b_start+1},{max(1, len(a_lines))} @@")
            conflict_blocks.append("<<<<<<< YOUR CHANGES (Current Editor)")
            for l in u_lines:
                conflict_blocks.append(f"+{l}")
            conflict_blocks.append("=======")
            for l in a_lines:
                conflict_blocks.append(f"-{l}")
            conflict_blocks.append(">>>>>>> CLAY AGENT")

        return "\n".join(conflict_blocks)
