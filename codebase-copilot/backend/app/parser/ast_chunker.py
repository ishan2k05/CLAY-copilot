import ast
from pathlib import Path
from typing import List, Tuple, Optional
from .symbols import SymbolNode, RelationshipEdge, CodeChunk

class PythonASTChunker:
    """
    Parses Python source files using native `ast` to perform
    code-aware chunking and extract symbol relationships.
    """

    def parse_file(self, file_path: str, source_code: str) -> Tuple[List[SymbolNode], List[RelationshipEdge], List[CodeChunk]]:
        symbols: List[SymbolNode] = []
        relationships: List[RelationshipEdge] = []
        chunks: List[CodeChunk] = []

        try:
            tree = ast.parse(source_code, filename=file_path)
        except SyntaxError:
            # If the file has syntax errors, fallback to whole file chunk
            chunk = CodeChunk(
                chunk_id=f"{file_path}#full",
                file_path=file_path,
                symbol_type="file",
                symbol_name=Path(file_path).name,
                start_line=1,
                end_line=len(source_code.splitlines()),
                content=source_code
            )
            return symbols, relationships, [chunk]

        lines = source_code.splitlines()

        # Track top-level imports
        file_imports: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    file_imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    file_imports.append(f"{module}.{alias.name}")

        # Top-level module chunk if imports exist
        if file_imports:
            chunks.append(CodeChunk(
                chunk_id=f"{file_path}#imports",
                file_path=file_path,
                symbol_type="module_imports",
                symbol_name=f"{Path(file_path).stem}_imports",
                start_line=1,
                end_line=min(20, len(lines)),
                imports=file_imports,
                content="\n".join(lines[:min(20, len(lines))])
            ))

        # Inspect classes and functions
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                class_sym, class_rels, class_chunks = self._process_class(node, file_path, lines, file_imports)
                symbols.extend(class_sym)
                relationships.extend(class_rels)
                chunks.extend(class_chunks)

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn_sym, fn_rels, fn_chunk = self._process_function(node, file_path, lines, file_imports)
                symbols.append(fn_sym)
                relationships.extend(fn_rels)
                chunks.append(fn_chunk)

            elif isinstance(node, ast.If):
                start_l = node.lineno
                end_l = getattr(node, "end_lineno", len(lines))
                block_lines = lines[start_l - 1:end_l]
                block_content = "\n".join(block_lines)

                is_main_entry = "__main__" in block_content
                sym_name = "__main__" if is_main_entry else f"if_block_L{start_l}"

                entry_sym = SymbolNode(
                    id=f"{file_path}#{sym_name}",
                    name=sym_name,
                    symbol_type="entrypoint" if is_main_entry else "top_level_block",
                    file_path=file_path,
                    start_line=start_l,
                    end_line=end_l,
                    docstring="Main execution entrypoint" if is_main_entry else None
                )
                symbols.append(entry_sym)

                # Extract calls inside top-level if block (e.g. main())
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        if isinstance(sub.func, ast.Name):
                            relationships.append(RelationshipEdge(
                                source_symbol=f"{file_path}#{sym_name}",
                                target_symbol=sub.func.id,
                                relation_type="calls",
                                file_path=file_path,
                                line_number=getattr(sub, "lineno", start_l)
                            ))

                chunks.append(CodeChunk(
                    chunk_id=f"{file_path}#{sym_name}",
                    file_path=file_path,
                    symbol_type="entrypoint" if is_main_entry else "top_level_block",
                    symbol_name=f"{Path(file_path).name}:{sym_name}",
                    start_line=start_l,
                    end_line=end_l,
                    content=block_content
                ))

        # If file has no classes or functions (e.g. script like solution.py), index the module itself
        if not any(s.symbol_type in ("class", "function", "method") for s in symbols):
            module_name = Path(file_path).stem
            mod_sym = SymbolNode(
                id=f"{file_path}#module",
                name=module_name,
                symbol_type="module",
                file_path=file_path,
                start_line=1,
                end_line=max(1, len(lines)),
                docstring=ast.get_docstring(tree)
            )
            symbols.append(mod_sym)
            chunks.append(CodeChunk(
                chunk_id=f"{file_path}#module_script",
                file_path=file_path,
                symbol_type="module_script",
                symbol_name=Path(file_path).name,
                start_line=1,
                end_line=max(1, len(lines)),
                imports=file_imports,
                content=source_code
            ))

        # For scripts and files <= 150 lines, always provide a complete file chunk
        # so the model sees all declarations, calls, and execution entrypoints in full context
        if len(lines) <= 150 and source_code.strip():
            if not any(c.chunk_id == f"{file_path}#full" or c.symbol_type == "module_script" for c in chunks):
                chunks.append(CodeChunk(
                    chunk_id=f"{file_path}#full",
                    file_path=file_path,
                    symbol_type="file",
                    symbol_name=Path(file_path).name,
                    start_line=1,
                    end_line=len(lines),
                    imports=file_imports,
                    content=source_code
                ))

        return symbols, relationships, chunks

    def _process_class(
        self,
        node: ast.ClassDef,
        file_path: str,
        lines: List[str],
        file_imports: List[str]
    ) -> Tuple[List[SymbolNode], List[RelationshipEdge], List[CodeChunk]]:
        symbols: List[SymbolNode] = []
        relationships: List[RelationshipEdge] = []
        chunks: List[CodeChunk] = []

        start_line = node.lineno
        end_line = getattr(node, "end_lineno", start_line + 10)
        docstring = ast.get_docstring(node)

        class_id = f"{file_path}#{node.name}"
        class_sym = SymbolNode(
            id=class_id,
            name=node.name,
            symbol_type="class",
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            docstring=docstring
        )
        symbols.append(class_sym)

        # Inheritance relationships
        for base in node.bases:
            if isinstance(base, ast.Name):
                relationships.append(RelationshipEdge(
                    source_symbol=class_id,
                    target_symbol=base.id,
                    relation_type="inherits",
                    file_path=file_path,
                    line_number=start_line
                ))

        # Process methods inside class
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method_sym, method_rels, method_chunk = self._process_function(
                    item, file_path, lines, file_imports, parent_class=node.name
                )
                symbols.append(method_sym)
                relationships.extend(method_rels)
                chunks.append(method_chunk)

        return symbols, relationships, chunks

    def _process_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        file_path: str,
        lines: List[str],
        file_imports: List[str],
        parent_class: Optional[str] = None
    ) -> Tuple[SymbolNode, List[RelationshipEdge], CodeChunk]:
        start_line = node.lineno
        end_line = getattr(node, "end_lineno", start_line + 5)
        docstring = ast.get_docstring(node)

        full_name = f"{parent_class}.{node.name}" if parent_class else node.name
        fn_id = f"{file_path}#{full_name}"

        # Extract calls inside function
        calls: List[str] = []
        relationships: List[RelationshipEdge] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                call_name = self._get_call_name(child.func)
                if call_name:
                    calls.append(call_name)
                    relationships.append(RelationshipEdge(
                        source_symbol=fn_id,
                        target_symbol=call_name,
                        relation_type="calls",
                        file_path=file_path,
                        line_number=getattr(child, "lineno", start_line)
                    ))

        content_slice = "\n".join(lines[start_line - 1 : end_line])

        sym = SymbolNode(
            id=fn_id,
            name=full_name,
            symbol_type="method" if parent_class else "function",
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            docstring=docstring,
            parent_symbol=parent_class
        )

        chunk = CodeChunk(
            chunk_id=fn_id,
            file_path=file_path,
            symbol_type="method" if parent_class else "function",
            symbol_name=full_name,
            parent_symbol=parent_class,
            start_line=start_line,
            end_line=end_line,
            docstring=docstring,
            imports=file_imports,
            calls=calls,
            content=content_slice
        )

        return sym, relationships, chunk

    def _get_call_name(self, func_node: ast.AST) -> Optional[str]:
        if isinstance(func_node, ast.Name):
            return func_node.id
        elif isinstance(func_node, ast.Attribute):
            val = self._get_call_name(func_node.value)
            return f"{val}.{func_node.attr}" if val else func_node.attr
        return None
