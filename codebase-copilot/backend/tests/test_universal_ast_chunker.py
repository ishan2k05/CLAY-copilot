import unittest
import sys
from pathlib import Path

_backend_dir = Path(__file__).resolve().parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))
_root_dir = _backend_dir.parent
if str(_root_dir) not in sys.path:
    sys.path.insert(0, str(_root_dir))

from app.parser.universal_chunker import UniversalASTChunker
from app.parser.ast_chunker import PythonASTChunker


class TestUniversalASTChunker(unittest.TestCase):

    def setUp(self):
        self.chunker = UniversalASTChunker()

    def test_python_parity_with_native_ast(self):
        code = (
            "class AuthService:\n"
            '    """Handles authentication."""\n'
            "    def login(self, user):\n"
            "        return self.repo.verify(user)\n\n"
            'if __name__ == "__main__":\n'
            "    main()\n"
        )
        native = PythonASTChunker()
        p_syms, p_rels, p_chunks = native.parse_file("auth.py", code)
        u_syms, u_rels, u_chunks = self.chunker.parse_file("auth.py", code)

        self.assertEqual(len(p_syms), len(u_syms))
        self.assertEqual(len(p_rels), len(u_rels))
        self.assertEqual(len(p_chunks), len(u_chunks))
        self.assertEqual([s.name for s in p_syms], [s.name for s in u_syms])

    def test_typescript_class_inheritance_and_implements(self):
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
            "}\n"
        )
        syms, rels, chunks = self.chunker.parse_file("src/auth/service.ts", ts_code)

        # 1. Class symbol
        class_sym = next((s for s in syms if s.symbol_type == "class"), None)
        self.assertIsNotNone(class_sym)
        self.assertEqual(class_sym.name, "AuthService")

        # 2. Inherits and Implements relationships
        inherits = [r for r in rels if r.relation_type == "inherits"]
        implements = [r for r in rels if r.relation_type == "implements"]
        self.assertTrue(any(r.target_symbol == "BaseService" for r in inherits))
        self.assertTrue(any("IAuth" in r.target_symbol for r in implements))
        self.assertTrue(any("ILogger" in r.target_symbol for r in implements))

        # 3. Method symbol and call
        method_sym = next((s for s in syms if s.symbol_type == "method" and s.name == "AuthService.login"), None)
        self.assertIsNotNone(method_sym)
        self.assertEqual(method_sym.parent_symbol, "AuthService")

        # 4. Calls
        calls = [r for r in rels if r.relation_type == "calls"]
        self.assertTrue(any("verifyPassword" in r.target_symbol for r in calls))

    def test_typescript_interface_and_arrow_function(self):
        ts_code = (
            "export interface UserDTO extends BaseDTO {\n"
            "  id: string;\n"
            "  email: string;\n"
            "}\n\n"
            "export const hashPassword = async (raw: string): Promise<string> => {\n"
            "  const salt = generateSalt();\n"
            "  return cryptoHash(raw, salt);\n"
            "};\n"
        )
        syms, rels, chunks = self.chunker.parse_file("src/types/user.ts", ts_code)

        iface_sym = next((s for s in syms if s.symbol_type == "interface"), None)
        self.assertIsNotNone(iface_sym)
        self.assertEqual(iface_sym.name, "UserDTO")

        inherits = [r for r in rels if r.relation_type == "inherits"]
        self.assertTrue(any(r.target_symbol == "BaseDTO" for r in inherits))

        fn_sym = next((s for s in syms if s.symbol_type == "function" and s.name == "hashPassword"), None)
        self.assertIsNotNone(fn_sym)

    def test_rust_struct_traits_and_impl(self):
        rs_code = (
            "use std::sync::Arc;\n\n"
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
        syms, rels, chunks = self.chunker.parse_file("src/db.rs", rs_code)

        # Struct and Trait symbols
        self.assertTrue(any(s.name == "Database" and s.symbol_type == "struct" for s in syms))
        self.assertTrue(any(s.name == "StorageEngine" and s.symbol_type == "trait" for s in syms))

        # Implements relationship
        implements = [r for r in rels if r.relation_type == "implements"]
        self.assertTrue(any(r.target_symbol == "StorageEngine" for r in implements))

        # Function and Calls
        fn_sym = next((s for s in syms if s.name == "init_db"), None)
        self.assertIsNotNone(fn_sym)
        calls = [r for r in rels if r.relation_type == "calls"]
        self.assertTrue(any("connect_pool" in r.target_symbol for r in calls))

    def test_go_struct_interface_and_receiver_methods(self):
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
        syms, rels, chunks = self.chunker.parse_file("server.go", go_code)

        self.assertTrue(any(s.name == "Server" and s.symbol_type == "struct" for s in syms))
        self.assertTrue(any(s.name == "Handler" and s.symbol_type == "interface" for s in syms))

        # Method with receiver Server.Start
        method_sym = next((s for s in syms if s.name == "Server.Start"), None)
        self.assertIsNotNone(method_sym)
        self.assertEqual(method_sym.parent_symbol, "Server")

        # Defines relationship from Server.Start to Server
        defines = [r for r in rels if r.relation_type == "defines"]
        self.assertTrue(any(r.target_symbol == "Server" for r in defines))

        # Standalone function
        self.assertTrue(any(s.name == "NewServer" and s.symbol_type == "function" for s in syms))

    def test_cpp_class_and_inheritance(self):
        cpp_code = (
            "#include <iostream>\n\n"
            "class Engine : public Subsystem {\n"
            "public:\n"
            "  void ignite() {\n"
            "    startFuelInjection();\n"
            "  }\n"
            "};\n"
        )
        syms, rels, chunks = self.chunker.parse_file("engine.cpp", cpp_code)

        self.assertTrue(any(s.name == "Engine" and s.symbol_type == "class" for s in syms))
        inherits = [r for r in rels if r.relation_type == "inherits"]
        self.assertTrue(any(r.target_symbol == "Subsystem" for r in inherits))

    def test_java_and_csharp_class(self):
        java_code = (
            "public class UserController extends BaseController implements IController {\n"
            "  public Response handleRequest(Request req) {\n"
            "    return process(req);\n"
            "  }\n"
            "}\n"
        )
        syms, rels, chunks = self.chunker.parse_file("UserController.java", java_code)
        self.assertTrue(any(s.name == "UserController" and s.symbol_type == "class" for s in syms))
        inherits = [r for r in rels if r.relation_type == "inherits"]
        self.assertTrue(any("BaseController" in r.target_symbol or "IController" in r.target_symbol for r in inherits))

    def test_malformed_syntax_fallback_does_not_crash(self):
        broken_code = "class Incomplete {\n  void broken( {\n"
        syms, rels, chunks = self.chunker.parse_file("broken.ts", broken_code)
        # Should not throw exception and should return chunks
        self.assertIsInstance(chunks, list)


if __name__ == "__main__":
    unittest.main()
