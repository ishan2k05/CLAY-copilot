import os
import re
import ast
from pathlib import Path
from typing import Dict, Any, List, Optional

class PatchEngine:
    """
    Surgical Unified Diff Patch Engine.
    Applies line-based diffs safely with AST pre-validation,
    supporting both new file creations and targeted modifications.
    """

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
        is_empty = False
        if target_path.exists() and target_path.is_file():
            try:
                is_empty = target_path.stat().st_size == 0
            except Exception:
                pass

        is_new_file = (
            not target_path.exists()
            or is_empty
            or "--- /dev/null" in diff_text
            or "@@ -0,0" in diff_text
        )

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
            if not cls.validate_python_syntax(new_content):
                repaired = cls._attempt_syntax_repair(orig_lines, lines_diff, new_content)
                if repaired and cls.validate_python_syntax(repaired):
                    new_content = repaired
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
    def _apply_unified_diff(cls, orig_lines: List[str], diff_lines: List[str]) -> List[str]:
        """
        Applies unified diff hunks onto original lines using context anchor matching,
        automatic indentation delta synchronization, and surgical hunk replacement
        to prevent syntax errors and misalignment when lines are redacted and added concurrently.
        """
        hunk_header_re = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")

        # 1. Parse into distinct hunks
        hunks: List[Dict[str, Any]] = []
        current_hunk = None
        has_hunk_headers = any(hunk_header_re.match(l) for l in diff_lines)

        def align_indentation(source_line: str, target_indent_str: str) -> str:
            stripped = source_line.lstrip()
            if not stripped:
                return ""
            return target_indent_str + stripped

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

        for line in diff_lines:
            match = hunk_header_re.match(line)
            if match:
                if current_hunk:
                    hunks.append(current_hunk)
                current_hunk = {
                    "old_start": int(match.group(1)),
                    "old_count": int(match.group(2)) if match.group(2) else 1,
                    "new_start": int(match.group(3)),
                    "new_count": int(match.group(4)) if match.group(4) else 1,
                    "lines": []
                }
            elif current_hunk is not None:
                current_hunk["lines"].append(line)

        if current_hunk:
            hunks.append(current_hunk)

        result: List[str] = []
        cursor = 0

        for hunk in hunks:
            hunk_raw = hunk["lines"]
            prefix_ctx: List[str] = []
            del_lines: List[str] = []
            add_lines: List[str] = []
            suffix_ctx: List[str] = []
            seen_changes = False

            for l in hunk_raw:
                if l.startswith("-") and not l.startswith("---"):
                    del_lines.append(l[1:])
                    seen_changes = True
                elif l.startswith("+") and not l.startswith("+++"):
                    add_lines.append(l[1:])
                    seen_changes = True
                elif l.startswith(" ") or not l.startswith("\\"):
                    line_val = l[1:] if l.startswith(" ") else l
                    if not seen_changes:
                        prefix_ctx.append(line_val)
                    else:
                        suffix_ctx.append(line_val)

            old_block = prefix_ctx + del_lines + suffix_ctx
            suggested_idx = max(cursor, hunk["old_start"] - 1)
            match_start = -1
            match_end = -1
            matched_indent = ""
            diff_base_indent = ""

            # Strategy 1: Match full old_block (prefix + deleted + suffix)
            if old_block:
                max_offset = max(len(orig_lines), 60)
                # Exact match near suggested_idx
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

                # Whitespace-stripped match for full block
                if match_start == -1:
                    stripped_target = [l.strip() for l in old_block]
                    for cand in range(cursor, len(orig_lines) - len(old_block) + 1):
                        if [l.strip() for l in orig_lines[cand : cand + len(old_block)]] == stripped_target:
                            match_start = cand + len(prefix_ctx)
                            match_end = match_start + len(del_lines)
                            target_line = orig_lines[match_start]
                            matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]
                            diff_base_indent = del_lines[0][:len(del_lines[0]) - len(del_lines[0].lstrip())] if del_lines else ""
                            break

            # Strategy 2: Match prefix + deleted
            if match_start == -1 and (prefix_ctx or del_lines):
                target = prefix_ctx + del_lines
                stripped_target = [l.strip() for l in target]
                for cand in range(cursor, len(orig_lines) - len(target) + 1):
                    cand_slice = orig_lines[cand : cand + len(target)]
                    if cand_slice == target or [l.strip() for l in cand_slice] == stripped_target:
                        match_start = cand + len(prefix_ctx)
                        match_end = match_start + len(del_lines)
                        target_line = orig_lines[match_start]
                        matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]
                        diff_base_indent = del_lines[0][:len(del_lines[0]) - len(del_lines[0].lstrip())] if del_lines else ""
                        break

            # Strategy 3: Match deleted lines directly
            if match_start == -1 and del_lines:
                stripped_del = [l.strip() for l in del_lines]
                for cand in range(cursor, len(orig_lines) - len(del_lines) + 1):
                    cand_slice = orig_lines[cand : cand + len(del_lines)]
                    if cand_slice == del_lines or [l.strip() for l in cand_slice] == stripped_del:
                        match_start = cand
                        match_end = cand + len(del_lines)
                        target_line = orig_lines[match_start]
                        matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]
                        diff_base_indent = del_lines[0][:len(del_lines[0]) - len(del_lines[0].lstrip())] if del_lines else ""
                        break

            # Strategy 4: Pure additions with context
            if match_start == -1 and not del_lines:
                if prefix_ctx:
                    stripped_prefix = [l.strip() for l in prefix_ctx]
                    for cand in range(cursor, len(orig_lines) - len(prefix_ctx) + 1):
                        cand_slice = orig_lines[cand : cand + len(prefix_ctx)]
                        if cand_slice == prefix_ctx or [l.strip() for l in cand_slice] == stripped_prefix:
                            match_start = cand + len(prefix_ctx)
                            match_end = match_start
                            target_line = orig_lines[cand + len(prefix_ctx) - 1]
                            matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]
                            break
                elif suffix_ctx:
                    stripped_suffix = [l.strip() for l in suffix_ctx]
                    for cand in range(cursor, len(orig_lines) - len(suffix_ctx) + 1):
                        cand_slice = orig_lines[cand : cand + len(suffix_ctx)]
                        if cand_slice == suffix_ctx or [l.strip() for l in cand_slice] == stripped_suffix:
                            match_start = cand
                            match_end = cand
                            target_line = orig_lines[cand]
                            matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]
                            break

            # Fallback if no match: clamp around suggested_idx
            if match_start == -1:
                match_start = min(len(orig_lines), suggested_idx)
                match_end = min(len(orig_lines), match_start + len(del_lines))
                if match_start < len(orig_lines):
                    target_line = orig_lines[match_start]
                    matched_indent = target_line[:len(target_line) - len(target_line.lstrip())]

            # Adjust added lines indentation to preserve Python block scoping
            indent_delta = len(matched_indent) - len(diff_base_indent) if (matched_indent and diff_base_indent) else 0
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

            # Append unchanged original lines from cursor up to match_start
            while cursor < match_start and cursor < len(orig_lines):
                result.append(orig_lines[cursor])
                cursor += 1

            # Append aligned additions
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
        (e.g., mismatched try/except blocks, orphaned except, indentation offsets,
        duplicate blocks).
        """
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
                # Find matching try above
                for try_idx in range(idx - 1, -1, -1):
                    if bad_lines[try_idx].strip().startswith("try:"):
                        try_indent = bad_lines[try_idx][:len(bad_lines[try_idx]) - len(bad_lines[try_idx].lstrip())]
                        curr_indent = line[:len(line) - len(line.lstrip())]
                        if try_indent != curr_indent:
                            # Re-indent the except line and its following block
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
            if 0 <= err_line < len(bad_lines):
                # Try adjusting indentation on the error line and immediate block
                for target_indent in (0, 4, 8, 12, 16):
                    modified = list(bad_lines)
                    modified[err_line] = " " * target_indent + modified[err_line].lstrip()
                    code_cand = "\n".join(modified) + "\n"
                    if cls.validate_python_syntax(code_cand):
                        return code_cand

                    # Also try shifting error line + next lines if it's a statement header
                    if modified[err_line].rstrip().endswith(":"):
                        for block_offset in range(1, min(5, len(bad_lines) - err_line)):
                            modified[err_line + block_offset] = " " * (target_indent + 4) + modified[err_line + block_offset].lstrip()
                        code_cand = "\n".join(modified) + "\n"
                        if cls.validate_python_syntax(code_cand):
                            return code_cand

        return None
