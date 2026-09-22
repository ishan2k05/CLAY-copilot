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
    def _resolve_root(cls, workspace_root: Optional[str]) -> Path:
        """Resolves workspace root, searching Desktop and common user directories if bare folder name is given."""
        if not workspace_root or not str(workspace_root).strip():
            return Path.cwd().resolve()
        p_str = str(workspace_root).strip()
        if p_str.startswith("~"):
            p = Path(os.path.expanduser(p_str))
            if p.exists() and p.is_dir():
                return p.resolve()
        p = Path(p_str)
        if p.is_absolute() and p.exists() and p.is_dir():
            return p.resolve()
        home = Path.home()
        for cand in [
            home / "Desktop" / p_str,
            home / p_str,
            home / "Documents" / p_str,
            home / "Downloads" / p_str,
            Path("/home/ishan/.gemini/antigravity-ide/scratch") / p_str,
            Path.cwd() / p_str,
        ]:
            if cand.exists() and cand.is_dir():
                return cand.resolve()
        dt = home / "Desktop"
        if dt.exists() and dt.is_dir():
            try:
                for child in dt.iterdir():
                    if child.is_dir() and child.name.lower() == p_str.lower():
                        return child.resolve()
            except Exception:
                pass
        return p.resolve()

    @classmethod
    def list_accessible_files(cls, workspace_root: str) -> Dict[str, Path]:
        """Returns a mapping of relative paths and basenames to Path objects."""
        root = cls._resolve_root(workspace_root)
        if not root.exists():
            return {}

        file_map: Dict[str, Path] = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue

            try:
                rel = path.relative_to(root)
            except ValueError:
                continue

            if any(part in cls.EXCLUDED_DIRS or part.startswith(".") for part in rel.parts):
                continue

            if SecretMasker.is_file_sensitive(str(path)):
                continue

            rel_str = str(rel)
            file_map[rel_str] = path
            file_map[path.name] = path

        return file_map

    @classmethod
    def list_accessible_dirs(cls, workspace_root: str) -> Dict[str, Path]:
        """Returns a mapping of relative directory paths and names to Path objects."""
        root = cls._resolve_root(workspace_root)
        if not root.exists():
            return {}

        dir_map: Dict[str, Path] = {}
        for path in root.rglob("*"):
            if not path.is_dir():
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue

            if any(part in cls.EXCLUDED_DIRS or part.startswith(".") for part in rel.parts):
                continue

            dir_map[str(rel).lower()] = path
            dir_map[path.name.lower()] = path

        return dir_map

    @classmethod
    def resolve_query_files(
        cls,
        query: str,
        workspace_root: str,
        security_mode: str = "sandboxed",
        max_chars_per_file: int = 1200
    ) -> List[CodeChunk]:
        """
        Identifies if user query references any workspace files or package directories (exact, substring, or typo/fuzzy).
        Reads matched files or module entrypoints directly from disk and wraps them in CodeChunks.
        """
        file_map = cls.list_accessible_files(workspace_root)
        dir_map = cls.list_accessible_dirs(workspace_root)
        if not file_map and not dir_map:
            return []

        clean_query = query.lower()
        # Extract potential word tokens
        tokens = [w.strip(".,;:?!'\"()[]{}<>`") for w in clean_query.split()]
        tokens = [t for t in tokens if len(t) >= 3]

        matched_paths: Dict[str, Path] = {}
        matched_dirs: Dict[str, Path] = {}
        all_basenames = [p.name.lower() for p in file_map.values()]
        basename_to_path = {p.name.lower(): p for p in file_map.values()}
        stem_to_path = {p.stem.lower(): p for p in file_map.values()}

        # 1. Exact & Substring Search on files
        for rel_str, p in file_map.items():
            rel_lower = rel_str.lower()
            name_lower = p.name.lower()
            stem_lower = p.stem.lower()

            if name_lower in clean_query or rel_lower in clean_query:
                matched_paths[str(p.resolve())] = p
            elif len(stem_lower) > 3 and stem_lower in tokens:
                matched_paths[str(p.resolve())] = p

        # 2. Match directory/package names (e.g. query mentions 'app.py' or 'app' when directory 'app' exists)
        for d_name, d_path in dir_map.items():
            if d_name in clean_query:
                matched_dirs[str(d_path.resolve())] = d_path
            for t in tokens:
                t_stem = t.split(".")[0]
                if t_stem == d_name or difflib.get_close_matches(t_stem, [d_name], n=1, cutoff=0.75):
                    matched_dirs[str(d_path.resolve())] = d_path

        # 3. Fuzzy Matching for Typos (e.g. 'soloution.py' -> 'solution.py')
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

        # 4. Read matched files within security mode constraints
        chunks: List[CodeChunk] = []
        root_path = cls._resolve_root(workspace_root)

        for p in matched_paths.values():
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
                if len(lines) > 35:
                    body = "\n".join(lines[:35]) + "\n... [truncated]"
                else:
                    body = "\n".join(lines)

                if len(body) > max_chars_per_file:
                    body = body[:max_chars_per_file] + "... [truncated]"

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

        # 5. Handle matched directory packages (e.g. app -> app/main.py or contents of app/)
        for d_path in matched_dirs.values():
            try:
                # Find entrypoint inside directory (main.py, __init__.py, index.ts, or first file)
                sub_files = [f for f in d_path.iterdir() if f.is_file() and not f.name.startswith(".")]
                entry_file = None
                for cand in ["main.py", "__init__.py", "index.ts", "index.js", "app.py"]:
                    if (d_path / cand).exists():
                        entry_file = d_path / cand
                        break
                if not entry_file and sub_files:
                    entry_file = sub_files[0]

                rel_dir = str(d_path.relative_to(root_path))
                sub_names = [f.name for f in sub_files[:10]]
                dir_summary = f"[Package/Directory: {rel_dir}]\nContains files: {', '.join(sub_names)}"
                if len(sub_files) > 10:
                    dir_summary += f" (+{len(sub_files) - 10} more)"

                if entry_file and not SecretMasker.is_file_sensitive(str(entry_file)):
                    lines = entry_file.read_text(encoding="utf-8", errors="ignore").splitlines()[:25]
                    entry_rel = str(entry_file.relative_to(root_path))
                    dir_summary += f"\n\n[Primary Entrypoint: {entry_rel}]\n" + "\n".join(lines)

                chunks.append(CodeChunk(
                    chunk_id=f"{d_path}#dir_overview",
                    file_path=str(entry_file or d_path),
                    symbol_type="directory_package",
                    symbol_name=f"{rel_dir} (directory)",
                    start_line=1,
                    end_line=25,
                    content=dir_summary[:max_chars_per_file]
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
            "show files", "which files", "summarize", "summarise",
            "sumamrize", "summary", "overview", "codebase", "project overview",
            "what is this repo", "what does this repo", "what is this project",
            "what does this project do", "explain repo", "explain the repo",
            "explain codebase", "explain the codebase", "tell me about this repo",
            "tell me about the repo", "tell me about this codebase",
            "architecture", "what does this code do", "what is in this repo",
            "what is this", "what is the repo", "what is this app"
        ]
        return any(k in q for k in keywords)

    @classmethod
    def get_workspace_catalog_summary(
        cls,
        workspace_root: str,
        security_mode: str = "sandboxed"
    ) -> str:
        """Returns a concise summary of the repository files, key entrypoints, and active security mode."""
        file_map = cls.list_accessible_files(workspace_root)
        root = cls._resolve_root(workspace_root)
        if not file_map:
            return f"[Workspace Repository Context]\n- Path: {workspace_root}\n- Status: Empty or inaccessible directory."

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

        # Inspect key entrypoint files to assist repository summary and overview
        entrypoint_candidates = [
            "solution.py", "app/main.py", "main.py", "standalone_server.py",
            "requirements.txt", "package.json", "README.md"
        ]
        found_entrypoints = []
        for cand in entrypoint_candidates:
            p = root / cand
            if p.exists() and p.is_file() and not SecretMasker.is_file_sensitive(str(p)):
                try:
                    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()[:12]
                    preview = " // ".join(l.strip() for l in lines if l.strip() and not l.strip().startswith("#"))[:160]
                    if preview:
                        found_entrypoints.append(f"  * {cand}: {preview}")
                except Exception:
                    pass

        if found_entrypoints:
            summary += "\n[Key Repository Entrypoints]\n" + "\n".join(found_entrypoints[:4])

        return summary
