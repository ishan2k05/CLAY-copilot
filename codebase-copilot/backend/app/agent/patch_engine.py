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

    @staticmethod
    def resolve_target_path(file_path: str, workspace_path: Optional[str] = None) -> Path:
        """Resolves target path against workspace if relative."""
        p = Path(file_path.strip().lstrip("b/").lstrip("a/"))
        if not p.is_absolute() and workspace_path:
            p = Path(workspace_path) / p
        return p

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

        # Check if this is a new file creation
        is_new_file = (
            not target_path.exists()
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

    @staticmethod
    def _apply_unified_diff(orig_lines: List[str], diff_lines: List[str]) -> List[str]:
        """Applies unified diff hunks onto original lines."""
        hunk_header_re = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")
        result = []
        orig_idx = 0
        i = 0

        # Skip headers until first hunk
        while i < len(diff_lines) and not hunk_header_re.match(diff_lines[i]):
            i += 1

        if i >= len(diff_lines):
            # Fallback: if no @@ hunks, look for +/- lines
            for line in diff_lines:
                if line.startswith("+") and not line.startswith("+++"):
                    result.append(line[1:])
                elif not line.startswith("-") and not line.startswith("---"):
                    result.append(line)
            return result if result else orig_lines

        while i < len(diff_lines):
            match = hunk_header_re.match(diff_lines[i])
            if match:
                old_start = int(match.group(1))
                # Add unchanged lines up to old_start - 1
                target_idx = max(0, old_start - 1)
                while orig_idx < target_idx and orig_idx < len(orig_lines):
                    result.append(orig_lines[orig_idx])
                    orig_idx += 1
                i += 1
                continue

            line = diff_lines[i]
            if line.startswith("+"):
                result.append(line[1:])
            elif line.startswith("-"):
                # deletion: advance original index
                orig_idx += 1
            elif line.startswith(" "):
                # context line: advance both
                result.append(line[1:])
                orig_idx += 1
            elif not line.startswith("\\"):
                result.append(line)
                orig_idx += 1
            i += 1

        # Append remaining original lines
        while orig_idx < len(orig_lines):
            result.append(orig_lines[orig_idx])
            orig_idx += 1

        return result
