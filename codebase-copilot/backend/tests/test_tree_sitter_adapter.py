import unittest
import sys
from pathlib import Path

_backend_dir = Path(__file__).resolve().parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))
_root_dir = _backend_dir.parent
if str(_root_dir) not in sys.path:
    sys.path.insert(0, str(_root_dir))

from app.parser.tree_sitter_adapter import TreeSitterAdapter
from app.parser.universal_chunker import UniversalASTChunker


class TestTreeSitterAdapter(unittest.TestCase):

    def setUp(self):
        self.adapter = TreeSitterAdapter()
        self.universal_chunker = UniversalASTChunker()

    def test_tree_sitter_availability(self):
        """Verifies that the TreeSitterAdapter correctly detects available libraries."""
        self.assertTrue(TreeSitterAdapter.is_available())
        # Should support at least typescript if installed
        if self.adapter.supports_extension(".ts"):
            self.assertTrue(self.adapter.supports_extension(".ts"))
            self.assertTrue(self.adapter.supports_extension(".tsx"))

    def test_typescript_ast_parsing(self):
        """Verifies high-fidelity TypeScript AST symbol extraction."""
        if not self.adapter.supports_extension(".ts"):
            self.skipTest("tree_sitter_typescript grammar not installed")

        ts_code = (
            "import { IAuth, ILogger } from './interfaces';\n"
            "import { BaseService } from './base';\n\n"
            "export class AuthService extends BaseService implements IAuth, ILogger {\n"
            "  constructor(private repo: any) {\n"
            "    super();\n"
            "  }\n\n"
            "  async login(username: string): Promise<boolean> {\n"
            "    const user = await this.repo.findUser(username);\n"
            "    return verifyPassword(user);\n"
            "  }\n"
            "}\n\n"
            "export interface UserDTO extends BaseDTO {\n"
            "  id: string;\n"
            "}\n\n"
            "export const hashPassword = async (raw: string): Promise<string> => {\n"
            "  return cryptoHash(raw);\n"
            "};\n"
        )
        res = self.adapter.parse("src/auth/service.ts", ts_code)
        self.assertIsNotNone(res)
        syms, rels, chunks = res

        # 1. Class symbol
        class_sym = next((s for s in syms if s.symbol_type == "class" and s.name == "AuthService"), None)
        self.assertIsNotNone(class_sym)

        # 2. Inherits and Implements
        inherits = [r for r in rels if r.relation_type == "inherits"]
        implements = [r for r in rels if r.relation_type == "implements"]
        self.assertTrue(any(r.target_symbol == "BaseService" for r in inherits))
        self.assertTrue(any("IAuth" in r.target_symbol for r in implements))

        # 3. Method symbol
        method_sym = next((s for s in syms if s.symbol_type == "method" and s.name == "AuthService.login"), None)
        self.assertIsNotNone(method_sym)
        self.assertEqual(method_sym.parent_symbol, "AuthService")

        # 4. Calls
        calls = [r for r in rels if r.relation_type == "calls"]
        self.assertTrue(any("verifyPassword" in r.target_symbol for r in calls))

        # 5. Interface
        iface_sym = next((s for s in syms if s.symbol_type == "interface" and s.name == "UserDTO"), None)
        self.assertIsNotNone(iface_sym)

        # 6. Arrow function
        fn_sym = next((s for s in syms if s.symbol_type == "function" and s.name == "hashPassword"), None)
        self.assertIsNotNone(fn_sym)

    def test_rust_ast_parsing(self):
        """Verifies high-fidelity Rust AST symbol extraction."""
        if not self.adapter.supports_extension(".rs"):
            self.skipTest("tree_sitter_rust grammar not installed")

        rs_code = (
            "pub trait StorageEngine {\n"
            "  fn get(&self, key: &str) -> Option<String>;\n"
            "}\n\n"
            "pub struct Database {\n"
            "  connection: String,\n"
            "}\n\n"
            "impl StorageEngine for Database {\n"
            "  fn get(&self, key: &str) -> Option<String> {\n"
            "    execute_query(&self.connection, key)\n"
            "  }\n"
            "}\n\n"
            "pub fn init_db() -> Database {\n"
            "  connect_pool()\n"
            "}\n"
        )
        res = self.adapter.parse("src/db.rs", rs_code)
        self.assertIsNotNone(res)
        syms, rels, chunks = res

        # Struct and Trait
        self.assertTrue(any(s.name == "Database" and s.symbol_type == "struct" for s in syms))
        self.assertTrue(any(s.name == "StorageEngine" and s.symbol_type == "trait" for s in syms))

        # Implements
        implements = [r for r in rels if r.relation_type == "implements"]
        self.assertTrue(any(r.source_symbol == "Database" and r.target_symbol == "StorageEngine" for r in implements))

        # Impl Method
        method_sym = next((s for s in syms if s.name == "Database.get"), None)
        self.assertIsNotNone(method_sym)

        # Standalone function and calls
        fn_sym = next((s for s in syms if s.name == "init_db"), None)
        self.assertIsNotNone(fn_sym)
        calls = [r for r in rels if r.relation_type == "calls"]
        self.assertTrue(any("connect_pool" in r.target_symbol for r in calls))

    def test_go_ast_parsing(self):
        """Verifies high-fidelity Go AST symbol extraction."""
        if not self.adapter.supports_extension(".go"):
            self.skipTest("tree_sitter_go grammar not installed")

        go_code = (
            "package main\n\n"
            "type Handler interface {\n"
            "  ServeHTTP(w ResponseWriter, r *Request)\n"
            "}\n\n"
            "type Server struct {\n"
            "  Port int\n"
            "}\n\n"
            "func (s *Server) Start() error {\n"
            "  return listenAndServe(s.Port)\n"
            "}\n\n"
            "func NewServer(port int) *Server {\n"
            "  return &Server{Port: port}\n"
            "}\n"
        )
        res = self.adapter.parse("server.go", go_code)
        self.assertIsNotNone(res)
        syms, rels, chunks = res

        # Struct and interface
        self.assertTrue(any(s.name == "Server" and s.symbol_type == "struct" for s in syms))
        self.assertTrue(any(s.name == "Handler" and s.symbol_type == "interface" for s in syms))

        # Receiver method
        method_sym = next((s for s in syms if s.name == "Server.Start"), None)
        self.assertIsNotNone(method_sym)
        self.assertEqual(method_sym.parent_symbol, "Server")

        defines = [r for r in rels if r.relation_type == "defines"]
        self.assertTrue(any(r.target_symbol == "Server" for r in defines))

        # Standalone function
        self.assertTrue(any(s.name == "NewServer" and s.symbol_type == "function" for s in syms))

    def test_cpp_ast_parsing(self):
        """Verifies high-fidelity C++ AST symbol extraction."""
        if not self.adapter.supports_extension(".cpp"):
            self.skipTest("tree_sitter_cpp grammar not installed")

        cpp_code = (
            "class Subsystem {};\n\n"
            "class Engine : public Subsystem {\n"
            "public:\n"
            "  void ignite() {\n"
            "    startFuelInjection();\n"
            "  }\n"
            "};\n\n"
            "void bootSystem() {\n"
            "  initHardware();\n"
            "}\n"
        )
        res = self.adapter.parse("engine.cpp", cpp_code)
        self.assertIsNotNone(res)
        syms, rels, chunks = res

        self.assertTrue(any(s.name == "Engine" and s.symbol_type == "class" for s in syms))
        inherits = [r for r in rels if r.relation_type == "inherits"]
        self.assertTrue(any(r.target_symbol == "Subsystem" for r in inherits))
        self.assertTrue(any(s.name == "bootSystem" and s.symbol_type == "function" for s in syms))

    def test_two_tier_fallback_when_tree_sitter_adapter_is_disabled(self):
        """Verifies that if TreeSitterAdapter is disabled, UniversalASTChunker transparently uses lexical parser."""
        ts_code = (
            "export class FallbackService {\n"
            "  run(): void {}\n"
            "}\n"
        )
        # Temporarily disable tree_sitter_adapter on chunker
        original_adapter = self.universal_chunker.tree_sitter_adapter
        try:
            self.universal_chunker.tree_sitter_adapter = None
            syms, rels, chunks = self.universal_chunker.parse_file("service.ts", ts_code)

            # Lexical parser still extracts FallbackService
            self.assertTrue(any(s.name == "FallbackService" and s.symbol_type == "class" for s in syms))
            self.assertGreater(len(chunks), 0)
        finally:
            self.universal_chunker.tree_sitter_adapter = original_adapter

    def test_two_tier_fallback_on_unsupported_language(self):
        """Verifies that unsupported or plain text files fall back to bounded chunker."""
        text_content = "This is a plain readme documentation file.\nNo code AST here.\n"
        syms, rels, chunks = self.universal_chunker.parse_file("README.txt", text_content)

        # Falls back to bounded chunking cleanly without crashing
        self.assertIsInstance(chunks, list)
        self.assertGreater(len(chunks), 0)


if __name__ == "__main__":
    unittest.main()
