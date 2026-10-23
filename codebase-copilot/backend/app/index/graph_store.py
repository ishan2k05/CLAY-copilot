import sqlite3
import asyncio
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from ..parser.symbols import SymbolNode, RelationshipEdge

class SQLiteGraphStore:
    """
    Embedded relational graph database and full-text search engine backed by standard library sqlite3.
    Indexes:
    1. Symbols (classes, functions, methods, entrypoints)
    2. Structural relationships (calls, inherits, imports)
    3. SQLite FTS5 BM25 Full-Text Search for exact token, identifier, and keyword matching.
    """

    def __init__(self, db_path: str = "code_graph.sqlite"):
        self.db_path = db_path
        self.has_fts = True

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        return conn

    def _sync_init_db(self):
        with self._get_connection() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS symbols (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    symbol_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    start_line INTEGER,
                    end_line INTEGER,
                    docstring TEXT,
                    parent_symbol TEXT
                );
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS relationships (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_symbol TEXT,
                    target_symbol TEXT,
                    relation_type TEXT,
                    file_path TEXT,
                    line_number INTEGER,
                    FOREIGN KEY(source_symbol) REFERENCES symbols(id)
                );
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_sym_name ON symbols(name);")
            db.execute("CREATE INDEX IF NOT EXISTS idx_rel_source ON relationships(source_symbol);")
            db.execute("CREATE INDEX IF NOT EXISTS idx_rel_target ON relationships(target_symbol);")

            # FTS5 Virtual Table for BM25 Search
            try:
                db.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                        chunk_id UNINDEXED,
                        file_path,
                        symbol_name,
                        content,
                        tokenize='porter unicode61'
                    );
                """)
                self.has_fts = True
            except sqlite3.OperationalError:
                self.has_fts = False
            db.commit()

    def _sync_clear_all(self):
        self._sync_init_db()
        with self._get_connection() as db:
            db.execute("DELETE FROM relationships;")
            db.execute("DELETE FROM symbols;")
            if getattr(self, "has_fts", True):
                try:
                    db.execute("DELETE FROM chunks_fts;")
                except Exception:
                    pass
            db.commit()

    async def clear_all(self):
        await asyncio.to_thread(self._sync_clear_all)

    def _sync_delete_file_symbols(self, file_path: str):
        self._sync_init_db()
        with self._get_connection() as db:
            norm = str(Path(file_path).resolve())
            orig = str(file_path)
            # Match exact path variants (Windows vs POSIX slashes) for this specific file only
            candidates = tuple(set([
                norm,
                norm.replace("\\", "/"),
                norm.replace("/", "\\"),
                orig,
                orig.replace("\\", "/"),
                orig.replace("/", "\\")
            ]))
            placeholders = ",".join("?" for _ in candidates)
            db.execute(f"DELETE FROM relationships WHERE file_path IN ({placeholders});", candidates)
            db.execute(f"DELETE FROM symbols WHERE file_path IN ({placeholders});", candidates)
            if getattr(self, "has_fts", True):
                try:
                    db.execute(f"DELETE FROM chunks_fts WHERE file_path IN ({placeholders});", candidates)
                except Exception:
                    pass
            db.commit()

    async def delete_file_symbols(self, file_path: str):
        await asyncio.to_thread(self._sync_delete_file_symbols, file_path)

    async def init_db(self):
        await asyncio.to_thread(self._sync_init_db)

    def _sync_insert_batch(self, symbols: List[SymbolNode], relationships: List[RelationshipEdge]):
        with self._get_connection() as db:
            db.executemany("""
                INSERT OR REPLACE INTO symbols (id, name, symbol_type, file_path, start_line, end_line, docstring, parent_symbol)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (s.id, s.name, s.symbol_type, s.file_path, s.start_line, s.end_line, s.docstring, s.parent_symbol)
                for s in symbols
            ])
            db.executemany("""
                INSERT INTO relationships (source_symbol, target_symbol, relation_type, file_path, line_number)
                VALUES (?, ?, ?, ?, ?)
            """, [
                (r.source_symbol, r.target_symbol, r.relation_type, r.file_path, r.line_number)
                for r in relationships
            ])
            db.commit()

    async def insert_batch(self, symbols: List[SymbolNode], relationships: List[RelationshipEdge]):
        await asyncio.to_thread(self._sync_insert_batch, symbols, relationships)

    def _sync_index_chunks_bm25(self, chunks: List[Any]):
        if not getattr(self, "has_fts", True) or not chunks:
            return
        with self._get_connection() as db:
            try:
                db.executemany("""
                    INSERT INTO chunks_fts (chunk_id, file_path, symbol_name, content)
                    VALUES (?, ?, ?, ?)
                """, [
                    (c.chunk_id, c.file_path, c.symbol_name, c.content)
                    for c in chunks
                ])
                db.commit()
            except Exception:
                pass

    async def index_chunks_bm25(self, chunks: List[Any]):
        await asyncio.to_thread(self._sync_index_chunks_bm25, chunks)

    def _sync_bm25_search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        if not getattr(self, "has_fts", True):
            return []
        tokens = re.findall(r'\b[a-zA-Z0-9_]+\b', query)
        if not tokens:
            return []
        match_query = " OR ".join(f'"{t}"*' for t in tokens[:12])
        with self._get_connection() as db:
            db.row_factory = sqlite3.Row
            try:
                cursor = db.execute("""
                    SELECT chunk_id, file_path, symbol_name, bm25(chunks_fts) AS rank_score
                    FROM chunks_fts
                    WHERE chunks_fts MATCH ?
                    ORDER BY rank_score ASC
                    LIMIT ?
                """, (match_query, limit))
                return [dict(r) for r in cursor.fetchall()]
            except Exception:
                return []

    async def bm25_search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._sync_bm25_search, query, limit)

    def _sync_find_symbols(self, query: str, limit: int) -> List[Dict[str, Any]]:
        with self._get_connection() as db:
            db.row_factory = sqlite3.Row
            cursor = db.execute("SELECT * FROM symbols WHERE name LIKE ? LIMIT ?", (f"%{query}%", limit))
            return [dict(row) for row in cursor.fetchall()]

    async def find_symbols(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._sync_find_symbols, query, limit)

    def _sync_get_all_symbols(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self._get_connection() as db:
            db.row_factory = sqlite3.Row
            cursor = db.execute("""
                SELECT id, name, symbol_type, file_path, start_line, end_line, docstring, parent_symbol
                FROM symbols
                ORDER BY file_path ASC, start_line ASC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    async def get_all_symbols(self, limit: int = 500) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._sync_get_all_symbols, limit)

    def _sync_get_call_graph(self, symbol_name: str) -> Dict[str, Any]:
        with self._get_connection() as db:
            db.row_factory = sqlite3.Row
            cur1 = db.execute("SELECT target_symbol, relation_type FROM relationships WHERE source_symbol LIKE ?", (f"%{symbol_name}%",))
            callees = [dict(r) for r in cur1.fetchall()]

            cur2 = db.execute("SELECT source_symbol, relation_type FROM relationships WHERE target_symbol LIKE ?", (f"%{symbol_name}%",))
            callers = [dict(r) for r in cur2.fetchall()]

            return {"symbol": symbol_name, "calls": callees, "called_by": callers}

    async def get_call_graph(self, symbol_name: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync_get_call_graph, symbol_name)
