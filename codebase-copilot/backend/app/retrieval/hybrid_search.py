import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from ..parser.symbols import CodeChunk
from ..index.graph_store import SQLiteGraphStore
from ..index.vector_store import LocalVectorStore
from ..security.secret_masker import SecretMasker

class HybridSearchEngine:
    """
    Token-Efficient Reciprocal Rank Fusion (RRF) combining:
    1. Direct Query-to-Filename Keyword Matching
    2. Vector Semantic Similarity (Chroma / Local Embeddings)
    3. Structural Graph Traversal (SQLite Call Graph)
    4. Surgical Token-Budget Context Formatting (< 350-450 tokens)
    """

    def __init__(self, vector_store: LocalVectorStore, graph_store: SQLiteGraphStore):
        self.vector_store = vector_store
        self.graph_store = graph_store

    async def search(
        self,
        query: str,
        top_k: int = 3,
        k_constant: int = 60,
        security_mode: str = "sandboxed",
        workspace_root: Optional[str] = None
    ) -> List[CodeChunk]:
        # 1. Semantic Vector Search
        vector_results = await self.vector_store.search(query, top_k=top_k * 2)

        # 2. Structural Symbol & Call Graph Search
        symbol_results = await self.graph_store.find_symbols(query, limit=top_k * 2)

        # 3. Direct Query Filename Matching (boost chunks matching named files in query)
        query_words = set(query.lower().replace("/", " ").replace("\\", " ").split())
        direct_file_matches = []
        for chunk in self.vector_store.chunks:
            p = Path(chunk.file_path)
            if p.name.lower() in query_words or p.stem.lower() in query_words:
                direct_file_matches.append(chunk)

        # 4. Compute RRF Scores
        scores: Dict[str, float] = {}
        chunk_map: Dict[str, CodeChunk] = {}

        # Direct filename matches get high initial boost
        for rank, chunk in enumerate(direct_file_matches[:6], start=1):
            cid = chunk.chunk_id
            chunk_map[cid] = chunk
            scores[cid] = scores.get(cid, 0.0) + (2.0 / (k_constant + rank))

        # Accumulate Vector Ranks
        for item in vector_results:
            chunk: CodeChunk = item["chunk"]
            cid = chunk.chunk_id
            chunk_map[cid] = chunk
            rank = item["rank"]
            scores[cid] = scores.get(cid, 0.0) + (1.0 / (k_constant + rank))

        # Accumulate Symbol Proximity Ranks
        for rank, sym in enumerate(symbol_results, start=1):
            for chunk in self.vector_store.chunks:
                if chunk.symbol_name == sym["name"]:
                    cid = chunk.chunk_id
                    chunk_map[cid] = chunk
                    scores[cid] = scores.get(cid, 0.0) + (1.0 / (k_constant + rank))

        # Sort by unified score
        sorted_cids = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)
        all_candidates = [chunk_map[cid] for cid in sorted_cids]

        # 5. Security Boundary Filtering
        filtered_chunks = self.filter_chunks_by_security(
            all_candidates,
            security_mode=security_mode,
            workspace_root=workspace_root
        )

        return filtered_chunks[:top_k]

    @staticmethod
    def filter_chunks_by_security(
        chunks: List[CodeChunk],
        security_mode: str = "sandboxed",
        workspace_root: Optional[str] = None
    ) -> List[CodeChunk]:
        """
        Enforces access control based on agent security mode:
        - strict: Only files inside workspace_root; sensitive patterns fully blocked.
        - sandboxed: Only files inside workspace_root; sensitive patterns masked/excluded.
        - full: All files allowed; sensitive patterns masked.
        """
        valid: List[CodeChunk] = []
        from .file_resolver import WorkspaceFileResolver
        root_path = WorkspaceFileResolver._resolve_root(workspace_root) if workspace_root else None

        for chunk in chunks:
            fpath = Path(chunk.file_path).resolve()

            # Zero-leak secret pattern exclusion
            if SecretMasker.is_file_sensitive(str(fpath)):
                continue

            # In sandboxed and strict modes, restrict to workspace
            if security_mode in ("sandboxed", "strict") and root_path:
                try:
                    fpath.relative_to(root_path)
                except ValueError:
                    # Outside workspace
                    continue

            valid.append(chunk)
        return valid

    @staticmethod
    def build_context_prompt(
        chunks: List[CodeChunk],
        max_chars: int = 1400,
        workspace_root: Optional[str] = None
    ) -> str:
        """
        Token-Budgeted Surgical Context Formatter:
        Caps repository context to ~350-450 tokens (~1400 chars) total,
        extracting focused symbol signatures and statements without bloated file dumps.
        """
        if not chunks:
            return ""

        context_parts = []
        current_len = 0
        from .file_resolver import WorkspaceFileResolver
        root_path = WorkspaceFileResolver._resolve_root(workspace_root) if workspace_root else None

        for chunk in chunks:
            # Clean relative path
            try:
                if root_path:
                    rel_file = str(Path(chunk.file_path).resolve().relative_to(root_path))
                else:
                    rel_file = Path(chunk.file_path).name
            except Exception:
                rel_file = Path(chunk.file_path).name

            header = f"[{rel_file}:{chunk.start_line}-{chunk.end_line} ({chunk.symbol_name})]"
            
            # Keep chunk content concise (max 20 lines / 350 chars per chunk)
            lines = chunk.content.strip().splitlines()
            if len(lines) > 20:
                body = "\n".join(lines[:20]) + "\n... [truncated]"
            else:
                body = "\n".join(lines)

            if len(body) > 350:
                body = body[:350] + "... [truncated]"

            block = f"{header}\n{body}\n"

            if current_len + len(block) > max_chars:
                # If we already have at least one block, break to respect token budget
                if context_parts:
                    break
                # Otherwise truncate the single block to fit budget
                block = block[:max_chars]

            context_parts.append(block)
            current_len += len(block)

        return "\n".join(context_parts)
