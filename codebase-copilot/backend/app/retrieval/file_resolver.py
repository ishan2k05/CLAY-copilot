import os
import difflib
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from ..parser.symbols import CodeChunk
from ..security.secret_masker import SecretMasker

class WorkspaceFileResolver:
    """
    Dynamically discovers and resolves files inside the user's workspace.
    Provides:
    1. Fuzzy and exact filename matching (handles typos like 'soloution.py' -> 'solution.py')
    2. Direct on-disk file reading compliant with active security mode
    3. Workspace repository awareness (file catalog and access level summary)
    """

    EXCLUDED_DIRS = {
        "venv", ".venv", "node_modules", ".git", "__pycache__",
        "dist", "build", ".pytest_cache", ".vscode", "egg-info"
    }

    @classmethod
    def list_accessible_files(cls, workspace_root: str) -> Dict[str, Path]:
        """Returns a mapping of relative paths and basenames to Path objects."""
        root = Path(workspace_root).resolve()
        if not root.exists():
            return {}

        file_map: Dict[str, Path] = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue

            # Ignore excluded directories
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue

            if any(part in cls.EXCLUDED_DIRS or part.startswith(".") for part in rel.parts):
                continue

            # Check zero-leak sensitive file protection
            if SecretMasker.is_file_sensitive(str(path)):
                continue

            rel_str = str(rel)
            file_map[rel_str] = path
            file_map[path.name] = path

        return file_map

    @classmethod
    def resolve_query_files(
        cls,
        query: str,
        workspace_root: str,
        security_mode: str = "sandboxed",
        max_chars_per_file: int = 1200
    ) -> List[CodeChunk]:
        """
        Identifies if user query references any workspace files (exact, substring, or typo/fuzzy).
        Reads matched files directly from disk and wraps them in CodeChunks.
        """
        file_map = cls.list_accessible_files(workspace_root)
        if not file_map:
            return []

        clean_query = query.lower()
        # Extract potential word tokens
        tokens = [w.strip(".,;:?!'\"()[]{}<>`") for w in clean_query.split()]
        tokens = [t for t in tokens if len(t) >= 3]

        matched_paths: Dict[str, Path] = {}
        all_basenames = [p.name.lower() for p in file_map.values()]
        basename_to_path = {p.name.lower(): p for p in file_map.values()}
        stem_to_path = {p.stem.lower(): p for p in file_map.values()}

        # 1. Exact & Substring Search
        for rel_str, p in file_map.items():
            rel_lower = rel_str.lower()
            name_lower = p.name.lower()
            stem_lower = p.stem.lower()

            if name_lower in clean_query or rel_lower in clean_query:
                matched_paths[str(p.resolve())] = p
            elif len(stem_lower) > 3 and stem_lower in tokens:
                matched_paths[str(p.resolve())] = p

        # 2. Fuzzy Matching for Typos (e.g. 'soloution.py' -> 'solution.py')
        for token in tokens:
            # Match with extension
            fuzzy_names = difflib.get_close_matches(token, all_basenames, n=1, cutoff=0.65)
            if fuzzy_names:
                target_name = fuzzy_names[0]
                p = basename_to_path[target_name]
                matched_paths[str(p.resolve())] = p
                continue

            # Match stem without extension
            token_stem = token.split(".")[0]
            if len(token_stem) >= 4:
                fuzzy_stems = difflib.get_close_matches(token_stem, list(stem_to_path.keys()), n=1, cutoff=0.7)
                if fuzzy_stems:
                    target_stem = fuzzy_stems[0]
                    p = stem_to_path[target_stem]
                    matched_paths[str(p.resolve())] = p

        # 3. Read matched files within security mode constraints
        chunks: List[CodeChunk] = []
        root_path = Path(workspace_root).resolve()

        for p in matched_paths.values():
            # Security verification
            if security_mode in ("strict", "sandboxed"):
                try:
                    p.resolve().relative_to(root_path)
                except ValueError:
                    continue

            if SecretMasker.is_file_sensitive(str(p)):
                continue

            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                lines = content.splitlines()
                # Take up to max lines / chars
                truncated = False
                if len(lines) > 35:
                    body = "\n".join(lines[:35]) + "\n... [truncated]"
                    truncated = True
                else:
                    body = "\n".join(lines)

                if len(body) > max_chars_per_file:
                    body = body[:max_chars_per_file] + "... [truncated]"
                    truncated = True

                try:
                    rel_name = str(p.resolve().relative_to(root_path))
                except Exception:
                    rel_name = p.name

                chunks.append(CodeChunk(
                    chunk_id=f"{p}#direct_file",
                    file_path=str(p),
                    symbol_type="file",
                    symbol_name=rel_name,
                    start_line=1,
                    end_line=min(35, max(1, len(lines))),
                    content=body
                ))
            except Exception:
                continue

        return chunks

    @classmethod
    def is_access_or_catalog_query(cls, query: str) -> bool:
        """Checks if the user query is asking about repository access, structure, or file list."""
        q = query.lower()
        keywords = [
            "access", "how much access", "permission", "browse",
            "what files", "what can you see", "list files", "directory",
            "structure", "repository files", "repo path", "workspace",
            "show files", "which files"
        ]
        return any(k in q for k in keywords)

    @classmethod
    def get_workspace_catalog_summary(
        cls,
        workspace_root: str,
        security_mode: str = "sandboxed"
    ) -> str:
        """Returns a concise summary of the repository files and active security mode."""
        file_map = cls.list_accessible_files(workspace_root)
        if not file_map:
            return ""

        unique_rel_files = sorted(list(set(
            k for k in file_map.keys() if "/" in k or not any(k == Path(v).name and "/" in v_rel for v_rel, v in file_map.items())
        )))

        # Prioritize root-level files (like solution.py, main scripts, config) then nested modules
        root_files = sorted([f for f in unique_rel_files if "/" not in f])
        nested_files = sorted([f for f in unique_rel_files if "/" in f])
        ordered_files = root_files + nested_files

        # Format concise overview (< 350 chars)
        sample_files = ordered_files[:25]
        summary = (
            f"[Workspace Repository Context]\n"
            f"- Path: {workspace_root}\n"
            f"- Security Mode: {security_mode.upper()} (Direct read/edit access to workspace files)\n"
            f"- Accessible Files ({len(unique_rel_files)}): {', '.join(sample_files)}"
        )
        if len(unique_rel_files) > 25:
            summary += f" ... (+{len(unique_rel_files) - 25} more)"
        return summary
