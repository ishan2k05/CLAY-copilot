import os
import math
import json
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional
from ..parser.symbols import CodeChunk

class LocalVectorStore:
    """
    Code Chunk Vector Store with support for Ollama embeddings
    (e.g., nomic-embed-text) and fast cosine similarity fallback.
    """

    def __init__(self, ollama_url: Optional[str] = None, embed_model: str = "nomic-embed-text"):
        self.ollama_url = ollama_url or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.embed_model = embed_model
        self.chunks: List[CodeChunk] = []
        self.vectors: List[List[float]] = []

    async def get_embedding(self, text: str) -> List[float]:
        """Fetches vector embedding from Ollama or computes fallback frequency vector."""
        url = f"{self.ollama_url}/api/embeddings"
        payload = json.dumps({"model": self.embed_model, "prompt": text[:2000]}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})

        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("embedding", [])
        except Exception:
            pass

        # Fallback pseudo-vector (hashed bag of tokens) for deterministic local ranking
        return self._fallback_hash_vector(text)

    def _fallback_hash_vector(self, text: str, dim: int = 128) -> List[float]:
        vec = [0.0] * dim
        tokens = text.lower().split()
        if not tokens:
            return vec
        for token in tokens:
            idx = abs(hash(token)) % dim
            vec[idx] += 1.0
        # Normalize
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    async def add_chunks(self, chunks: List[CodeChunk]):
        """Embeds and indexes code chunks."""
        self.chunks.extend(chunks)
        for chunk in chunks:
            summary = f"{chunk.symbol_name} ({chunk.symbol_type}) in {chunk.file_path}: {chunk.docstring or ''}\n{chunk.content[:500]}"
            vec = await self.get_embedding(summary)
            self.vectors.append(vec)

    async def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Cosine similarity search over indexed chunks."""
        if not self.chunks or not self.vectors:
            return []

        query_vec = await self.get_embedding(query)
        results = []

        for i, (chunk, vec) in enumerate(zip(self.chunks, self.vectors)):
            # Cosine similarity
            dot = sum(a * b for a, b in zip(query_vec, vec))
            norm_q = math.sqrt(sum(a * a for a in query_vec)) or 1.0
            norm_v = math.sqrt(sum(b * b for b in vec)) or 1.0
            score = dot / (norm_q * norm_v)
            results.append({"chunk": chunk, "score": score, "rank": 0})

        results.sort(key=lambda x: x["score"], reverse=True)
        for rank, item in enumerate(results):
            item["rank"] = rank + 1

        return results[:top_k]
