import re
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any, Set
from .symbols import SymbolNode, RelationshipEdge, CodeChunk
from .ast_chunker import PythonASTChunker


class BaseSemanticParser:
    """Base class providing lexical delimiter matching and chunk formatting across languages."""

    @staticmethod
    def find_closing_brace(lines: List[str], start_line_idx: int, open_char: str = "{", close_char: str = "}") -> int:
        """
        Finds the 0-indexed line containing the matching closing brace.
        Accurately respects strings (double, single, backticks) and comments.
        """
        depth = 0
        in_line_comment = False
        in_block_comment = False
        in_str: Optional[str] = None
        found_open = False

        for l_idx in range(start_line_idx, len(lines)):
            line = lines[l_idx]
            i = 0
            n = len(line)
            in_line_comment = False

            while i < n:
                ch = line[i]

                # Comments
                if not in_str and not in_block_comment and ch == "/" and i + 1 < n:
                    if line[i + 1] == "/":
                        in_line_comment = True
                        break
                    elif line[i + 1] == "*":
                        in_block_comment = True
                        i += 2
                        continue

                if in_block_comment:
                    if ch == "*" and i + 1 < n and line[i + 1] == "/":
                        in_block_comment = False
                        i += 2
                    else:
                        i += 1
                    continue

                if in_line_comment:
                    break

                # Strings
                if in_str is not None:
                    if ch == "\\" and in_str != "`":
                        i += 2
                        continue
                    elif ch == in_str:
                        in_str = None
                    i += 1
                    continue

                if ch in ('"', "'", "`"):
                    in_str = ch
                    i += 1
                    continue

                if ch == open_char:
                    depth += 1
                    found_open = True
                elif ch == close_char:
                    depth -= 1
                    if found_open and depth <= 0:
                        return l_idx

                i += 1

            if found_open and depth <= 0:
                return l_idx

        return min(start_line_idx + 30, len(lines) - 1)

    @staticmethod
    def extract_calls_from_text(text: str, defined_symbols: Set[str]) -> List[str]:
        """Extracts function/method call identifiers from a text block."""
        call_re = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\(")
        stop_keywords = {
            "if", "for", "while", "switch", "catch", "return", "sizeof",
            "typeof", "instanceof", "new", "import", "from", "export",
            "function", "def", "fn", "func", "let", "const", "var"
        }
        calls = []
        for m in call_re.finditer(text):
            cname = m.group(1).strip()
            base = cname.split(".")[0]
            if base not in stop_keywords and len(cname) > 1 and not cname.isdigit():
                if cname not in calls:
                    calls.append(cname)
        return calls[:15]


class TypeScriptSemanticParser(BaseSemanticParser):
    """
    Semantic AST chunker for TypeScript and JavaScript (.ts, .tsx, .js, .jsx, .mjs, .cjs).
    Extracts classes, inheritance (extends), interfaces (implements), standalone functions,
    class methods, arrow functions, and call graphs.
    """

    CLASS_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z0-9_$]+)(?:<.*?>)?(?:\s+extends\s+([A-Za-z0-9_$\.]+))?(?:\s+implements\s+([A-Za-z0-9_$,\s\.]+))?", re.MULTILINE)
    INTERFACE_RE = re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z0-9_$]+)(?:<.*?>)?(?:\s+extends\s+([A-Za-z0-9_$,\s\.]+))?", re.MULTILINE)
    TYPE_RE = re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z0-9_$]+)(?:<.*?>)?\s*=", re.MULTILINE)
    FUNC_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*(?:\*\s*)?([A-Za-z0-9_$]+)\s*\(", re.MULTILINE)
    ARROW_RE = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*(?::\s*[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z0-9_$]+)\s*=>", re.MULTILINE)
    METHOD_RE = re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+|override\s+|readonly\s+)*([A-Za-z0-9_$]+)\s*(?:<.*?>)?\s*\([^)]*\)\s*(?::\s*[^{]+)?\s*\{")
    IMPORT_RE = re.compile(r"^\s*import\s+(?:(?:\{([^}]+)\})|(?:\*\s+as\s+([A-Za-z0-9_$]+))|([A-Za-z0-9_$]+))\s+from\s+['\"]([^'\"]+)['\"]", re.MULTILINE)

    def parse(self, file_path: str, source_code: str) -> Tuple[List[SymbolNode], List[RelationshipEdge], List[CodeChunk]]:
        symbols: List[SymbolNode] = []
        relationships: List[RelationshipEdge] = []
        chunks: List[CodeChunk] = []

        lines = source_code.splitlines()
        if not lines:
            return symbols, relationships, chunks

        # 1. Imports
        file_imports: List[str] = []
        for m in self.IMPORT_RE.finditer(source_code):
            module = m.group(4)
            items = m.group(1) or m.group(2) or m.group(3) or ""
            for item in items.split(","):
                item_name = item.strip().split(" as ")[0].strip()
                if item_name:
                    file_imports.append(f"{module}.{item_name}")

        if file_imports:
            imp_end = min(25, len(lines))
            chunks.append(CodeChunk(
                chunk_id=f"{file_path}#imports",
                file_path=file_path,
                symbol_type="module_imports",
                symbol_name=f"{Path(file_path).stem}_imports",
                start_line=1,
                end_line=imp_end,
                imports=file_imports,
                content="\n".join(lines[:imp_end])
            ))

        processed_ranges: List[Tuple[int, int]] = []

        # 2. Classes & Methods
        for m in self.CLASS_RE.finditer(source_code):
            cname = m.group(1)
            extends_val = m.group(2)
            implements_val = m.group(3)

            start_char = m.start()
            start_line = source_code[:start_char].count("\n") + 1
            start_idx = start_line - 1
            end_idx = self.find_closing_brace(lines, start_idx)
            end_line = end_idx + 1

            cid = f"{file_path}#{cname}"
            symbols.append(SymbolNode(
                id=cid,
                name=cname,
                symbol_type="class",
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                signature=m.group(0).strip()
            ))

            if extends_val:
                relationships.append(RelationshipEdge(
                    source_symbol=cid,
                    target_symbol=extends_val.strip(),
                    relation_type="inherits",
                    file_path=file_path,
                    line_number=start_line
                ))

            if implements_val:
                for iface in implements_val.split(","):
                    iface_clean = iface.strip()
                    if iface_clean:
                        relationships.append(RelationshipEdge(
                            source_symbol=cid,
                            target_symbol=iface_clean,
                            relation_type="implements",
                            file_path=file_path,
                            line_number=start_line
                        ))

            # Scan methods inside class
            for m_idx in range(start_idx + 1, end_idx):
                mline = lines[m_idx]
                method_match = self.METHOD_RE.match(mline)
                if method_match:
                    mname = method_match.group(1)
                    if mname in ("if", "for", "while", "switch", "catch"):
                        continue
                    m_end_idx = self.find_closing_brace(lines, m_idx)
                    m_start_line = m_idx + 1
                    m_end_line = m_end_idx + 1
                    mid = f"{file_path}#{cname}.{mname}"
                    m_content = "\n".join(lines[m_idx : m_end_idx + 1])
                    m_calls = self.extract_calls_from_text(m_content, {cname})

                    symbols.append(SymbolNode(
                        id=mid,
                        name=f"{cname}.{mname}",
                        symbol_type="method",
                        file_path=file_path,
                        start_line=m_start_line,
                        end_line=m_end_line,
                        parent_symbol=cname,
                        signature=mline.strip()
                    ))
                    for call in m_calls:
                        relationships.append(RelationshipEdge(
                            source_symbol=mid,
                            target_symbol=call,
                            relation_type="calls",
                            file_path=file_path,
                            line_number=m_start_line
                        ))
                    chunks.append(CodeChunk(
                        chunk_id=mid,
                        file_path=file_path,
                        symbol_type="method",
                        symbol_name=f"{cname}.{mname}",
                        parent_symbol=cname,
                        start_line=m_start_line,
                        end_line=m_end_line,
                        imports=file_imports,
                        calls=m_calls,
                        content=m_content
                    ))

            class_content = "\n".join(lines[start_idx : end_idx + 1])
            chunks.append(CodeChunk(
                chunk_id=cid,
                file_path=file_path,
                symbol_type="class",
                symbol_name=cname,
                start_line=start_line,
                end_line=end_line,
                imports=file_imports,
                content=class_content
            ))
            processed_ranges.append((start_line, end_line))

        # 3. Interfaces & Types
        for m in self.INTERFACE_RE.finditer(source_code):
            iname = m.group(1)
            ext_val = m.group(2)
            start_line = source_code[:m.start()].count("\n") + 1
            start_idx = start_line - 1
            end_idx = self.find_closing_brace(lines, start_idx)
            end_line = end_idx + 1
            iid = f"{file_path}#{iname}"

            symbols.append(SymbolNode(
                id=iid,
                name=iname,
                symbol_type="interface",
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                signature=m.group(0).strip()
            ))
            if ext_val:
                for base in ext_val.split(","):
                    b_clean = base.strip()
                    if b_clean:
                        relationships.append(RelationshipEdge(
                            source_symbol=iid,
                            target_symbol=b_clean,
                            relation_type="inherits",
                            file_path=file_path,
                            line_number=start_line
                        ))
            chunks.append(CodeChunk(
                chunk_id=iid,
                file_path=file_path,
                symbol_type="interface",
                symbol_name=iname,
                start_line=start_line,
                end_line=end_line,
                content="\n".join(lines[start_idx : end_idx + 1])
            ))
            processed_ranges.append((start_line, end_line))

        for m in self.TYPE_RE.finditer(source_code):
            tname = m.group(1)
            start_line = source_code[:m.start()].count("\n") + 1
            tid = f"{file_path}#{tname}"
            symbols.append(SymbolNode(
                id=tid,
                name=tname,
                symbol_type="type",
                file_path=file_path,
                start_line=start_line,
                end_line=min(start_line + 5, len(lines)),
                signature=m.group(0).strip()
            ))

        # 4. Standalone & Arrow Functions
        for fn_re in (self.FUNC_RE, self.ARROW_RE):
            for m in fn_re.finditer(source_code):
                fname = m.group(1)
                start_line = source_code[:m.start()].count("\n") + 1
                if any(pr_start <= start_line <= pr_end for pr_start, pr_end in processed_ranges):
                    continue
                start_idx = start_line - 1
                end_idx = self.find_closing_brace(lines, start_idx)
                end_line = end_idx + 1
                fid = f"{file_path}#{fname}"
                f_content = "\n".join(lines[start_idx : end_idx + 1])
                f_calls = self.extract_calls_from_text(f_content, {fname})

                symbols.append(SymbolNode(
                    id=fid,
                    name=fname,
                    symbol_type="function",
                    file_path=file_path,
                    start_line=start_line,
                    end_line=end_line,
                    signature=m.group(0).strip()
                ))
                for call in f_calls:
                    relationships.append(RelationshipEdge(
                        source_symbol=fid,
                        target_symbol=call,
                        relation_type="calls",
                        file_path=file_path,
                        line_number=start_line
                    ))
                chunks.append(CodeChunk(
                    chunk_id=fid,
                    file_path=file_path,
                    symbol_type="function",
                    symbol_name=fname,
                    start_line=start_line,
                    end_line=end_line,
                    imports=file_imports,
                    calls=f_calls,
                    content=f_content
                ))
                processed_ranges.append((start_line, end_line))

        # Whole-file fallback for smaller scripts
        if len(lines) <= 150 and source_code.strip():
            if not any(c.chunk_id == f"{file_path}#full" for c in chunks):
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


class RustSemanticParser(BaseSemanticParser):
    """Semantic AST chunker for Rust (.rs). Extracts structs, enums, traits, impl blocks, and functions."""

    STRUCT_RE = re.compile(r"^\s*(?:pub(?:\([^\)]+\))?\s+)?struct\s+([A-Za-z0-9_]+)", re.MULTILINE)
    ENUM_RE = re.compile(r"^\s*(?:pub(?:\([^\)]+\))?\s+)?enum\s+([A-Za-z0-9_]+)", re.MULTILINE)
    TRAIT_RE = re.compile(r"^\s*(?:pub(?:\([^\)]+\))?\s+)?trait\s+([A-Za-z0-9_]+)", re.MULTILINE)
    IMPL_FOR_RE = re.compile(r"^\s*impl(?:<.*?>)?\s+([A-Za-z0-9_:]+)\s+for\s+([A-Za-z0-9_:]+)", re.MULTILINE)
    IMPL_RE = re.compile(r"^\s*impl(?:<.*?>)?\s+([A-Za-z0-9_:]+)\s*\{", re.MULTILINE)
    FN_RE = re.compile(r"^\s*(?:pub(?:\([^\)]+\))?\s+)?(?:async\s+)?fn\s+([A-Za-z0-9_]+)\s*(?:<.*?>)?\s*\(", re.MULTILINE)
    USE_RE = re.compile(r"^\s*use\s+([A-Za-z0-9_:]+(?:::\{[^}]+\})?);", re.MULTILINE)

    def parse(self, file_path: str, source_code: str) -> Tuple[List[SymbolNode], List[RelationshipEdge], List[CodeChunk]]:
        symbols: List[SymbolNode] = []
        relationships: List[RelationshipEdge] = []
        chunks: List[CodeChunk] = []

        lines = source_code.splitlines()
        if not lines:
            return symbols, relationships, chunks

        file_imports = [m.group(1) for m in self.USE_RE.finditer(source_code)]

        # 1. Structs, Enums, Traits
        for kind, pat in (("struct", self.STRUCT_RE), ("enum", self.ENUM_RE), ("trait", self.TRAIT_RE)):
            for m in pat.finditer(source_code):
                sname = m.group(1)
                start_line = source_code[:m.start()].count("\n") + 1
                start_idx = start_line - 1
                end_idx = self.find_closing_brace(lines, start_idx)
                end_line = end_idx + 1
                sid = f"{file_path}#{sname}"

                symbols.append(SymbolNode(
                    id=sid,
                    name=sname,
                    symbol_type=kind,
                    file_path=file_path,
                    start_line=start_line,
                    end_line=end_line,
                    signature=m.group(0).strip()
                ))
                chunks.append(CodeChunk(
                    chunk_id=sid,
                    file_path=file_path,
                    symbol_type=kind,
                    symbol_name=sname,
                    start_line=start_line,
                    end_line=end_line,
                    content="\n".join(lines[start_idx : end_idx + 1])
                ))

        # 2. Impl Blocks
        for m in self.IMPL_FOR_RE.finditer(source_code):
            trait_name = m.group(1).split("::")[-1]
            target_name = m.group(2).split("::")[-1]
            start_line = source_code[:m.start()].count("\n") + 1
            target_id = f"{file_path}#{target_name}"

            relationships.append(RelationshipEdge(
                source_symbol=target_id,
                target_symbol=trait_name,
                relation_type="implements",
                file_path=file_path,
                line_number=start_line
            ))

        # 3. Functions
        for m in self.FN_RE.finditer(source_code):
            fname = m.group(1)
            start_line = source_code[:m.start()].count("\n") + 1
            start_idx = start_line - 1
            end_idx = self.find_closing_brace(lines, start_idx)
            end_line = end_idx + 1
            fid = f"{file_path}#{fname}"
            f_content = "\n".join(lines[start_idx : end_idx + 1])
            f_calls = self.extract_calls_from_text(f_content, {fname})

            symbols.append(SymbolNode(
                id=fid,
                name=fname,
                symbol_type="function",
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                signature=m.group(0).strip()
            ))
            for call in f_calls:
                relationships.append(RelationshipEdge(
                    source_symbol=fid,
                    target_symbol=call,
                    relation_type="calls",
                    file_path=file_path,
                    line_number=start_line
                ))
            chunks.append(CodeChunk(
                chunk_id=fid,
                file_path=file_path,
                symbol_type="function",
                symbol_name=fname,
                start_line=start_line,
                end_line=end_line,
                calls=f_calls,
                content=f_content
            ))

        if len(lines) <= 150 and source_code.strip():
            if not any(c.chunk_id == f"{file_path}#full" for c in chunks):
                chunks.append(CodeChunk(
                    chunk_id=f"{file_path}#full",
                    file_path=file_path,
                    symbol_type="file",
                    symbol_name=Path(file_path).name,
                    start_line=1,
                    end_line=len(lines),
                    content=source_code
                ))

        return symbols, relationships, chunks


class GoSemanticParser(BaseSemanticParser):
    """Semantic AST chunker for Go (.go). Extracts structs, interfaces, functions, receiver methods, and calls."""

    STRUCT_RE = re.compile(r"^\s*type\s+([A-Za-z0-9_]+)\s+struct\s*\{", re.MULTILINE)
    INTERFACE_RE = re.compile(r"^\s*type\s+([A-Za-z0-9_]+)\s+interface\s*\{", re.MULTILINE)
    METHOD_RE = re.compile(r"^\s*func\s*\(\s*(?:[A-Za-z0-9_]+\s+)?\*?([A-Za-z0-9_]+)\s*\)\s*([A-Za-z0-9_]+)\s*\(", re.MULTILINE)
    FUNC_RE = re.compile(r"^\s*func\s+([A-Za-z0-9_]+)\s*\(", re.MULTILINE)

    def parse(self, file_path: str, source_code: str) -> Tuple[List[SymbolNode], List[RelationshipEdge], List[CodeChunk]]:
        symbols: List[SymbolNode] = []
        relationships: List[RelationshipEdge] = []
        chunks: List[CodeChunk] = []

        lines = source_code.splitlines()
        if not lines:
            return symbols, relationships, chunks

        # 1. Structs and Interfaces
        for kind, pat in (("struct", self.STRUCT_RE), ("interface", self.INTERFACE_RE)):
            for m in pat.finditer(source_code):
                tname = m.group(1)
                start_line = source_code[:m.start()].count("\n") + 1
                start_idx = start_line - 1
                end_idx = self.find_closing_brace(lines, start_idx)
                end_line = end_idx + 1
                tid = f"{file_path}#{tname}"

                symbols.append(SymbolNode(
                    id=tid,
                    name=tname,
                    symbol_type=kind,
                    file_path=file_path,
                    start_line=start_line,
                    end_line=end_line,
                    signature=m.group(0).strip()
                ))
                chunks.append(CodeChunk(
                    chunk_id=tid,
                    file_path=file_path,
                    symbol_type=kind,
                    symbol_name=tname,
                    start_line=start_line,
                    end_line=end_line,
                    content="\n".join(lines[start_idx : end_idx + 1])
                ))

        # 2. Receiver Methods
        for m in self.METHOD_RE.finditer(source_code):
            rcv = m.group(1)
            mname = m.group(2)
            start_line = source_code[:m.start()].count("\n") + 1
            start_idx = start_line - 1
            end_idx = self.find_closing_brace(lines, start_idx)
            end_line = end_idx + 1
            mid = f"{file_path}#{rcv}.{mname}"
            m_content = "\n".join(lines[start_idx : end_idx + 1])
            m_calls = self.extract_calls_from_text(m_content, {rcv, mname})

            symbols.append(SymbolNode(
                id=mid,
                name=f"{rcv}.{mname}",
                symbol_type="method",
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                parent_symbol=rcv,
                signature=m.group(0).strip()
            ))
            relationships.append(RelationshipEdge(
                source_symbol=mid,
                target_symbol=rcv,
                relation_type="defines",
                file_path=file_path,
                line_number=start_line
            ))
            for call in m_calls:
                relationships.append(RelationshipEdge(
                    source_symbol=mid,
                    target_symbol=call,
                    relation_type="calls",
                    file_path=file_path,
                    line_number=start_line
                ))
            chunks.append(CodeChunk(
                chunk_id=mid,
                file_path=file_path,
                symbol_type="method",
                symbol_name=f"{rcv}.{mname}",
                parent_symbol=rcv,
                start_line=start_line,
                end_line=end_line,
                calls=m_calls,
                content=m_content
            ))

        # 3. Standalone Functions
        for m in self.FUNC_RE.finditer(source_code):
            fname = m.group(1)
            start_line = source_code[:m.start()].count("\n") + 1
            # Check if this matched inside receiver method
            m_line = lines[start_line - 1]
            if "func (" in m_line:
                continue
            start_idx = start_line - 1
            end_idx = self.find_closing_brace(lines, start_idx)
            end_line = end_idx + 1
            fid = f"{file_path}#{fname}"
            f_content = "\n".join(lines[start_idx : end_idx + 1])
            f_calls = self.extract_calls_from_text(f_content, {fname})

            symbols.append(SymbolNode(
                id=fid,
                name=fname,
                symbol_type="function",
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                signature=m.group(0).strip()
            ))
            for call in f_calls:
                relationships.append(RelationshipEdge(
                    source_symbol=fid,
                    target_symbol=call,
                    relation_type="calls",
                    file_path=file_path,
                    line_number=start_line
                ))
            chunks.append(CodeChunk(
                chunk_id=fid,
                file_path=file_path,
                symbol_type="function",
                symbol_name=fname,
                start_line=start_line,
                end_line=end_line,
                calls=f_calls,
                content=f_content
            ))

        if len(lines) <= 150 and source_code.strip():
            if not any(c.chunk_id == f"{file_path}#full" for c in chunks):
                chunks.append(CodeChunk(
                    chunk_id=f"{file_path}#full",
                    file_path=file_path,
                    symbol_type="file",
                    symbol_name=Path(file_path).name,
                    start_line=1,
                    end_line=len(lines),
                    content=source_code
                ))

        return symbols, relationships, chunks


class GenericCStyleSemanticParser(BaseSemanticParser):
    """
    Parser for C, C++, Java, C#, PHP, Ruby, Kotlin, and Swift.
    Extracts classes, structs, interfaces, inheritance, and functions across C-family grammars.
    """

    CLASS_RE = re.compile(r"^\s*(?:(?:public|private|protected|static|abstract|final|open|data|sealed|export)\s+)*(?:class|interface|struct|record|trait|module)\s+([A-Za-z0-9_]+)(?:<.*?>)?(?:\s*(?:extends|implements|:|<)\s*([A-Za-z0-9_,\s\.<>]+))?", re.MULTILINE)
    FUNC_RE = re.compile(r"^\s*(?:(?:public|private|protected|static|async|override|fun|func|def|function)\s+)*(?:[A-Za-z0-9_<>\*&]+\s+)?([A-Za-z0-9_]+)\s*\([^)]*\)\s*(?:throws\s+[^{]+)?\s*\{", re.MULTILINE)

    def parse(self, file_path: str, source_code: str) -> Tuple[List[SymbolNode], List[RelationshipEdge], List[CodeChunk]]:
        symbols: List[SymbolNode] = []
        relationships: List[RelationshipEdge] = []
        chunks: List[CodeChunk] = []

        lines = source_code.splitlines()
        if not lines:
            return symbols, relationships, chunks

        # 1. Classes and Interfaces
        for m in self.CLASS_RE.finditer(source_code):
            cname = m.group(1)
            bases_str = m.group(2)
            start_line = source_code[:m.start()].count("\n") + 1
            start_idx = start_line - 1
            end_idx = self.find_closing_brace(lines, start_idx)
            end_line = end_idx + 1
            cid = f"{file_path}#{cname}"

            symbols.append(SymbolNode(
                id=cid,
                name=cname,
                symbol_type="class",
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                signature=m.group(0).strip()
            ))

            if bases_str:
                for base in bases_str.replace("implements", ",").replace("extends", ",").split(","):
                    b_clean = base.strip().split("<")[0].strip()
                    if b_clean and b_clean not in ("{", "public", "private", "protected"):
                        relationships.append(RelationshipEdge(
                            source_symbol=cid,
                            target_symbol=b_clean,
                            relation_type="inherits",
                            file_path=file_path,
                            line_number=start_line
                        ))

            chunks.append(CodeChunk(
                chunk_id=cid,
                file_path=file_path,
                symbol_type="class",
                symbol_name=cname,
                start_line=start_line,
                end_line=end_line,
                content="\n".join(lines[start_idx : end_idx + 1])
            ))

        # 2. Functions
        for m in self.FUNC_RE.finditer(source_code):
            fname = m.group(1)
            if fname in ("if", "for", "while", "switch", "catch", "class", "struct", "interface"):
                continue
            start_line = source_code[:m.start()].count("\n") + 1
            start_idx = start_line - 1
            end_idx = self.find_closing_brace(lines, start_idx)
            end_line = end_idx + 1
            fid = f"{file_path}#{fname}"
            f_content = "\n".join(lines[start_idx : end_idx + 1])
            f_calls = self.extract_calls_from_text(f_content, {fname})

            symbols.append(SymbolNode(
                id=fid,
                name=fname,
                symbol_type="function",
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                signature=m.group(0).strip()
            ))
            for call in f_calls:
                relationships.append(RelationshipEdge(
                    source_symbol=fid,
                    target_symbol=call,
                    relation_type="calls",
                    file_path=file_path,
                    line_number=start_line
                ))
            chunks.append(CodeChunk(
                chunk_id=fid,
                file_path=file_path,
                symbol_type="function",
                symbol_name=fname,
                start_line=start_line,
                end_line=end_line,
                calls=f_calls,
                content=f_content
            ))

        if len(lines) <= 150 and source_code.strip():
            if not any(c.chunk_id == f"{file_path}#full" for c in chunks):
                chunks.append(CodeChunk(
                    chunk_id=f"{file_path}#full",
                    file_path=file_path,
                    symbol_type="file",
                    symbol_name=Path(file_path).name,
                    start_line=1,
                    end_line=len(lines),
                    content=source_code
                ))

        return symbols, relationships, chunks


class UniversalASTChunker:
    """
    Universal Multi-Language Semantic AST Chunker.
    Seamlessly routes source files across 30+ extensions to dedicated semantic parsers:
    - Python (.py) -> PythonASTChunker (100% backward compatible native ast.parse)
    - TypeScript & JavaScript (.ts, .tsx, .js, .jsx, .mjs, .cjs) -> TypeScriptSemanticParser
    - Rust (.rs) -> RustSemanticParser
    - Go (.go) -> GoSemanticParser
    - C, C++, Java, C#, PHP, Ruby, Kotlin, Swift -> GenericCStyleSemanticParser
    - Other text files -> Bounded 30-line fallback chunking
    """

    def __init__(self):
        self.python_chunker = PythonASTChunker()
        self.ts_parser = TypeScriptSemanticParser()
        self.rust_parser = RustSemanticParser()
        self.go_parser = GoSemanticParser()
        self.cstyle_parser = GenericCStyleSemanticParser()

    def parse_file(
        self, file_path: str, source_code: str
    ) -> Tuple[List[SymbolNode], List[RelationshipEdge], List[CodeChunk]]:
        ext = Path(file_path).suffix.lower()

        # 1. Python uses 100% untouched native AST chunker
        if ext == ".py":
            return self.python_chunker.parse_file(file_path, source_code)

        try:
            # 2. TypeScript and JavaScript
            if ext in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
                return self.ts_parser.parse(file_path, source_code)

            # 3. Rust
            if ext == ".rs":
                return self.rust_parser.parse(file_path, source_code)

            # 4. Go
            if ext == ".go":
                return self.go_parser.parse(file_path, source_code)

            # 5. C/C++, Java, C#, PHP, Ruby, Kotlin, Swift
            if ext in {
                ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx",
                ".java", ".cs", ".php", ".rb", ".kt", ".kts", ".swift"
            }:
                return self.cstyle_parser.parse(file_path, source_code)

        except Exception:
            # Graceful degradation on malformed code
            pass

        # 6. Fallback bounded chunker for documentation, configs, and other formats
        return self._fallback_bounded_chunking(file_path, source_code)

    def _fallback_bounded_chunking(
        self, file_path: str, source_code: str
    ) -> Tuple[List[SymbolNode], List[RelationshipEdge], List[CodeChunk]]:
        chunks: List[CodeChunk] = []
        lines = source_code.splitlines()
        if not lines:
            return [], [], []

        fname = Path(file_path).name
        step = 30
        for i in range(0, len(lines), step):
            chunk_lines = lines[i : i + step]
            body = "\n".join(chunk_lines)
            if body.strip():
                chunks.append(CodeChunk(
                    chunk_id=f"{file_path}#{i+1}-{i+len(chunk_lines)}",
                    file_path=file_path,
                    symbol_type="file_section",
                    symbol_name=f"{fname}:{i+1}",
                    start_line=i + 1,
                    end_line=i + len(chunk_lines),
                    content=body
                ))

        if len(lines) <= 150 and source_code.strip():
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

        return [], [], chunks
