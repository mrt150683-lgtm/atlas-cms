# Codebase Memory System (CMS) — Design Spec & Build Plan

**Version:** 1.0  
**Date:** July 2026  
**Purpose:** A self-bootstrapping, clean, AI-accessible memory layer for any codebase.  
**Goal:** Give AI models/agents a true "understanding map" of a codebase — not just grep, but structural knowledge + lightweight semantic summaries — so they can reason about where things are, how they connect, and what they do before (or instead of) reading raw code.

This document is written to be copied or transferred directly into a new chat for iterative design and building.

---

## 1. Vision & Core Idea

You want something that:

1. **Programmatically** parses a directory → builds a **knowledge graph** of statements, functions, classes, variables, imports, calls, and data/control flow.
2. **AI layer** then analyses the structure and produces **lightweight "low-resolution" text summaries** for each significant piece (files, functions, classes, key blocks).
3. Summaries live **inside the graph nodes** (and are also exported to readable files).
4. The whole thing stays **clean** — it only sees *your* source code, never node_modules, venv, build artifacts, __pycache__, etc.
5. It is **self-referential**: the tool analyses its own directory first, produces its own memory layer, and can use that memory to help build/improve future versions.
6. The output is **AI-consumable** — an agent can query the memory layer ("where is the directory scanning logic?") and get precise answers with file paths, line ranges, connections, and summaries — far better context than raw grep.
7. Optional future: feels like a **custom smart IDE / enhanced VS Code** experience (file tree + memory graph + summaries + surgical code access).

This is **not** another full RAG-over-codebase tool. It is a **structural + semantic map** that sits *over* the code, stays small, and stays up-to-date.

---

## 2. Why This Is Powerful (The "Why" for AI Agents)

Current problem with agents + large codebases:
- They can grep, but they don't *know where to look* or *why* something exists.
- Full file reads blow up context.
- They lose the "big picture" of how modules connect.

With CMS:
- Agent first queries the **graph + summaries** (tiny context).
- Gets back: "The auth logic is in `src/auth/router.py` lines 42-89. It defines `verify_token()` which is called by 7 other places. Summary: middleware that checks JWT and attaches user to request. Depends on `src/auth/jwt.py`."
- Then agent can decide to read only the exact relevant raw snippet if needed.
- Result: dramatically better reasoning, fewer hallucinations about code structure, and the ability to work on large or unfamiliar codebases.

---

## 3. High-Level Architecture

```
Input: any directory (starts with its own)
        │
        ▼
┌───────────────────────┐
│ 1. Clean Directory    │  ← ignores junk, produces filtered tree
│    Scanner            │
└───────────┬───────────┘
            │ clean_tree.json + clean_tree.md
            ▼
┌───────────────────────┐
│ 2. Programmatic       │  ← AST / tree-sitter parsing
│    Graph Builder      │     Nodes + edges (calls, imports, defines)
└───────────┬───────────┘
            │ graph.json (with empty summaries)
            ▼
┌───────────────────────┐
│ 3. AI Summary         │  ← lightweight structural descriptions
│    Generator          │     (low-res + line numbers + connections)
└───────────┬───────────┘
            │ summaries written into graph nodes
            ▼
┌───────────────────────┐
│ 4. Memory Exporter    │  ← .memory/ folder with human + AI readable files
│    & Indexer          │
└───────────┬───────────┘
            │
            ▼
┌───────────────────────┐
│ 5. Query Interface    │  ← CLI + Python API for agents
│    (for AI models)    │     "find where X happens" → relevant nodes + summaries
└───────────────────────┘
```

All output lives inside the target project (or a `.memory/` subfolder) so it travels with the code and can be committed or used by agents.

---

## 4. Clean Directory Scanner (Phase 1 priority)

**Requirements:**
- Recursive scan starting from a root path.
- **Strict filtering** so only real source is included.
- Configurable via a simple list or `.cmsignore` file (gitignore-style).
- Outputs a beautiful, AI-readable `clean_tree.md` and machine `clean_tree.json`.

**Recommended ignore patterns (starter set for Python + general projects):**

```ignore
# Version control & VCS
.git
.gitignore
.gitattributes

# Python
__pycache__
*.pyc
*.pyo
*.pyd
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg
venv/
.venv/
env/
.env/
ENV/
env.bak/
venv.bak/
.mypy_cache/
.pytest_cache/
.coverage
htmlcov/
.tox/
.nox/
.pytype/

# Node / JS / TS
node_modules/
npm-debug.log*
yarn-debug.log*
yarn-error.log*
.pnp/
.pnp.js
.yarn/
bower_components/

# Build / output
dist/
build/
out/
*.min.js
*.map
.next/
.nuxt/
.cache/

# IDE / editor
.vscode/
.idea/
*.swp
*.swo
*~
.project
.classpath
.settings/
*.sublime-project
*.sublime-workspace

# OS
.DS_Store
Thumbs.db
desktop.ini

# Logs & temp
*.log
*.tmp
*.temp
logs/
tmp/
temp/

# The memory system itself (optional — decide per project)
.memory/
.cms/
```

**Allowed / focus extensions (configurable):**
`.py`, `.md`, `.txt`, `.json`, `.yaml`, `.yml`, `.toml`, `.ini`, `.cfg`, `.sh`, `.bash`, `.js`, `.ts`, `.jsx`, `.tsx`, `.html`, `.css`, `.scss` (add more as needed).

The scanner should also record for each file:
- relative path
- absolute path (internal)
- file size (bytes)
- line count
- last modified timestamp
- language (guessed from extension)

This clean tree becomes the single source of truth for "what belongs to this codebase".

---

## 5. Knowledge Graph Structure

**Node types (start simple, expand later):**

- `File` — path, language, line_count, summary, last_analysed
- `Module` / `Package`
- `Function` / `Method` — name, signature, start_line, end_line, docstring, summary, calls, called_by
- `Class` — name, bases, start_line, end_line, methods, summary
- `Variable` / `Constant` (optional, only important ones)
- `Import` / `FromImport` (can be edges instead)

**Edge types:**
- `IMPORTS` (file → file or module)
- `CALLS` (function → function)
- `DEFINES` (file/class → function/class)
- `INHERITS` (class → class)
- `REFERENCES` (variable usage, etc.)
- `CONTAINS` (file → functions/classes inside it)

**Storage options (choose one for v1):**
1. **Recommended for start:** `networkx.DiGraph` serialized to `graph.json` (human-inspectable, portable, zero extra deps beyond networkx).
2. Later: SQLite with simple tables (nodes + edges) for better persistence and querying without loading everything into RAM.
3. Future: Neo4j or Kuzu if you want heavy graph queries.

Each node can carry a `summary` field (string) + `summary_meta` (lines, key_connections, confidence, generated_at).

---

## 6. AI Summary Generator — The "Low Resolution" Layer

This is the secret sauce.

For every significant node (File + top-level Function/Class), the system calls an LLM with a carefully engineered prompt that produces a **concise structural description**.

**Example prompt template (tune this):**

```
You are an expert senior software architect creating a LOW-RESOLUTION structural map of code for another AI agent.

The goal is to give the agent a fast, accurate "mental model" of the codebase so it knows where to look and what things do, without needing to read every line.

File: {relative_path}
Language: {language}
Total lines: {line_count}

Here is the source:

```python
{source_code}   # or truncated intelligently if very long
```

Task:
Produce a short, dense summary with these exact sections:

1. **File Purpose** (1-2 sentences max)
2. **Key Components** (for each important top-level function, class, or block):
   - Name + line range (e.g. `def process_directory() 45-112`)
   - One-sentence intent
   - Main control flow notes (loops, conditionals, key variables)
   - What it calls or depends on (from this file or imports)
3. **Important Connections**
   - Files/modules this imports or is imported by
   - Key data flow or shared state

Rules:
- Stay low-resolution. Focus on *why* and *how it fits*, not implementation details.
- Use relative line numbers.
- Be brutally concise. Total output should fit in ~15-25 lines for most files.
- If something is boilerplate or obvious, say so briefly.
- Never invent behaviour not visible in the provided code.
```

The generated text is stored directly on the graph node under `summary`.

You can also export individual `.md` files under `.memory/summaries/` for easy human reading or extra grep.

---

## 7. Output Directory Structure (inside the analysed project)

```
your-project/
├── .memory/                     # ← the memory layer lives here
│   ├── clean_tree.md            # beautiful filtered directory tree (human + AI readable)
│   ├── clean_tree.json          # machine version with metadata
│   ├── graph.json               # full knowledge graph + all summaries embedded
│   ├── index.md                 # quick start + how to query
│   └── summaries/               # optional per-file detailed summaries
│       ├── src/
│       │   ├── main.py.md
│       │   └── auth/
│       │       └── router.py.md
│       └── cms/
│           └── scanner.py.md
│
├── src/                         # your real source (only this gets analysed)
├── cms/                         # the memory tool's own code (self-analysis)
├── pyproject.toml
└── README.md
```

**Important design decision:**
- The `.memory/` folder can be **gitignored** (if you don't want to commit the generated layer) **or committed** (so agents always have the latest map when they clone).
- The scanner should be able to detect if `.memory/` already exists and do incremental updates (only re-process changed files).

---

## 8. Query Interface for AI Models / Agents

This is how external AIs actually *use* the memory.

**CLI (v1):**
```bash
cms query "where is the directory scanning and filtering logic?"
cms query "find all places that handle file ignores or clean tree building"
cms query "show me the graph neighbourhood around the AI summary generator"
```

**Python API (for your agents / Hermes / other tools):**
```python
from cms import CodebaseMemory

mem = CodebaseMemory.load(".memory/graph.json")

results = mem.query_intent(
    "find the code responsible for building the clean directory tree and applying ignore patterns",
    top_k=5,
    include_summaries=True,
    include_connections=True
)

for node in results:
    print(node.path, node.lines, node.summary)
    print("Called by:", node.called_by)
```

**Later enhancements:**
- Semantic search over summaries (embeddings)
- Graph traversal queries ("show everything that imports scanner.py")
- "Explain this feature" that walks the relevant subgraph + summaries

---

## 9. Self-Bootstrapping & Meta Loop

The killer feature:

1. You point CMS at its own directory (`cms/` or the whole project).
2. It produces `.memory/` containing `clean_tree.md`, `graph.json`, and summaries of its own scanner, graph builder, summary generator, etc.
3. Future versions of CMS can **read its own `.memory/` first** to understand its structure before making changes.
4. You get a virtuous cycle: better memory → better code → better memory.

This is why storing summaries *in the graph nodes* and also exporting readable files is powerful.

---

## 10. Tech Stack Recommendations (Keep It Lightweight)

**v1 (recommended — minimal deps, fast to build):**
- Language: Python 3.11+
- Parsing: Start with built-in `ast` module for Python files (very fast). Add `tree-sitter` + language grammars later for multi-language.
- Graph: `networkx` (easy, powerful, JSON export)
- Ignore patterns: `pathspec` library (excellent gitignore-style matching) or simple fnmatch + custom walker.
- CLI: `typer` (beautiful, modern) or `argparse`
- LLM for summaries: Your choice — local via Ollama / MLX / LM Studio, or Grok API, OpenAI, etc. Make it configurable.
- Storage: JSON files + optional SQLite later.

**No heavy frameworks needed at the start.**

**For the "custom VS Code" feel later:**
- **Best open-source starting point:** [VSCodium](https://vscodium.com/) (VS Code without Microsoft telemetry + full extension ecosystem).
- **Modern alternative:** [Zed](https://zed.dev/) — fast, native, Rust, excellent extension support.
- **AI coding extension you can learn from / extend:** [Continue.dev](https://continue.dev/) (open source autopilot with codebase RAG — very close in spirit).
- **Ground-up lightweight option:** 
  - TUI: [Textual](https://textual.textualize.io/) + Rich (you can build a very nice terminal IDE quickly).
  - Web: FastAPI backend + Monaco Editor (the same editor core as VS Code) in the frontend. This gives you a "custom VS Code" web experience with full control.

**Recommendation for this project:**
Start **pure Python CLI + JSON/Markdown outputs** (Phase 1-4).  
Once the memory layer is proven valuable, *then* decide whether to wrap it in a Textual TUI, a small web UI with Monaco, or a VSCodium extension.  
Don't build a full editor until the core memory engine is solid and self-hosting.

---

## 11. Phased Implementation Plan (Ready for New Chat)

Copy this document into a new chat and say:

> "Let's build the Codebase Memory System step by step using this spec. Start with Phase 1."

**Phase 0 — Spec Refinement (current)**
- Review and tweak this document together.
- Decide on exact output folder name (`.memory/` vs `.cms/` vs `.codebase-memory/`).
- Choose initial tech (pure `ast` + networkx vs tree-sitter early).

**Phase 1 — Clean Directory Scanner + Tree Output (1-2 focused sessions)**
- Implement recursive walker with ignore patterns + allowed extensions.
- Output `clean_tree.md` (nice formatted tree) and `clean_tree.json`.
- Make it analyse its own directory first as the primary test.
- Add file metadata (lines, size, mtime).

**Phase 2 — Basic Knowledge Graph Builder (Python files first)**
- Use `ast` to extract functions, classes, imports, and basic call relationships.
- Build NetworkX DiGraph.
- Serialize to `graph.json` (nodes carry basic attributes, summaries empty for now).
- Visualise the graph (optional: graphviz or simple text output).

**Phase 3 — AI Summary Generator**
- Create prompt template for low-resolution summaries.
- Wire up LLM call (start with whatever you have access to — local or API).
- Generate summaries for File and top-level Function/Class nodes.
- Store summaries in graph nodes + export readable `.md` files under `.memory/summaries/`.
- Re-run on its own code and inspect the quality of the summaries.

**Phase 4 — Query Interface + CLI**
- Build simple intent-based query over the graph (keyword + structural).
- CLI commands: `scan`, `build-graph`, `generate-summaries`, `query`, `update`.
- Python API class that agents can import.
- Test: give the memory to another instance of Grok / your agent and ask it to explore the CMS codebase using only the memory layer.

**Phase 5 — Incremental Updates & Polish**
- mtime-based change detection.
- Only re-parse and re-summarise changed files.
- Handle the `.memory/` folder itself gracefully (include or exclude?).
- Add basic tests.

**Phase 6+ — Power-ups (only after core works)**
- Multi-language via tree-sitter.
- Embeddings + semantic search on summaries.
- GraphRAG-style traversal.
- Simple TUI or web UI (Monaco + graph viz).
- VS Code / VSCodium extension that shows the memory panel.
- Integration as a tool for your other agents (Hermes, etc.).

---

## 12. Example Starter Code Snippets (for Phase 1)

You can paste these into the new chat when we start coding.

**Basic clean walker skeleton:**
```python
import os
from pathlib import Path
import pathspec

def load_ignore_spec(root: Path) -> pathspec.PathSpec:
    # load .cmsignore or use built-in defaults + user config
    ...

def should_include(path: Path, spec: pathspec.PathSpec) -> bool:
    ...

def build_clean_tree(root: Path) -> dict:
    spec = load_ignore_spec(root)
    tree = {}
    for dirpath, dirnames, filenames in os.walk(root):
        # prune dirnames in-place for efficiency
        dirnames[:] = [d for d in dirnames if should_include(Path(dirpath) / d, spec)]
        for f in filenames:
            p = Path(dirpath) / f
            if should_include(p, spec):
                # collect metadata
                ...
    return tree
```

We will flesh this out properly in the build chat.

---

## 13. Success Criteria (How We Know It's Working)

After Phase 4 you should be able to:

1. Run the tool on its own directory and get a sensible `clean_tree.md` with zero junk files.
2. Open `graph.json` and see connected nodes for files → functions with correct relationships.
3. Read the generated summaries and think "yes, that accurately captures the intent and structure at a useful abstraction level".
4. Ask an AI (in a fresh chat) to analyse the CMS codebase **using only the `.memory/` artifacts** and have it correctly identify where key logic lives, without hallucinating structure.
5. The system feels lightweight, fast, and stays in sync with code changes.

---

## 14. Open Questions (to resolve in new chat)

- Exact name of the output folder (`.memory/`, `.cms/`, `.codegraph/`, etc.)?
- Should summaries be committed to git by default, or gitignored?
- Start with pure Python `ast` or bring in `tree-sitter` from day one?
- Preferred LLM for summary generation during development (local Ollama? Grok API? Claude?).
- How verbose should the initial CLI be?
- Any specific file types or languages you want supported early (beyond Python)?
- Do you want the graph to include variable-level detail early, or stay at function/class/file level?

---

**This document is now ready for transfer.**

To use it:
1. Download or copy the content of `codebase_memory_system_design_spec.md`
2. Start a new chat with Grok (or your preferred model)
3. Paste the entire document (or attach it) and say something like:

> "Let's design and build the Codebase Memory System exactly as described in this spec. Begin with Phase 1 — the clean directory scanner. I'll provide feedback and we iterate one phase at a time until it's self-hosting and useful."

You now have a complete, self-contained blueprint that captures everything we discussed.

Ready when you are.