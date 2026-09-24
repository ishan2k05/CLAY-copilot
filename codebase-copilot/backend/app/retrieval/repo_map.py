import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from ..security.secret_masker import SecretMasker
from .file_resolver import WorkspaceFileResolver

class RepoMapGenerator:
    """
    Generates a concise, high-value architectural blueprint (Repo Map)
    of the entire workspace repository.

    Provides the LLM with instant global awareness of:
    1. Full project file structure and relative hierarchy.
    2. Exported classes, methods, and functions with exact line ranges.
    3. Main execution entrypoints, servers, CLI scripts, and configs.
    4. Strict token-budget compliance (< 1,200-1,500 tokens) to ensure zero prompt bloat.
    """

    ENTRYPOINT_NAMES = {
        "main.py", "app.py", "server.py", "standalone_server.py", "index.py",
        "index.ts", "index.js", "extension.ts", "extension.js", "cli.py",
        "__main__.py", "app.ts", "server.ts", "manage.py", "wsgi.py"
    }

    @classmethod
    def generate_repo_map(
        cls,
        workspace_root: str,
        graph_store: Optional[Any] = None,
        max_chars: int = 4500,
        security_mode: str = "sandboxed"
    ) -> str:
        root_path = WorkspaceFileResolver._resolve_root(workspace_root)
        if not root_path.exists() or not root_path.is_dir():
            return ""

        accessible_files = WorkspaceFileResolver.list_accessible_files(str(root_path))
        if not accessible_files:
            return ""

        # Retrieve indexed AST symbols from SQLite graph if available
        symbols_by_file: Dict[str, List[Dict[str, Any]]] = {}
        if graph_store and hasattr(graph_store, "_sync_get_all_symbols"):
            try:
                all_symbols = graph_store._sync_get_all_symbols(limit=600)
                for sym in all_symbols:
                    fpath = sym.get("file_path", "")
                    try:
                        rel = str(Path(fpath).resolve().relative_to(root_path))
                    except Exception:
                        rel = Path(fpath).name
                    symbols_by_file.setdefault(rel, []).append(sym)
            except Exception:
                pass

        # Sort and score files by importance / centrality
        # 1. Entrypoints first
        # 2. Files with more symbols next
        # 3. Non-test files before test files
        # 4. Alphabetical by path
        scored_files = []
        for rel_path, p in accessible_files.items():
            if not p.is_file():
                continue
            if SecretMasker.is_file_sensitive(str(p)):
                continue

            try:
                rel = str(p.relative_to(root_path))
            except ValueError:
                rel = p.name

            syms = symbols_by_file.get(rel, [])
            score = 0
            name_lower = p.name.lower()
            if name_lower in cls.ENTRYPOINT_NAMES:
                score += 100
            if syms:
                score += min(len(syms) * 5, 50)
            if "test" in rel.lower() or "spec" in rel.lower():
                score -= 30

            try:
                line_count = sum(1 for _ in p.open("r", encoding="utf-8", errors="ignore"))
            except Exception:
                line_count = 0

            scored_files.append({
                "rel_path": rel,
                "path_obj": p,
                "score": score,
                "lines": line_count,
                "symbols": syms,
                "ext": p.suffix.lstrip(".").lower() or "file"
            })

        # Sort files by descending score
        scored_files.sort(key=lambda x: (x["score"], -len(x["symbols"]), x["rel_path"]), reverse=True)

        lines: List[str] = [
            f"[GLOBAL REPO MAP: {len(scored_files)} files, {sum(len(f['symbols']) for f in scored_files)} symbols]",
            f"Workspace: {root_path.name}/"
        ]

        current_chars = sum(len(l) + 1 for l in lines)
        files_included = 0

        for f in scored_files:
            rel = f["rel_path"]
            l_cnt = f["lines"]
            syms = f["symbols"]

            file_header = f"├── {rel} ({l_cnt} lines)"
            file_lines = [file_header]

            if syms:
                # Group symbols by classes and standalone functions
                classes: Dict[str, List[Dict[str, Any]]] = {}
                functions: List[Dict[str, Any]] = []
                entrypoints: List[Dict[str, Any]] = []

                for s in syms:
                    stype = s.get("symbol_type")
                    sname = s.get("name")
                    if stype == "class":
                        classes.setdefault(sname, [])
                    elif stype == "method" and s.get("parent_symbol"):
                        classes.setdefault(s.get("parent_symbol"), []).append(s)
                    elif stype == "entrypoint":
                        entrypoints.append(s)
                    else:
                        functions.append(s)

                # Format classes and methods
                for cname, methods in classes.items():
                    file_lines.append(f"│   ├── class {cname}")
                    for m in methods[:6]:
                        m_start = m.get("start_line", 0)
                        file_lines.append(f"│   │   └── def {m.get('name')}() [L{m_start}]")
                    if len(methods) > 6:
                        file_lines.append(f"│   │   └── ... [{len(methods) - 6} more methods]")

                # Format standalone functions
                for fn in functions[:8]:
                    fn_start = fn.get("start_line", 0)
                    fn_end = fn.get("end_line", 0)
                    line_range = f"L{fn_start}-L{fn_end}" if fn_end else f"L{fn_start}"
                    file_lines.append(f"│   ├── def {fn.get('name')}() [{line_range}]")

                if len(functions) > 8:
                    file_lines.append(f"│   ├── ... [{len(functions) - 8} more functions]")

                for ep in entrypoints:
                    file_lines.append(f"│   └── [entrypoint] {ep.get('name')}")

            block_text = "\n".join(file_lines)
            if current_chars + len(block_text) > max_chars:
                remaining = len(scored_files) - files_included
                if remaining > 0:
                    lines.append(f"└── ... [+ {remaining} additional files in workspace]")
                break

            lines.extend(file_lines)
            current_chars += len(block_text) + 1
            files_included += 1

        return "\n".join(lines)

    @classmethod
    def get_repo_map_data(cls, workspace_root: str, graph_store: Optional[Any] = None) -> Dict[str, Any]:
        """Returns structured JSON summary of the repository map."""
        root_path = WorkspaceFileResolver._resolve_root(workspace_root)
        if not root_path.exists() or not root_path.is_dir():
            return {"status": "empty", "files_count": 0, "symbols_count": 0, "map_text": ""}

        map_text = cls.generate_repo_map(str(root_path), graph_store=graph_store, max_chars=8000)
        accessible = WorkspaceFileResolver.list_accessible_files(str(root_path))
        return {
            "status": "success",
            "workspace_root": str(root_path),
            "files_count": len(accessible),
            "map_text": map_text
        }
