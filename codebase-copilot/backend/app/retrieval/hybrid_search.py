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
    2. SQLite FTS5 BM25 Full-Text Keyword & Symbol Matching
    3. Vector Semantic Similarity (Ollama / Local Embeddings)
    4. Structural Graph Traversal (SQLite Call Graph)
    5. Surgical Token-Budget Context Formatting (< 350-450 tokens)
    """

    def __init__(self, vector_store: LocalVectorStore, graph_store: SQLiteGraphStore):
        self.vector_store = vector_store
        self.graph_store = graph_store

    async def search(
        self,
        query: str,
        top_k: int = 4,
        k_constant: int = 60,
        security_mode: str = "sandboxed",
        workspace_root: Optional[str] = None
    ) -> List[CodeChunk]:
        # 1. Semantic Vector Search
        vector_results = await self.vector_store.search(query, top_k=top_k * 2)

        # 2. Structural Symbol & Call Graph Search
        symbol_results = await self.graph_store.find_symbols(query, limit=top_k * 2)

        # 3. SQLite FTS5 BM25 Full-Text Keyword Search
        bm25_results = await self.graph_store.bm25_search(query, limit=top_k * 3)

        # 4. Direct Query Filename Matching (boost chunks matching named files in query)
        query_words = set(query.lower().replace("/", " ").replace("\\", " ").split())
        direct_file_matches = []
        for chunk in self.vector_store.chunks:
            p = Path(chunk.file_path)
            if p.name.lower() in query_words or p.stem.lower() in query_words:
                direct_file_matches.append(chunk)

        # 5. Compute Unified RRF Scores across all 4 streams
        scores: Dict[str, float] = {}
        chunk_map: Dict[str, CodeChunk] = {}

        # Stream A: Direct filename matches get high initial boost (weight: 2.0)
        for rank, chunk in enumerate(direct_file_matches[:6], start=1):
            cid = chunk.chunk_id
            chunk_map[cid] = chunk
            scores[cid] = scores.get(cid, 0.0) + (2.0 / (k_constant + rank))

        # Stream B: SQLite FTS5 BM25 Full-Text Keyword Matches (weight: 1.5)
        for rank, bm in enumerate(bm25_results, start=1):
            cid = bm.get("chunk_id")
            if cid:
                for chunk in self.vector_store.chunks:
                    if chunk.chunk_id == cid:
                        chunk_map[cid] = chunk
                        scores[cid] = scores.get(cid, 0.0) + (1.5 / (k_constant + rank))
                        break

        # Stream C: Semantic Vector Ranks (weight: 1.0)
        for item in vector_results:
            chunk: CodeChunk = item["chunk"]
            cid = chunk.chunk_id
            chunk_map[cid] = chunk
            rank = item["rank"]
            scores[cid] = scores.get(cid, 0.0) + (1.0 / (k_constant + rank))

        # Stream D: Symbol Proximity Ranks (weight: 1.0)
        for rank, sym in enumerate(symbol_results, start=1):
            for chunk in self.vector_store.chunks:
                if chunk.symbol_name == sym["name"]:
                    cid = chunk.chunk_id
                    chunk_map[cid] = chunk
                    scores[cid] = scores.get(cid, 0.0) + (1.0 / (k_constant + rank))

        # Sort by unified RRF score
        sorted_cids = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)
        all_candidates = [chunk_map[cid] for cid in sorted_cids]

        # 6. Security Boundary Filtering
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
        max_chars: int = 35000,
        workspace_root: Optional[str] = None
    ) -> str:
        """
        Token-Budgeted Context Formatter:
        Packages repository and file context, preserving full content for directly targeted files
        and focused symbol signatures for search chunks.
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

            # If chunk is a direct file or empty file indicator, preserve complete content
            if chunk.symbol_type in ("file", "empty_file"):
                body = chunk.content
            else:
                lines = chunk.content.strip().splitlines()
                if len(lines) > 80:
                    body = "\n".join(lines[:80]) + "\n... [truncated]"
                else:
                    body = "\n".join(lines)

                if len(body) > 3000:
                    body = body[:3000] + "... [truncated]"

            block = f"{header}\n{body}\n"

            if current_len + len(block) > max_chars:
                if context_parts:
                    if not context_parts[-1].rstrip().endswith("... [truncated]"):
                        context_parts.append("... [truncated]")
                    break
                avail = max(0, max_chars - 17)
                block = block[:avail] + "\n... [truncated]"

            context_parts.append(block)
            current_len += len(block)

        return "\n".join(context_parts)
