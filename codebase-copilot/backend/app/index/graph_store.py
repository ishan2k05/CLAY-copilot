import sqlite3
import asyncio
from typing import List, Dict, Any, Optional
from ..parser.symbols import SymbolNode, RelationshipEdge

class SQLiteGraphStore:
    """
    Embedded relational graph database backed by standard library sqlite3
    to index symbols, inheritance, imports, and call trees with zero dependencies.
    """

    def __init__(self, db_path: str = "code_graph.sqlite"):
        self.db_path = db_path

    def _sync_init_db(self):
        with sqlite3.connect(self.db_path) as db:
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
            db.commit()

    async def init_db(self):
        await asyncio.to_thread(self._sync_init_db)

    def _sync_insert_batch(self, symbols: List[SymbolNode], relationships: List[RelationshipEdge]):
        with sqlite3.connect(self.db_path) as db:
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

    def _sync_find_symbols(self, query: str, limit: int) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            cursor = db.execute("SELECT * FROM symbols WHERE name LIKE ? LIMIT ?", (f"%{query}%", limit))
            return [dict(row) for row in cursor.fetchall()]

    async def find_symbols(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._sync_find_symbols, query, limit)

    def _sync_get_call_graph(self, symbol_name: str) -> Dict[str, Any]:
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            cur1 = db.execute("SELECT target_symbol, relation_type FROM relationships WHERE source_symbol LIKE ?", (f"%{symbol_name}%",))
            callees = [dict(r) for r in cur1.fetchall()]

            cur2 = db.execute("SELECT source_symbol, relation_type FROM relationships WHERE target_symbol LIKE ?", (f"%{symbol_name}%",))
            callers = [dict(r) for r in cur2.fetchall()]

            return {"symbol": symbol_name, "calls": callees, "called_by": callers}

    async def get_call_graph(self, symbol_name: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync_get_call_graph, symbol_name)
