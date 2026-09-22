# Codebase Copilot

> **AI Repository Intelligence & Autonomous Coding Agent Platform**
> 
> *Frontend:* TypeScript + VS Code Webview API ("Warm Minimalist Canvas" UI)  
> *Backend:* Python FastAPI + Structural AST & Tree-sitter  
> *Reasoning Engine:* **`gemma4:31b-cloud`** via local Ollama Daemon (`http://localhost:11434`)  
> *Auth & Access:* Zero API keys required — authenticated seamlessly via your signed-in Ollama account!  
> *Security:* Zero-Leak Privacy Guard (regex secret & token scrubber + sensitive file filter)  
> *Knowledge Store:* SQLite (Relational Call Graph) + Local Vector Store (RRF Hybrid Search)

---

## 📖 Master Specification
The complete architectural blueprint, hybrid RAG algorithms, unified diff patch engine, and implementation roadmap are documented in detail in:

👉 **[CODEBASE_COPILOT_SPEC.md](file:///home/ishan/.gemini/antigravity-ide/scratch/codebase-copilot/CODEBASE_COPILOT_SPEC.md)**

---

## ⚡ Quick Start: Running with Ollama `gemma4:31b-cloud`

### 1. Verify Ollama Model
Confirm that your signed-in Ollama daemon has `gemma4:31b-cloud` available:
```bash
ollama list
# NAME               ID             SIZE   MODIFIED
# gemma4:31b-cloud   ef09f235533c   -      34 minutes ago
```

### 2. Run Backend Tests
```bash
cd backend
python3 -m unittest discover -s tests -p "test_*.py"
# Ran 6 tests in 0.028s ... OK
```

### 3. Open in VS Code
Open the project directory:
```bash
code /home/ishan/.gemini/antigravity-ide/scratch/codebase-copilot
```
Open [`extension/preview.html`](file:///home/ishan/.gemini/antigravity-ide/scratch/codebase-copilot/extension/preview.html) in any browser to see the interactive Warm Minimalist Canvas UI.

---

## 🏗️ Project Monorepo Layout
```
codebase-copilot/
├── extension/                          # TypeScript VS Code Extension
│   ├── package.json                    # Extension manifest & commands
│   ├── tsconfig.json                   # TypeScript configuration
│   ├── preview.html                    # Standalone browser UI preview
│   └── src/
│       ├── extension.ts                # Entrypoint & sidecar process launcher
│       ├── auth/OllamaManager.ts       # Ollama daemon connectivity checker
│       ├── api/backendClient.ts        # SSE streaming client to FastAPI
│       ├── server/processManager.ts    # Sidecar lifecycle manager
│       └── webview/
│           ├── SidebarProvider.ts      # Webview View Provider
│           └── media/
│               ├── style.css           # Warm Minimalist Canvas theme
│               └── main.js             # Interactive 5-tab UI controller
│
└── backend/                            # Python Intelligence Engine
    ├── requirements.txt                # Dependencies
    ├── app/
    │   ├── main.py                     # FastAPI gateway (CORS, /api/chat, /scan, /graph/mermaid)
    │   ├── security/secret_masker.py   # Zero-Leak Privacy Guard (regex secret scrubber)
    │   ├── parser/
    │   │   ├── symbols.py              # Symbol & relationship dataclasses
    │   │   └── ast_chunker.py          # Python AST code-aware chunker
    │   ├── index/
    │   │   ├── graph_store.py          # SQLite embedded relational graph
    │   │   └── vector_store.py         # Vector embeddings & cosine similarity
    │   ├── retrieval/hybrid_search.py  # Reciprocal Rank Fusion (RRF) engine
    │   ├── agent/
    │   │   ├── planner.py              # Multi-step task planner
    │   │   ├── patch_engine.py         # Unified diff validator & patcher
    │   │   ├── repair_loop.py          # Self-healing test & repair loop
    │   │   └── tools/terminal_agent.py # Sandboxed test runner
    │   └── llm/gemma_provider.py       # Ollama gemma4:31b-cloud client
    └── tests/
        ├── test_ast_chunker.py         # AST chunking & privacy tests
        └── test_hybrid_and_repair.py   # RRF, patch, and terminal tests
```
