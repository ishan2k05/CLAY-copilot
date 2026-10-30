<p align="center">
  <img src="https://raw.githubusercontent.com/ishan2k05/CLAY-copilot/main/codebase-copilot/extension/resources/icon.png" width="96" height="96" alt="CLAY Copilot Logo" />
</p>

# CLAY Copilot
### Autonomous Repository Intelligence & Coding Agent Platform for VS Code

[![Platform: Windows](https://img.shields.io/badge/Platform-Windows%20(win32--x64)-0078D6?logo=windows&logoColor=white)](https://github.com/ishan2k05/CLAY-copilot/releases)
[![Version: v2.7.20](https://img.shields.io/badge/Version-v2.7.20-C15C3D.svg)](extension/package.json)
[![Runtime: FastAPI](https://img.shields.io/badge/Backend-FastAPI%20Sidecar-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Parser: Tree--Sitter](https://img.shields.io/badge/Parser-Tree--Sitter%20AST-brightgreen)](https://tree-sitter.github.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Zero Setup](https://img.shields.io/badge/Setup-Zero--Dependency%20Binary-orange)](https://github.com/ishan2k05/CLAY-copilot)

**CLAY Copilot** is a high-performance repository intelligence platform and autonomous software engineering agent built directly into VS Code. Powered by modern LLMs (via local or cloud Ollama), a multi-language **Tree-Sitter AST engine**, an embedded **SQLite relational call graph**, and a **4-stream hybrid RAG retrieval pipeline**, CLAY Copilot navigates, explains, plans, and surgically refactors codebases of any size.

Unlike extensions that require complex Python installations, virtual environments, or external SaaS dependencies, CLAY Copilot ships as a **self-contained standalone application sidecar** (`clay-server.exe`) -- download, click install, and start coding immediately with zero setup.

---

## What's New in Release 2.7.20

Release `2.7.20` is a major stability and feature update, introducing multi-file diff orchestration, expanded context token ceilings, interactive reasoning traces, and essential engine-hardening fixes.

### 1. Multi-File Diff & Granular Hunk Staging
- **Multi-File Tabbed Reviewer:** When the agent modifies multiple files in a complex refactor, all touched files are grouped into a responsive multi-file selector.
- **Granular Hunk-Level Actions:** Review individual change hunks (`+` additions, `-` deletions) per file. Stage or unstage specific hunks before committing changes to disk.
- **AST Rebase & Self-Healing Safety:** Validates syntax integrity across all modified files before writing to disk, catching misaligned indentation and patch conflicts automatically.

### 2. Dynamic Reasoning Trace & Interactive `<think>` Drawer
- **Chain-of-Thought Visualizer:** Native rendering for reasoning models (DeepSeek-R1, Qwen-Reasoning) inside a dedicated, collapsible **Thought Process** drawer with live stopwatch metrics.
- **CSP-Compliant Event Delegation:** Completely rebuilt thinking drawer toggle logic with secure DOM event delegation, eliminating Content Security Policy blocks in VS Code webviews.
- **60fps Smooth Streaming:** RAF-throttled token rendering with discrete terminal pulse animations.

### 3. Massive Context Window & Generation Scale
- **Up to 1M Tokens Context Window:** Added support for deep-context models with dropdown options spanning **2k, 4k, 8k, 16k, 32k, 64k, 128k, 256k, 512k, and 1,048,576 tokens (1M)**.
- **Extended Output Prediction:** Configurable max generation limits up to **64k tokens (65,536 tokens)** for large multi-file code synthesis without truncation.

### 4. 3-Way Merge Conflict Guard & Context Actions
- **Concurrent Disk Conflict Warning:** Real-time detection of external disk edits with warning banners to prevent overwriting active changes.
- **VS Code Context Menu Integration:** Right-click directly inside any editor window to execute:
  - `CLAY Copilot: Explain Selected Code`
  - `CLAY Copilot: Fix Current Error`
  - `CLAY Copilot: Ask Question`

### 5. Critical Bug Fixes & Engine Hardening
- **BUG 1 (Vector Store Sibling File Retention):** Fixed an issue where editing a file purged embeddings for all sibling files sharing its basename (e.g. `__init__.py`, `index.ts`, `auth.py`). Vector store now strictly matches exact canonical normalized paths (`WHERE file_path IN (...)`).
- **BUG 3 (CSP Inline Script Block in Webview):** Removed inline `onclick` handlers on historical thought process drawers, resolving CSP security rejections.
- **BUG 4 (Chat UI Stream Error Freeze):** Handled streaming connection drops by dispatching `streamComplete`, preventing the chat UI from locking permanently in the "Generating..." state.
- **BUG 5 (SSE Chunk Packet Fragmentation):** Added a persistent carry-over buffer in `backendClient.ts` to seamlessly handle JSON tokens split across TCP/SSE packet boundaries.
- **BUG 8 (Mermaid Syntax Diagram Failures):** Sanitized node identifiers to pure alphanumeric characters (`node_[id]`) and quoted display labels, eliminating diagram render failures on kebab-cased or hyphenated symbols.

### 6. UI & UX Refinements
- **Claude-Inspired Obsidian Dark Palette:** Polished high-contrast dark theme with warm terracotta accents and pixel-prompt cursor animation.
- **Unified Dynamic Controller:** Removed outdated placeholders, streamlined empty states, and refreshed font typography.
- **Multi-Instance Loopback Isolation:** Dynamic loopback port negotiation (`8765-8785`) with cryptographic session token authentication (`X-Clay-Auth-Token`) ensures multiple IDE windows never collide.

---

## Table of Contents

- [Core Highlights](#core-highlights)
- [The 5 Integrated Workspaces](#the-5-integrated-workspaces)
- [Under the Hood: Dual-Pillar Architecture](#under-the-hood-dual-pillar-architecture)
- [Zero-Dependency Setup (Windows win32-x64)](#zero-dependency-setup-windows-win32-x64)
- [Quick Start & Installation](#quick-start--installation)
- [Key Workflows & Usage](#key-workflows--usage)
- [Security & Zero-Leak Privacy Guard](#security--zero-leak-privacy-guard)
- [Development & Contributing](#development--contributing)
- [License](#license)

---

## Core Highlights

- **Zero-Dependency One-Click Install:** Bundled with a native compiled binary (`clay-server.exe`). No Python, pip, or virtual environment configuration required.
- **Embedded Multi-Language Tree-Sitter Engine:** Native AST parsing for **Python, TypeScript, React TSX, JavaScript, Rust, Go, and C/C++** with zero external compiler dependencies.
- **Two-Tier Autonomous Agent:** Dynamic LLM task decomposer that plans multi-step tasks (`scan` -> `prep` -> `synthesize` -> `validate` -> `verify`), executes native terminal processes, and handles live token streaming.
- **Automated 1-Shot Self-Healing Repair:** Catches AST syntax errors, misaligned indentation, and malformed diffs, automatically invoking an internal repair loop to heal the patch before staging.
- **4-Stream RRF Hybrid Search Engine:** Blends SQLite FTS5 BM25 text search, packed binary vector embeddings, structural AST symbols, and relational call graph hops via Reciprocal Rank Fusion.
- **Zero-Leak Privacy Guard:** Client-side regex engine that strips API keys, OAuth tokens, private `.pem` certificates, and `.env` credentials before prompts leave your machine.
- **Multi-Instance Workspace Isolation:** Run multiple CLAY windows concurrently across separate projects with automated dynamic loopback port allocation (`8765-8785`), workspace handshake verification, and isolated per-window lifecycles.
- **Isolated State Management:** User settings persist safely in VS Code's User Application Storage across updates, while repository call graphs remain strictly isolated per workspace.

---

## The 5 Integrated Workspaces

CLAY Copilot provides a custom, responsive, **Warm Minimalist Canvas** sidebar interface divided into five dedicated workspaces:

### 1. Chat Workspace (Conversational Intelligence & Smart Mentions)
- **Token-Budgeted Rolling Compaction:** Multi-turn session memory split into Tier 1 (recent turn buffers) and Tier 2 (rolling semantic summary) to preserve token budget.
- **Smooth Streaming & Reasoning Trace:** Interactive collapsible thinking drawer showing internal reasoning steps with duration counters.
- **Smart Mention Chips:** Type `@` to dynamically mention and pin files (`@app.py`), structural symbols (`@UserService`), or directories (`@src/auth`).
- **Interactive Line Citation Pills:** Responses output clickable line citations (e.g., `[auth.py:L14-L32]`) that jump directly to the code in your active editor.

<p align="center">
  <img src="https://raw.githubusercontent.com/ishan2k05/CLAY-copilot/main/docs/images/02_chattab.png" width="850" alt="CLAY Copilot - Conversational Reasoning & Streaming" />
</p>

#### Smart Context Engine (`@` Mentions)
Pin target symbols, classes, or files directly into the prompt context with fuzzy search:

<p align="center">
  <img src="https://raw.githubusercontent.com/ishan2k05/CLAY-copilot/main/docs/images/07_function_mention.png" width="850" alt="CLAY Copilot - Smart Mention Autocomplete" />
</p>

---

### 2. Agent Workspace (Autonomous Multi-Step Execution)
- **Dynamic Task Decomposer:** Analyzes user instructions and generates tailored multi-step execution plans with real-time status badges (`[+] Done`, `[>] Active`, `[-] Queued`).
- **Live Terminal Logging:** Embedded subshell monitor displaying real-time compiler, build, and agent execution logs.
- **Interactive Ambiguity Resolution:** When a target file is ambiguous across multiple candidates, the agent pauses, renders selection chips, and resumes immediately upon selection.
- **One-Click Stop & Resume:** Non-blocking abort triggers that cleanly terminate child processes and terminal sessions without freezing the editor.

<p align="center">
  <img src="https://raw.githubusercontent.com/ishan2k05/CLAY-copilot/main/docs/images/03_agenttab.png" width="850" alt="CLAY Copilot - Autonomous Agent Workspace & Terminal Stream" />
</p>

---

### 3. Repo Workspace (Repository Map & Symbol Graph)
- **Universal AST Symbol Navigator:** Structural hierarchy listing classes, methods, functions, interfaces, entrypoints, and imports across all supported languages.
- **Interactive SVG Architecture Diagrams:** Generates and renders Mermaid diagrams (`flowchart LR`, `classDiagram`, `sequenceDiagram`) directly inside an interactive SVG card with drag-to-pan, 30% to 500% zoom, and Fit-to-Window controls.
- **1-Click Editor Jump:** Clicking any symbol in the repository map instantly navigates to its exact definition in the active editor.

<p align="center">
  <img src="https://raw.githubusercontent.com/ishan2k05/CLAY-copilot/main/docs/images/05_repotab.png" width="850" alt="CLAY Copilot - Repository AST Symbol Tree" />
</p>

#### Interactive Architecture & Class Diagrams
Inspect dependencies, call flows, and module hierarchies visually:

<p align="center">
  <img src="https://raw.githubusercontent.com/ishan2k05/CLAY-copilot/main/docs/images/08_diagrameg.png" width="850" alt="CLAY Copilot - Interactive Architectural Diagrams" />
</p>

---

### 4. Diff Workspace (Surgical Multi-File Reviewer)
- **Multi-File Tabbed Selector:** Seamlessly switch between all modified files in complex multi-file refactorings.
- **Hunk-by-Hunk Staging:** Review syntax-highlighted additions (`+`) and deletions (`-`). Stage or discard specific hunks individually.
- **1-Click Patch Application:** Accept and apply surgical changes directly to disk with automated AST safety verification.

---

### 5. Settings Workspace (Configuration & Guardrails)
- **Theme & IDE Bindings:** Toggle between the signature Warm Minimalist CLAY Palette, Claude Obsidian Dark, and live VS Code token inheritance.
- **Dynamic Model Selector:** Seamlessly switch between local models (Gemma, Llama, Qwen, DeepSeek) and cloud endpoints without restarting.
- **Massive Context Scaling:** Sliders and dropdowns for context window (`num_ctx` up to 1,048,576 tokens) and output prediction limits (`num_predict` up to 64k tokens).
- **Security Sandboxing:** Toggle between **Sandboxed** (workspace-confined), **Strict**, and **Open** execution modes.

---

## Under the Hood: Dual-Pillar Architecture

CLAY Copilot uses a decoupled client-server architecture communicating via authenticated, low-latency loopback HTTP and Server-Sent Events (SSE):

```
VS Code Extension Host (TypeScript)
│
├── SidebarProvider (Webview Canvas UI)
│   ├── main.js (Chat, Agent, Repo Map, Multi-Diff Reviewer, Settings)
│   └── style.css (Warm Minimalist & Claude Obsidian Themes)
│
├── ProcessManager (Sidecar Lifecycle Manager)
│   ├── Dynamic Multi-Instance Port Allocation (8765 -> 8785)
│   ├── Workspace-Aware Health Handshake & Ownership Guard
│   ├── Zombie Port Reclamation & Process Tree Killer (taskkill /T /F)
│   └── Storage Path Resolver (globalStorageUri & storageUri)
│
└── BackendClient (EventSource SSE & JSON Client)
        │  (HTTP / SSE on 127.0.0.1:<allocated_port> + Session Token Auth)
        ▼
Bundled Standalone Server (clay-server.exe)
│
├── FastAPI Application Gateway (Uvicorn Engine)
│   ├── /api/chat (SSE Streaming Token Generator)
│   ├── /api/agent/plan (LLM Dynamic Decomposer)
│   ├── /api/agent/step/stream (Step Execution & Multi-Diff Builder)
│   └── /api/graph/mermaid (Relational Dependency Graph)
│
├── Tree-Sitter & Universal AST Chunker (Python, TS, JS, Rust, Go, C/C++)
├── 4-Stream RRF Hybrid Search Engine (SQLite BM25 + Vector BLOBs)
├── Multi-File Patch Engine & 1-Shot Self-Healing Syntax Repair Loop
└── Zero-Leak Privacy Guard (Secret & Token Sanitizer)
```

---

## Zero-Dependency Setup (Windows win32-x64)

CLAY Copilot is compiled as a native PE binary for Windows (`win32-x64`). It embeds:
- Standalone Python 3 Runtime
- Uvicorn & FastAPI Asynchronous Web Server
- Tree-Sitter Grammars (Python, TypeScript, JavaScript, Rust, Go, C/C++)
- SQLite3 Engine with FTS5 Full-Text Search
- Pydantic, HTTPX, and Unidiff Libraries

### Decoupled State & Data Hygiene:
- **Global Settings (`runtime_settings.json`):** Persisted in `%APPDATA%\Code\User\globalStorage\codebase-copilot\`. Upgrading or reinstalling the extension will **never** wipe your configuration.
- **Code Graph Database (`code_graph.sqlite`):** Isolated within VS Code's per-workspace storage folder. **No temporary `.sqlite` files are ever written to your git repository.**

---

## Quick Start & Installation

### Option A: Install from VS Code Marketplace
1. Search for **`CLAY Copilot`** in the Extensions view (`Ctrl+Shift+X`).
2. Click **Install**.

### Option B: Install from .vsix (Pre-release / Local)
1. Download the latest `codebase-copilot-win32-x64.vsix` from [Releases](https://github.com/ishan2k05/CLAY-copilot/releases).
2. In VS Code, press `Ctrl+Shift+P` -> type **`Extensions: Install from VSIX...`** -> select the file.
   *Or install via terminal:*
   ```powershell
   code --install-extension codebase-copilot-win32-x64.vsix --force
   ```
3. Reload VS Code (`Ctrl+Shift+P` -> `Developer: Reload Window`).

### Option C: Ensure Ollama is Running
CLAY Copilot connects seamlessly to your local or cloud Ollama instance:
1. Make sure Ollama is installed and running (`ollama serve`).
2. Pull your preferred coding model, e.g.:
   ```bash
   ollama pull qwen2.5-coder:7b
   ```
3. Open the CLAY Copilot sidebar -- the status indicator will illuminate **Live** with latency metrics.

---

## Key Workflows & Usage

### 1. Asking Repository Questions with Mentions
In the **Chat** tab, ask complex architectural or codebase questions:
```text
How does our authentication handshake work? Compare @auth/verifier.ts and @server/processManager.ts.
```
CLAY Copilot resolves the file ASTs, fetches related call graphs via hybrid search, and streams a response with clickable line citation pills.

### 2. Autonomous Multi-Step Refactoring
In the **Agent** tab, specify an objective:
```text
Create a new folder 'api/v2', add an authentication route handler in auth.py, and write a pytest suite for it.
```
1. **Plan:** The LLM decomposes the goal into structured sub-steps.
2. **Execute:** The agent scaffolds directories, synthesizes code, and executes syntax checks.
3. **Review:** Click the **Diff** tab to inspect the multi-file changes and click **Apply to Disk**.

---

## Security & Zero-Leak Privacy Guard

CLAY Copilot was built from the ground up for strict privacy and enterprise compliance:
- **Client-Side Secret Masking:** Automatically sanitizes AWS access keys, GitHub tokens, JWTs, private keys, database connection strings, and `.env` pairs using compiled regex patterns before dispatching context to the model.
- **Custom Exclusion Filters:** Specify custom glob patterns in Settings (e.g. `*.key, *.pem, credentials.json, id_rsa`) to exclude sensitive files from indexing.
- **Loopback-Only Binding:** The local sidecar binds strictly to `127.0.0.1` and validates all incoming requests against a cryptographically secure 256-bit session token (`X-Clay-Auth-Token`).

---

## Development & Contributing

### Building from Source:

1. **Clone the repository:**
   ```bash
   git clone https://github.com/ishan2k05/CLAY-copilot.git
   cd CLAY-copilot
   ```

2. **Setup the Python Backend:**
   ```powershell
   cd codebase-copilot\backend
   python -m venv .venv
   .\.venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Run Backend in Development Mode:**
   ```powershell
   python -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
   ```

4. **Setup and Debug the Extension:**
   ```powershell
   cd ..\extension
   npm install
   npm run compile
   ```
   Open the `codebase-copilot` folder in VS Code and press **`F5`** to launch the Extension Development Host.

5. **Package Windows VSIX:**
   ```powershell
   # 1. Compile backend executable
   cd ..\backend
   pyinstaller clay-server.spec --clean
   Copy-Item dist\clay-server.exe -Destination ..\extension\bin\clay-server.exe

   # 2. Package extension
   cd ..\extension
   npm run package:win32
   ```

---

## License

CLAY Copilot is licensed under the [MIT License](LICENSE).
