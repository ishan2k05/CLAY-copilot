import os
import math
import json
import time
import zlib
import struct
import sqlite3
import asyncio
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Dict, Any, Optional
from ..parser.symbols import CodeChunk

try:
    import httpx
except ImportError:
    httpx = None

class LocalVectorStore:
    """
    Code Chunk Vector Store backed by SQLite persistence (with binary float32 BLOB packing)
    and support for Ollama embeddings (e.g., nomic-embed-text) and fast cosine similarity fallback.
    """

    def __init__(
        self,
        ollama_url: Optional[str] = None,
        embed_model: str = "nomic-embed-text",
        db_path: Optional[str] = None
    ):
        self.ollama_url = ollama_url or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.embed_model = embed_model
        self.db_path = db_path or os.getenv("CLAY_VECTOR_DB_PATH", "code_graph.sqlite")
        self._chunks: List[CodeChunk] = []
        self._vectors: List[List[float]] = []
        self._ollama_available: Optional[bool] = None
        self._last_ollama_check: float = 0.0
        self._mem_conn: Optional[sqlite3.Connection] = None

        self._sync_init_db()
        self._load_persisted_chunks()

    @property
    def chunks(self) -> List[CodeChunk]:
        return self._chunks

    @chunks.setter
    def chunks(self, value: List[CodeChunk]):
        self._chunks = list(value) if value else []
        if not self._chunks:
            self._vectors = []
            self._sync_clear_db()

    @property
    def chunk_embeddings(self) -> Optional[List[List[float]]]:
        return self._vectors

    @chunk_embeddings.setter
    def chunk_embeddings(self, value):
        if value is None:
            self._vectors = []
        elif isinstance(value, list):
            self._vectors = list(value)

    @property
    def vectors(self) -> List[List[float]]:
        return self._vectors

    @vectors.setter
    def vectors(self, value: List[List[float]]):
        self._vectors = list(value) if value else []

    def __len__(self) -> int:
        return len(self._chunks)

    def __iter__(self):
        return iter(self._chunks)

    def _get_connection(self) -> sqlite3.Connection:
        if self.db_path == ":memory:":
            if self._mem_conn is None:
                self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            return self._mem_conn
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        return conn

    def _sync_init_db(self):
        if self.db_path != ":memory:":
            parent_dir = Path(self.db_path).parent
            if parent_dir and str(parent_dir) not in (".", ""):
                parent_dir.mkdir(parents=True, exist_ok=True)

        conn = self._get_connection()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vector_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    symbol_name TEXT,
                    symbol_type TEXT,
                    content TEXT NOT NULL,
                    docstring TEXT,
                    start_line INTEGER,
                    end_line INTEGER,
                    dim INTEGER NOT NULL,
                    vector BLOB NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_vc_file_path ON vector_chunks(file_path);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_vc_symbol ON vector_chunks(symbol_name);")
            conn.commit()
        finally:
            if self.db_path != ":memory:":
                conn.close()

    def _load_persisted_chunks(self):
        try:
            self._sync_init_db()
            conn = self._get_connection()
            try:
                cursor = conn.execute("""
                    SELECT chunk_id, file_path, symbol_name, symbol_type,
                           content, docstring, start_line, end_line, dim, vector
                    FROM vector_chunks
                """)
                rows = cursor.fetchall()
            finally:
                if self.db_path != ":memory:":
                    conn.close()

            loaded_chunks: List[CodeChunk] = []
            loaded_vectors: List[List[float]] = []

            for row in rows:
                (chunk_id, file_path, symbol_name, symbol_type,
                 content, docstring, start_line, end_line, dim, vector_blob) = row

                try:
                    actual_dim = dim if (len(vector_blob) == dim * 4) else (len(vector_blob) // 4)
                    if actual_dim <= 0:
                        continue
                    vec = list(struct.unpack(f"{actual_dim}f", vector_blob[:actual_dim * 4]))
                except Exception:
                    continue

                chunk = CodeChunk(
                    chunk_id=chunk_id,
                    file_path=file_path,
                    symbol_name=symbol_name or "",
                    symbol_type=symbol_type or "",
                    content=content,
                    docstring=docstring or "",
                    start_line=start_line or 0,
                    end_line=end_line or 0
                )
                loaded_chunks.append(chunk)
                loaded_vectors.append(vec)

            self._chunks = loaded_chunks
            self._vectors = loaded_vectors
        except Exception:
            pass

    def _sync_persist_chunks(self, chunks: List[CodeChunk], vectors: List[List[float]]):
        self._sync_init_db()
        records = []
        for c, vec in zip(chunks, vectors):
            dim = len(vec)
            blob = struct.pack(f"{dim}f", *vec)
            records.append((
                c.chunk_id,
                c.file_path,
                c.symbol_name,
                c.symbol_type,
                c.content,
                c.docstring or "",
                c.start_line,
                c.end_line,
                dim,
                blob
            ))

        conn = self._get_connection()
        try:
            conn.executemany("""
                INSERT OR REPLACE INTO vector_chunks (
                    chunk_id, file_path, symbol_name, symbol_type,
                    content, docstring, start_line, end_line, dim, vector
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, records)
            conn.commit()
        finally:
            if self.db_path != ":memory:":
                conn.close()

    def _sync_delete_file_chunks(self, file_path: str):
        self._sync_init_db()
        target_name = Path(file_path).name
        try:
            target_norm = str(Path(file_path).resolve())
        except Exception:
            target_norm = str(file_path)

        conn = self._get_connection()
        try:
            conn.execute("""
                DELETE FROM vector_chunks
                WHERE file_path = ? OR file_path = ? OR file_path LIKE ?
            """, (target_norm, file_path, f"%{target_name}"))
            conn.commit()
        finally:
            if self.db_path != ":memory:":
                conn.close()

    def _sync_clear_db(self):
        self._sync_init_db()
        conn = self._get_connection()
        try:
            conn.execute("DELETE FROM vector_chunks;")
            conn.commit()
        finally:
            if self.db_path != ":memory:":
                conn.close()

    async def clear_all(self):
        """Purges all chunks and vectors from memory and SQLite."""
        self._chunks.clear()
        self._vectors.clear()
        await asyncio.to_thread(self._sync_clear_db)

    def remove_chunks_for_file(self, file_path: str):
        """Purges both chunks and their corresponding embeddings for a file to maintain exact synchronization."""
        target_name = Path(file_path).name
        try:
            target_norm = str(Path(file_path).resolve())
        except Exception:
            target_norm = str(file_path)

        new_chunks = []
        new_vectors = []
        for chunk, vec in zip(self._chunks, self._vectors):
            try:
                c_norm = str(Path(chunk.file_path).resolve())
            except Exception:
                c_norm = str(chunk.file_path)
            c_name = Path(chunk.file_path).name

            if c_norm != target_norm and c_name != target_name:
                new_chunks.append(chunk)
                new_vectors.append(vec)

        self._chunks = new_chunks
        self._vectors = new_vectors
        self._sync_delete_file_chunks(file_path)

    async def _check_ollama_availability(self) -> bool:
        """Quickly probes if Ollama is reachable, caching result for 15s to avoid repeated socket timeouts."""
        now = time.time()
        if self._ollama_available is not None and (now - self._last_ollama_check) < 15.0:
            return self._ollama_available

        url = f"{self.ollama_url}/api/tags"
        reachable = False
        try:
            if httpx is not None:
                async with httpx.AsyncClient(timeout=0.4) as client:
                    resp = await client.get(url)
                    reachable = (resp.status_code == 200)
            else:
                def _probe():
                    try:
                        req = urllib.request.Request(url)
                        with urllib.request.urlopen(req, timeout=0.4) as r:
                            return r.status == 200
                    except Exception:
                        return False
                reachable = await asyncio.to_thread(_probe)
        except Exception:
            reachable = False

        self._ollama_available = reachable
        self._last_ollama_check = now
        return reachable

    async def get_embedding(self, text: str) -> List[float]:
        """Fetches vector embedding from Ollama non-blockingly or computes fallback frequency vector."""
        if not await self._check_ollama_availability():
            return self._fallback_hash_vector(text)

        url = f"{self.ollama_url}/api/embeddings"
        payload = {"model": self.embed_model, "prompt": text[:2000]}

        try:
            if httpx is not None:
                async with httpx.AsyncClient(timeout=1.5) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        emb = data.get("embedding", [])
                        if emb:
                            return emb
            else:
                def _sync_post():
                    req_data = json.dumps(payload).encode("utf-8")
                    req = urllib.request.Request(url, data=req_data, headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=1.5) as resp:
                        if resp.status == 200:
                            data = json.loads(resp.read().decode("utf-8"))
                            return data.get("embedding", [])
                    return []
                emb = await asyncio.to_thread(_sync_post)
                if emb:
                    return emb
        except Exception:
            self._ollama_available = False
            self._last_ollama_check = time.time()

        # Fallback pseudo-vector (hashed bag of tokens) for deterministic local ranking
        return self._fallback_hash_vector(text)

    def _fallback_hash_vector(self, text: str, dim: int = 128) -> List[float]:
        vec = [0.0] * dim
        tokens = text.lower().split()
        if not tokens:
            return vec
        for token in tokens:
            idx = zlib.crc32(token.encode("utf-8")) % dim
            vec[idx] += 1.0
        # Normalize
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    async def add_chunks(self, chunks: List[CodeChunk]):
        """Embeds and indexes code chunks with non-blocking concurrency, persistence, and fast fallback."""
        if not chunks:
            return

        # If Ollama is offline, instantly compute local fallback pseudo-vectors without socket attempts
        if not await self._check_ollama_availability():
            new_vectors = []
            for chunk in chunks:
                summary = f"{chunk.symbol_name} ({chunk.symbol_type}) in {chunk.file_path}: {chunk.docstring or ''}\n{chunk.content[:500]}"
                new_vectors.append(self._fallback_hash_vector(summary))
        else:
            sem = asyncio.Semaphore(5)
            async def _embed_single(c: CodeChunk) -> List[float]:
                async with sem:
                    summary = f"{c.symbol_name} ({c.symbol_type}) in {c.file_path}: {c.docstring or ''}\n{c.content[:500]}"
                    return await self.get_embedding(summary)

            new_vectors = await asyncio.gather(*[_embed_single(c) for c in chunks])

        self._chunks.extend(chunks)
        self._vectors.extend(new_vectors)

        # Persist vectors and chunks to SQLite
        await asyncio.to_thread(self._sync_persist_chunks, chunks, new_vectors)

    async def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Cosine similarity search over indexed chunks."""
        if not self._chunks or not self._vectors:
            return []

        query_vec = await self.get_embedding(query)
        results = []

        norm_q = math.sqrt(sum(a * a for a in query_vec)) or 1.0

        for chunk, vec in zip(self._chunks, self._vectors):
            dot = sum(a * b for a, b in zip(query_vec, vec))
            norm_v = math.sqrt(sum(b * b for b in vec)) or 1.0
            score = dot / (norm_q * norm_v)
            results.append({"chunk": chunk, "score": score, "rank": 0})

        results.sort(key=lambda x: x["score"], reverse=True)
        for rank, item in enumerate(results):
            item["rank"] = rank + 1

        return results[:top_k]

# Backward compatibility alias
SQLiteVectorStore = LocalVectorStore
