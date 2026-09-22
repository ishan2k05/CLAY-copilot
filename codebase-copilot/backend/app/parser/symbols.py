from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class SymbolNode:
    id: str
    name: str
    symbol_type: str  # 'class', 'function', 'method', 'variable', 'route'
    file_path: str
    start_line: int
    end_line: int
    signature: Optional[str] = None
    docstring: Optional[str] = None
    parent_symbol: Optional[str] = None

@dataclass
class RelationshipEdge:
    source_symbol: str
    target_symbol: str
    relation_type: str  # 'calls', 'imports', 'inherits', 'defines'
    file_path: str
    line_number: int

@dataclass
class CodeChunk:
    chunk_id: str
    file_path: str
    symbol_type: str
    symbol_name: str
    start_line: int
    end_line: int
    content: str
    parent_symbol: Optional[str] = None
    signature: Optional[str] = None
    docstring: Optional[str] = None
    imports: List[str] = field(default_factory=list)
    calls: List[str] = field(default_factory=list)
