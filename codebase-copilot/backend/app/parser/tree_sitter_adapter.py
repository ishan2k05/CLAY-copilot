from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any, Set
from .symbols import SymbolNode, RelationshipEdge, CodeChunk

try:
    import tree_sitter
    from tree_sitter import Language, Parser, Node
    _TREE_SITTER_LIB_AVAILABLE = True
except Exception:
    _TREE_SITTER_LIB_AVAILABLE = False
    tree_sitter = None
    Language = None
    Parser = None
    Node = None


def _wrap_language(raw_lang_provider: Any) -> Optional[Any]:
    """Wraps raw language functions across Tree-Sitter versions into a Language instance."""
    if raw_lang_provider is None or Language is None:
        return None
    try:
        raw = raw_lang_provider() if callable(raw_lang_provider) else raw_lang_provider
        if isinstance(raw, Language):
            return raw
        return Language(raw)
    except Exception:
        try:
            return Language(raw_lang_provider)
        except Exception:
            return None


def _create_parser(language: Any) -> Optional[Any]:
    """Creates a Parser instance supporting both modern (0.22+) and older (0.21) APIs."""
    if language is None or Parser is None:
        return None
    try:
        return Parser(language)
    except TypeError:
        try:
            p = Parser()
            p.language = language
            return p
        except Exception:
            return None
    except Exception:
        return None


class TreeSitterAdapter:
    """
    High-Fidelity AST Adapter powered by Tree-Sitter.
    Dynamically loads available language grammars (TypeScript, Rust, Go, C/C++)
    and extracts precise symbols, signatures, call graphs, and relational edges.
    Provides seamless zero-downtime fallback if Tree-Sitter is unavailable.
    """

    def __init__(self):
        self._parsers: Dict[str, Any] = {}
        self._languages: Dict[str, Any] = {}
        self._discover_grammars()

    @classmethod
    def is_available(cls) -> bool:
        """Returns True if the core tree-sitter library is installed."""
        return _TREE_SITTER_LIB_AVAILABLE

    def _discover_grammars(self):
        """Discovers and caches available Tree-Sitter language grammars."""
        if not _TREE_SITTER_LIB_AVAILABLE:
            return

        # 1. TypeScript and JavaScript
        try:
            import tree_sitter_typescript as ts_mod
            ts_lang = _wrap_language(getattr(ts_mod, "language_typescript", None))
            if ts_lang:
                ts_parser = _create_parser(ts_lang)
                if ts_parser:
                    self._parsers[".ts"] = ts_parser
                    self._parsers[".js"] = ts_parser
                    self._parsers[".mjs"] = ts_parser
                    self._parsers[".cjs"] = ts_parser

            tsx_lang = _wrap_language(getattr(ts_mod, "language_tsx", None))
            if tsx_lang:
                tsx_parser = _create_parser(tsx_lang)
                if tsx_parser:
                    self._parsers[".tsx"] = tsx_parser
                    self._parsers[".jsx"] = tsx_parser
        except Exception:
            pass

        # 2. Rust
        try:
            import tree_sitter_rust as rust_mod
            rust_lang = _wrap_language(getattr(rust_mod, "language", None))
            if rust_lang:
                rust_parser = _create_parser(rust_lang)
                if rust_parser:
                    self._parsers[".rs"] = rust_parser
        except Exception:
            pass

        # 3. Go
        try:
            import tree_sitter_go as go_mod
            go_lang = _wrap_language(getattr(go_mod, "language", None))
            if go_lang:
                go_parser = _create_parser(go_lang)
                if go_parser:
                    self._parsers[".go"] = go_parser
        except Exception:
            pass

        # 4. C++ and C
        try:
            import tree_sitter_cpp as cpp_mod
            cpp_lang = _wrap_language(getattr(cpp_mod, "language", None))
            if cpp_lang:
                cpp_parser = _create_parser(cpp_lang)
                if cpp_parser:
                    for ext in (".cpp", ".hpp", ".cc", ".cxx", ".c", ".h"):
                        self._parsers[ext] = cpp_parser
        except Exception:
            pass

    def supports_extension(self, ext: str) -> bool:
        """Returns True if a Tree-Sitter parser is available for the given extension."""
        return ext.lower() in self._parsers

    def parse(
        self, file_path: str, source_code: str
    ) -> Optional[Tuple[List[SymbolNode], List[RelationshipEdge], List[CodeChunk]]]:
        """
        Parses source code using Tree-Sitter.
        Returns (symbols, relationships, chunks) on success, or None on failure/unsupported language.
        """
        ext = Path(file_path).suffix.lower()
        parser = self._parsers.get(ext)
        if not parser or not source_code:
            return None

        try:
            source_bytes = source_code.encode("utf-8")
            tree = parser.parse(source_bytes)
            if not tree or not tree.root_node:
                return None

            symbols: List[SymbolNode] = []
            relationships: List[RelationshipEdge] = []
            chunks: List[CodeChunk] = []

            lines = source_code.splitlines()

            if ext in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
                self._parse_typescript(tree.root_node, source_bytes, file_path, lines, symbols, relationships, chunks)
            elif ext == ".rs":
                self._parse_rust(tree.root_node, source_bytes, file_path, lines, symbols, relationships, chunks)
            elif ext == ".go":
                self._parse_go(tree.root_node, source_bytes, file_path, lines, symbols, relationships, chunks)
            elif ext in {".cpp", ".hpp", ".cc", ".cxx", ".c", ".h"}:
                self._parse_cpp(tree.root_node, source_bytes, file_path, lines, symbols, relationships, chunks)
            else:
                return None

            # Add full-file chunk for concise modules (<= 150 lines)
            if len(lines) <= 150 and source_code.strip():
                fname = Path(file_path).name
                if not any(c.chunk_id == f"{file_path}#full" for c in chunks):
                    chunks.append(CodeChunk(
                        chunk_id=f"{file_path}#full",
                        file_path=file_path,
                        symbol_type="file",
                        symbol_name=fname,
                        start_line=1,
                        end_line=len(lines),
                        content=source_code
                    ))

            # If no symbols were found, return None to let lexical parser attempt detection
            if not symbols and not chunks:
                return None

            return symbols, relationships, chunks

        except Exception:
            return None

    # -------------------------------------------------------------------------
    # TypeScript & JavaScript AST Extraction
    # -------------------------------------------------------------------------
    def _find_identifiers_in_subtree(self, node: Any, source: bytes) -> List[str]:
        results: List[str] = []
        stop = {"extends", "implements", "public", "private", "protected", "class", "interface", "type"}

        def _walk(cur: Any):
            if cur.type in ("identifier", "type_identifier"):
                txt = self._get_node_text(cur, source)
                if txt and txt not in stop and txt not in results:
                    results.append(txt)
            for ch in cur.children:
                _walk(ch)

        _walk(node)
        return results

    def _parse_typescript(
        self,
        root: Any,
        source: bytes,
        file_path: str,
        lines: List[str],
        symbols: List[SymbolNode],
        relationships: List[RelationshipEdge],
        chunks: List[CodeChunk]
    ):
        # Flatten top-level nodes, unwrapping export statements
        nodes_to_process: List[Tuple[Any, Any]] = []
        for top in root.children:
            if top.type == "export_statement":
                decl = top.child_by_field_name("declaration")
                if decl:
                    nodes_to_process.append((decl, top))
                else:
                    found_inner = False
                    for child in top.children:
                        if child.type in (
                            "class_declaration",
                            "interface_declaration",
                            "type_alias_declaration",
                            "function_declaration",
                            "lexical_declaration",
                            "variable_declaration",
                        ):
                            nodes_to_process.append((child, top))
                            found_inner = True
                    if not found_inner:
                        nodes_to_process.append((top, top))
            else:
                nodes_to_process.append((top, top))

        for node, enclosing in nodes_to_process:
            # 1. Class Declaration
            if node.type == "class_declaration":
                cname = self._get_node_text(node.child_by_field_name("name"), source)
                if not cname:
                    for c in node.children:
                        if c.type in ("type_identifier", "identifier"):
                            cname = self._get_node_text(c, source)
                            break
                if not cname:
                    continue

                start_line = enclosing.start_point[0] + 1
                end_line = enclosing.end_point[0] + 1
                body_text = self._slice_lines(lines, start_line, end_line)

                # Inheritance & Implementation
                for child in node.children:
                    if child.type in ("class_heritage", "heritage_clause"):
                        for clause in child.children:
                            c_type = clause.type
                            c_text = self._get_node_text(clause, source)
                            if c_type == "extends_clause" or "extends" in c_text:
                                for tgt in self._find_identifiers_in_subtree(clause, source):
                                    relationships.append(RelationshipEdge(
                                        source_symbol=cname,
                                        target_symbol=tgt,
                                        relation_type="inherits",
                                        file_path=file_path,
                                        line_number=start_line
                                    ))
                            if c_type == "implements_clause" or "implements" in c_text:
                                for tgt in self._find_identifiers_in_subtree(clause, source):
                                    relationships.append(RelationshipEdge(
                                        source_symbol=cname,
                                        target_symbol=tgt,
                                        relation_type="implements",
                                        file_path=file_path,
                                        line_number=start_line
                                    ))

                symbols.append(SymbolNode(
                    id=f"{file_path}#{cname}",
                    name=cname,
                    symbol_type="class",
                    file_path=file_path,
                    start_line=start_line,
                    end_line=end_line,
                    signature=f"class {cname}"
                ))
                chunks.append(CodeChunk(
                    chunk_id=f"{file_path}#{cname}",
                    file_path=file_path,
                    symbol_type="class",
                    symbol_name=cname,
                    start_line=start_line,
                    end_line=end_line,
                    content=body_text
                ))

                # Methods inside class body
                body_node = node.child_by_field_name("body")
                if body_node:
                    for member in body_node.children:
                        if member.type == "method_definition":
                            mname = self._get_node_text(member.child_by_field_name("name"), source)
                            if not mname:
                                for c in member.children:
                                    if c.type in ("property_identifier", "identifier"):
                                        mname = self._get_node_text(c, source)
                                        break
                            if mname:
                                m_start = member.start_point[0] + 1
                                m_end = member.end_point[0] + 1
                                m_body = self._slice_lines(lines, m_start, m_end)
                                full_mname = f"{cname}.{mname}"
                                m_calls = self._extract_call_identifiers(member, source)

                                symbols.append(SymbolNode(
                                    id=f"{file_path}#{full_mname}",
                                    name=full_mname,
                                    symbol_type="method",
                                    file_path=file_path,
                                    start_line=m_start,
                                    end_line=m_end,
                                    parent_symbol=cname
                                ))
                                chunks.append(CodeChunk(
                                    chunk_id=f"{file_path}#{full_mname}",
                                    file_path=file_path,
                                    symbol_type="method",
                                    symbol_name=full_mname,
                                    start_line=m_start,
                                    end_line=m_end,
                                    content=m_body,
                                    parent_symbol=cname,
                                    calls=m_calls
                                ))
                                for call_tgt in m_calls:
                                    relationships.append(RelationshipEdge(
                                        source_symbol=full_mname,
                                        target_symbol=call_tgt,
                                        relation_type="calls",
                                        file_path=file_path,
                                        line_number=m_start
                                    ))

            # 2. Interface Declaration
            elif node.type == "interface_declaration":
                iname = self._get_node_text(node.child_by_field_name("name"), source)
                if not iname:
                    for c in node.children:
                        if c.type in ("type_identifier", "identifier"):
                            iname = self._get_node_text(c, source)
                            break
                if iname:
                    start_line = enclosing.start_point[0] + 1
                    end_line = enclosing.end_point[0] + 1
                    body_text = self._slice_lines(lines, start_line, end_line)

                    # Extends interface
                    for child in node.children:
                        if child.type in ("extends_type_clause", "extends_clause", "heritage_clause"):
                            for tgt in self._find_identifiers_in_subtree(child, source):
                                relationships.append(RelationshipEdge(
                                    source_symbol=iname,
                                    target_symbol=tgt,
                                    relation_type="inherits",
                                    file_path=file_path,
                                    line_number=start_line
                                ))

                    symbols.append(SymbolNode(
                        id=f"{file_path}#{iname}",
                        name=iname,
                        symbol_type="interface",
                        file_path=file_path,
                        start_line=start_line,
                        end_line=end_line,
                        signature=f"interface {iname}"
                    ))
                    chunks.append(CodeChunk(
                        chunk_id=f"{file_path}#{iname}",
                        file_path=file_path,
                        symbol_type="interface",
                        symbol_name=iname,
                        start_line=start_line,
                        end_line=end_line,
                        content=body_text
                    ))

            # 3. Function Declaration
            elif node.type == "function_declaration":
                fname = self._get_node_text(node.child_by_field_name("name"), source)
                if not fname:
                    for c in node.children:
                        if c.type in ("identifier",):
                            fname = self._get_node_text(c, source)
                            break
                if fname:
                    start_line = enclosing.start_point[0] + 1
                    end_line = enclosing.end_point[0] + 1
                    body_text = self._slice_lines(lines, start_line, end_line)
                    fn_calls = self._extract_call_identifiers(node, source)

                    symbols.append(SymbolNode(
                        id=f"{file_path}#{fname}",
                        name=fname,
                        symbol_type="function",
                        file_path=file_path,
                        start_line=start_line,
                        end_line=end_line
                    ))
                    chunks.append(CodeChunk(
                        chunk_id=f"{file_path}#{fname}",
                        file_path=file_path,
                        symbol_type="function",
                        symbol_name=fname,
                        start_line=start_line,
                        end_line=end_line,
                        content=body_text,
                        calls=fn_calls
                    ))
                    for call_tgt in fn_calls:
                        relationships.append(RelationshipEdge(
                            source_symbol=fname,
                            target_symbol=call_tgt,
                            relation_type="calls",
                            file_path=file_path,
                            line_number=start_line
                        ))

            # 4. Lexical Variable Declarations (const foo = () => {})
            elif node.type in ("lexical_declaration", "variable_declaration"):
                self._extract_arrow_functions(node, source, file_path, lines, symbols, relationships, chunks, enclosing)

    def _extract_arrow_functions(
        self,
        node: Any,
        source: bytes,
        file_path: str,
        lines: List[str],
        symbols: List[SymbolNode],
        relationships: List[RelationshipEdge],
        chunks: List[CodeChunk],
        enclosing: Optional[Any] = None
    ):
        enc = enclosing or node
        declarators = []
        if node.type in ("variable_declaration", "lexical_declaration"):
            for child in node.children:
                if child.type == "variable_declarator":
                    declarators.append(child)
        else:
            for child in node.children:
                if child.type in ("variable_declaration", "lexical_declaration"):
                    for sub in child.children:
                        if sub.type == "variable_declarator":
                            declarators.append(sub)

        for declarator in declarators:
            name_node = declarator.child_by_field_name("name")
            val_node = declarator.child_by_field_name("value")
            if name_node and val_node and val_node.type in ("arrow_function", "function_expression"):
                aname = self._get_node_text(name_node, source)
                if aname:
                    start_line = enc.start_point[0] + 1
                    end_line = enc.end_point[0] + 1
                    body_text = self._slice_lines(lines, start_line, end_line)
                    fn_calls = self._extract_call_identifiers(val_node, source)

                    symbols.append(SymbolNode(
                        id=f"{file_path}#{aname}",
                        name=aname,
                        symbol_type="function",
                        file_path=file_path,
                        start_line=start_line,
                        end_line=end_line
                    ))
                    chunks.append(CodeChunk(
                        chunk_id=f"{file_path}#{aname}",
                        file_path=file_path,
                        symbol_type="function",
                        symbol_name=aname,
                        start_line=start_line,
                        end_line=end_line,
                        content=body_text,
                        calls=fn_calls
                    ))
                    for call_tgt in fn_calls:
                        relationships.append(RelationshipEdge(
                            source_symbol=aname,
                            target_symbol=call_tgt,
                            relation_type="calls",
                            file_path=file_path,
                            line_number=start_line
                        ))

    # -------------------------------------------------------------------------
    # Rust AST Extraction
    # -------------------------------------------------------------------------
    def _parse_rust(
        self,
        root: Any,
        source: bytes,
        file_path: str,
        lines: List[str],
        symbols: List[SymbolNode],
        relationships: List[RelationshipEdge],
        chunks: List[CodeChunk]
    ):
        for node in root.children:
            # 1. Struct Item
            if node.type == "struct_item":
                sname = self._get_node_text(node.child_by_field_name("name"), source)
                if sname:
                    start_line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    body_text = self._slice_lines(lines, start_line, end_line)
                    symbols.append(SymbolNode(
                        id=f"{file_path}#{sname}",
                        name=sname,
                        symbol_type="struct",
                        file_path=file_path,
                        start_line=start_line,
                        end_line=end_line,
                        signature=f"struct {sname}"
                    ))
                    chunks.append(CodeChunk(
                        chunk_id=f"{file_path}#{sname}",
                        file_path=file_path,
                        symbol_type="struct",
                        symbol_name=sname,
                        start_line=start_line,
                        end_line=end_line,
                        content=body_text
                    ))

            # 2. Trait Item
            elif node.type == "trait_item":
                tname = self._get_node_text(node.child_by_field_name("name"), source)
                if tname:
                    start_line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    body_text = self._slice_lines(lines, start_line, end_line)
                    symbols.append(SymbolNode(
                        id=f"{file_path}#{tname}",
                        name=tname,
                        symbol_type="trait",
                        file_path=file_path,
                        start_line=start_line,
                        end_line=end_line,
                        signature=f"trait {tname}"
                    ))
                    chunks.append(CodeChunk(
                        chunk_id=f"{file_path}#{tname}",
                        file_path=file_path,
                        symbol_type="trait",
                        symbol_name=tname,
                        start_line=start_line,
                        end_line=end_line,
                        content=body_text
                    ))

            # 3. Impl Item (impl Trait for Struct OR impl Struct)
            elif node.type == "impl_item":
                type_node = node.child_by_field_name("type")
                trait_node = node.child_by_field_name("trait")
                struct_name = self._get_node_text(type_node, source) if type_node else ""
                trait_name = self._get_node_text(trait_node, source) if trait_node else ""

                start_line = node.start_point[0] + 1
                if struct_name and trait_name:
                    relationships.append(RelationshipEdge(
                        source_symbol=struct_name,
                        target_symbol=trait_name,
                        relation_type="implements",
                        file_path=file_path,
                        line_number=start_line
                    ))

                # Functions inside impl block
                body_node = node.child_by_field_name("body")
                if body_node:
                    for child in body_node.children:
                        if child.type == "function_item":
                            mname = self._get_node_text(child.child_by_field_name("name"), source)
                            if mname:
                                m_start = child.start_point[0] + 1
                                m_end = child.end_point[0] + 1
                                m_body = self._slice_lines(lines, m_start, m_end)
                                full_mname = f"{struct_name}.{mname}" if struct_name else mname
                                m_calls = self._extract_call_identifiers(child, source)

                                symbols.append(SymbolNode(
                                    id=f"{file_path}#{full_mname}",
                                    name=full_mname,
                                    symbol_type="method",
                                    file_path=file_path,
                                    start_line=m_start,
                                    end_line=m_end,
                                    parent_symbol=struct_name
                                ))
                                chunks.append(CodeChunk(
                                    chunk_id=f"{file_path}#{full_mname}",
                                    file_path=file_path,
                                    symbol_type="method",
                                    symbol_name=full_mname,
                                    start_line=m_start,
                                    end_line=m_end,
                                    content=m_body,
                                    parent_symbol=struct_name,
                                    calls=m_calls
                                ))
                                for call_tgt in m_calls:
                                    relationships.append(RelationshipEdge(
                                        source_symbol=full_mname,
                                        target_symbol=call_tgt,
                                        relation_type="calls",
                                        file_path=file_path,
                                        line_number=m_start
                                    ))

            # 4. Standalone Function Item
            elif node.type == "function_item":
                fname = self._get_node_text(node.child_by_field_name("name"), source)
                if fname:
                    start_line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    body_text = self._slice_lines(lines, start_line, end_line)
                    fn_calls = self._extract_call_identifiers(node, source)

                    symbols.append(SymbolNode(
                        id=f"{file_path}#{fname}",
                        name=fname,
                        symbol_type="function",
                        file_path=file_path,
                        start_line=start_line,
                        end_line=end_line
                    ))
                    chunks.append(CodeChunk(
                        chunk_id=f"{file_path}#{fname}",
                        file_path=file_path,
                        symbol_type="function",
                        symbol_name=fname,
                        start_line=start_line,
                        end_line=end_line,
                        content=body_text,
                        calls=fn_calls
                    ))
                    for call_tgt in fn_calls:
                        relationships.append(RelationshipEdge(
                            source_symbol=fname,
                            target_symbol=call_tgt,
                            relation_type="calls",
                            file_path=file_path,
                            line_number=start_line
                        ))

    # -------------------------------------------------------------------------
    # Go AST Extraction
    # -------------------------------------------------------------------------
    def _parse_go(
        self,
        root: Any,
        source: bytes,
        file_path: str,
        lines: List[str],
        symbols: List[SymbolNode],
        relationships: List[RelationshipEdge],
        chunks: List[CodeChunk]
    ):
        for node in root.children:
            # 1. Type Declarations (type Server struct / type Handler interface)
            if node.type == "type_declaration":
                for spec in node.children:
                    if spec.type == "type_spec":
                        tname = self._get_node_text(spec.child_by_field_name("name"), source)
                        type_body = spec.child_by_field_name("type")
                        stype = "struct" if type_body and type_body.type == "struct_type" else "interface"
                        if tname:
                            start_line = node.start_point[0] + 1
                            end_line = node.end_point[0] + 1
                            body_text = self._slice_lines(lines, start_line, end_line)
                            symbols.append(SymbolNode(
                                id=f"{file_path}#{tname}",
                                name=tname,
                                symbol_type=stype,
                                file_path=file_path,
                                start_line=start_line,
                                end_line=end_line,
                                signature=f"type {tname} {stype}"
                            ))
                            chunks.append(CodeChunk(
                                chunk_id=f"{file_path}#{tname}",
                                file_path=file_path,
                                symbol_type=stype,
                                symbol_name=tname,
                                start_line=start_line,
                                end_line=end_line,
                                content=body_text
                            ))

            # 2. Standalone Function Declaration
            elif node.type == "function_declaration":
                fname = self._get_node_text(node.child_by_field_name("name"), source)
                if fname:
                    start_line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    body_text = self._slice_lines(lines, start_line, end_line)
                    fn_calls = self._extract_call_identifiers(node, source)

                    symbols.append(SymbolNode(
                        id=f"{file_path}#{fname}",
                        name=fname,
                        symbol_type="function",
                        file_path=file_path,
                        start_line=start_line,
                        end_line=end_line
                    ))
                    chunks.append(CodeChunk(
                        chunk_id=f"{file_path}#{fname}",
                        file_path=file_path,
                        symbol_type="function",
                        symbol_name=fname,
                        start_line=start_line,
                        end_line=end_line,
                        content=body_text,
                        calls=fn_calls
                    ))
                    for call_tgt in fn_calls:
                        relationships.append(RelationshipEdge(
                            source_symbol=fname,
                            target_symbol=call_tgt,
                            relation_type="calls",
                            file_path=file_path,
                            line_number=start_line
                        ))

            # 3. Method Declaration (with receiver, e.g. func (s *Server) Start() error)
            elif node.type == "method_declaration":
                mname = self._get_node_text(node.child_by_field_name("name"), source)
                receiver_node = node.child_by_field_name("receiver")
                receiver_type = ""
                if receiver_node:
                    rec_text = self._get_node_text(receiver_node, source)
                    # Extract type name from e.g. (s *Server) or (Server)
                    for tok in rec_text.replace("(", "").replace(")", "").split():
                        tok = tok.lstrip("*&")
                        if tok and tok[0].isupper():
                            receiver_type = tok
                            break

                if mname:
                    start_line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    body_text = self._slice_lines(lines, start_line, end_line)
                    full_mname = f"{receiver_type}.{mname}" if receiver_type else mname
                    fn_calls = self._extract_call_identifiers(node, source)

                    symbols.append(SymbolNode(
                        id=f"{file_path}#{full_mname}",
                        name=full_mname,
                        symbol_type="method",
                        file_path=file_path,
                        start_line=start_line,
                        end_line=end_line,
                        parent_symbol=receiver_type
                    ))
                    chunks.append(CodeChunk(
                        chunk_id=f"{file_path}#{full_mname}",
                        file_path=file_path,
                        symbol_type="method",
                        symbol_name=full_mname,
                        start_line=start_line,
                        end_line=end_line,
                        content=body_text,
                        parent_symbol=receiver_type,
                        calls=fn_calls
                    ))
                    if receiver_type:
                        relationships.append(RelationshipEdge(
                            source_symbol=full_mname,
                            target_symbol=receiver_type,
                            relation_type="defines",
                            file_path=file_path,
                            line_number=start_line
                        ))
                    for call_tgt in fn_calls:
                        relationships.append(RelationshipEdge(
                            source_symbol=full_mname,
                            target_symbol=call_tgt,
                            relation_type="calls",
                            file_path=file_path,
                            line_number=start_line
                        ))

    # -------------------------------------------------------------------------
    # C / C++ AST Extraction
    # -------------------------------------------------------------------------
    def _parse_cpp(
        self,
        root: Any,
        source: bytes,
        file_path: str,
        lines: List[str],
        symbols: List[SymbolNode],
        relationships: List[RelationshipEdge],
        chunks: List[CodeChunk]
    ):
        for node in root.children:
            # 1. Class or Struct Specifier
            if node.type in ("class_specifier", "struct_specifier") or (
                node.type == "declaration" and any(c.type in ("class_specifier", "struct_specifier") for c in node.children)
            ):
                spec = node if node.type in ("class_specifier", "struct_specifier") else next(c for c in node.children if c.type in ("class_specifier", "struct_specifier"))
                cname = self._get_node_text(spec.child_by_field_name("name"), source)
                if cname:
                    start_line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    body_text = self._slice_lines(lines, start_line, end_line)

                    # Base classes
                    for child in spec.children:
                        if child.type == "base_class_clause":
                            for sub in child.children:
                                if sub.type in ("type_identifier", "identifier"):
                                    tgt = self._get_node_text(sub, source)
                                    if tgt and tgt not in ("public", "protected", "private", ":"):
                                        relationships.append(RelationshipEdge(
                                            source_symbol=cname,
                                            target_symbol=tgt,
                                            relation_type="inherits",
                                            file_path=file_path,
                                            line_number=start_line
                                        ))

                    symbols.append(SymbolNode(
                        id=f"{file_path}#{cname}",
                        name=cname,
                        symbol_type="class",
                        file_path=file_path,
                        start_line=start_line,
                        end_line=end_line,
                        signature=f"class {cname}"
                    ))
                    chunks.append(CodeChunk(
                        chunk_id=f"{file_path}#{cname}",
                        file_path=file_path,
                        symbol_type="class",
                        symbol_name=cname,
                        start_line=start_line,
                        end_line=end_line,
                        content=body_text
                    ))

            # 2. Function Definition
            elif node.type == "function_definition":
                decl = node.child_by_field_name("declarator")
                fname = ""
                if decl:
                    # Traverses through pointer_declarator, function_declarator, etc.
                    cur = decl
                    while cur and cur.children:
                        if cur.type == "identifier" or cur.type == "field_identifier":
                            fname = self._get_node_text(cur, source)
                            break
                        cur = cur.child_by_field_name("declarator") or cur.children[0]
                    if not fname and cur:
                        fname = self._get_node_text(cur, source)

                if fname:
                    start_line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    body_text = self._slice_lines(lines, start_line, end_line)
                    fn_calls = self._extract_call_identifiers(node, source)

                    symbols.append(SymbolNode(
                        id=f"{file_path}#{fname}",
                        name=fname,
                        symbol_type="function",
                        file_path=file_path,
                        start_line=start_line,
                        end_line=end_line
                    ))
                    chunks.append(CodeChunk(
                        chunk_id=f"{file_path}#{fname}",
                        file_path=file_path,
                        symbol_type="function",
                        symbol_name=fname,
                        start_line=start_line,
                        end_line=end_line,
                        content=body_text,
                        calls=fn_calls
                    ))
                    for call_tgt in fn_calls:
                        relationships.append(RelationshipEdge(
                            source_symbol=fname,
                            target_symbol=call_tgt,
                            relation_type="calls",
                            file_path=file_path,
                            line_number=start_line
                        ))

    # -------------------------------------------------------------------------
    # Helper Utilities
    # -------------------------------------------------------------------------
    def _get_node_text(self, node: Any, source: bytes) -> str:
        """Safely extracts text for a given Tree-Sitter AST node."""
        if not node:
            return ""
        try:
            return source[node.start_byte:node.end_byte].decode("utf-8", errors="ignore").strip()
        except Exception:
            return ""

    def _slice_lines(self, lines: List[str], start_1idx: int, end_1idx: int) -> str:
        """Extracts lines using 1-indexed boundaries."""
        s = max(0, start_1idx - 1)
        e = min(len(lines), end_1idx)
        return "\n".join(lines[s:e])

    def _extract_call_identifiers(self, node: Any, source: bytes) -> List[str]:
        """Extracts called function identifiers from a function/method AST subtree."""
        calls: List[str] = []
        stop_keywords = {
            "if", "for", "while", "switch", "catch", "return", "sizeof",
            "typeof", "instanceof", "new", "import", "from", "export",
            "function", "def", "fn", "func", "let", "const", "var"
        }

        def _walk(cur: Any):
            if cur.type == "call_expression":
                fn_node = cur.child_by_field_name("function")
                if fn_node:
                    raw_name = self._get_node_text(fn_node, source)
                    base = raw_name.split(".")[-1].split("->")[-1].split("::")[-1]
                    if base and base not in stop_keywords and len(base) > 1 and not base.isdigit():
                        if base not in calls:
                            calls.append(base)
            for child in cur.children:
                _walk(child)

        _walk(node)
        return calls[:15]
