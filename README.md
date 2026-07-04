# CMS — Codebase Memory System

A self-bootstrapping structural + semantic memory layer for codebases, built for AI
agents. CMS scans a project (ignoring junk like `node_modules/`, `__pycache__/`,
build output), parses the source into a knowledge graph of files, classes, and
functions, generates low-resolution AI summaries for each, and exposes a query
interface so an agent can ask *"where is the auth logic?"* and get precise answers —
file paths, line ranges, call connections, and intent summaries — instead of grepping.

Full design rationale: [`codebase_memory_system_design_spec.md`](codebase_memory_system_design_spec.md).

## Install

```bash
pip install -e .            # core (networkx, pathspec, typer)
pip install -e ".[anthropic]"  # + Anthropic SDK for LLM summaries
```

## Usage

```bash
cms run-all                 # scan -> graph -> summaries -> features -> git -> .memory/
cms query "where is the ignore pattern filtering logic?"
cms ui                      # open the memory viewer in your browser
cms update                  # incremental: only changed files re-summarized
cms watch                   # keep .memory/ in sync as you edit
cms impact cms/scanner.py::scan   # blast radius of a change
cms verify                  # map tests to features via coverage
cms verify CleanDirectoryScanner  # run exactly the tests proving a feature
cms mcp                     # MCP server for AI agents (see below)
```

## App mode (`cms app` / CMS.exe)

Everything in motion with one command — or one double-click:

```bash
cms app        # sync memory -> start file watcher -> serve UI -> open browser
cms            # no arguments does the same thing
```

On launch it heals any stale memory (only changed / mock-summarized files are
reprocessed), then watches for edits and keeps `.memory/` current while the UI
runs. Ctrl+C stops everything.

### Packaging as CMS.exe

```bash
pip install pyinstaller
python -m PyInstaller --onefile --name CMS --console --clean --noconfirm ^
    --add-data "cms/ui_assets/index.html;cms/ui_assets" --hidden-import anthropic ^
    --exclude-module torch --exclude-module torchvision --exclude-module torchaudio ^
    --exclude-module numpy --exclude-module scipy --exclude-module pandas ^
    --exclude-module matplotlib --exclude-module cv2 --exclude-module PIL ^
    --exclude-module lxml --exclude-module IPython --exclude-module jupyter ^
    --exclude-module pytest --exclude-module coverage --exclude-module rich ^
    --exclude-module pygments --exclude-module tkinter --exclude-module setuptools ^
    cms_exe.py
```

The excludes matter: networkx probes for optional backends (numpy/scipy/pandas/
matplotlib) at import time, so PyInstaller happily bundles whatever heavy
packages live in your site-packages (a torch install alone adds ~400 MB).
CMS uses none of them.

**Installer-style first run:** double-click `CMS.exe` anywhere and it asks which
codebase this copy should work on, then saves the choice to `cms.workspace.json`
next to the exe. Every launch after that goes straight to that project — so you
can keep one copy of CMS.exe per codebase, each linked to its own root. Delete
`cms.workspace.json` (or pass `--root`) to re-link. If the exe sits inside a
project root already, that project is used directly with no prompt.

All CLI commands work through the exe too (`CMS.exe query "..."`,
`CMS.exe impact ...`). The API key is read from `~/.cms/config.json` as usual.
Note: `CMS.exe verify` shells out to your installed Python for pytest/coverage.

## MCP server (`cms mcp`)

Expose the memory to AI agents as native tools — memory consulted before grep:

```bash
claude mcp add cms -- cms mcp --root /path/to/project
```

Tools: `query_codebase`, `get_file_summary`, `list_features`, `get_feature_trace`,
`who_calls`, `who_imports`, `get_impact`, `get_source`. Every call is logged to
`.memory/activity.jsonl`, and the UI renders live glow pulses on the touched
nodes plus an `MCP · tool` badge — you can watch your agent think.

## Git history layer

Inside a git repo, `run-all`/`update` enrich file nodes with commits, authors,
churn and age, and detect **hidden coupling**: file pairs that repeatedly change
together without any import relationship (CO_CHANGES edges). In the UI, hit
`heat` — nodes recolor by change frequency (calm→hot), co-change pairs draw as
dashed amber links, and the inspector gains a History section.

## Verification loop

`cms verify` runs your tests under coverage with per-test contexts and maps each
feature to the tests that actually execute its code (`verified_by`). Then
`cms verify <Feature>` runs exactly those tests — the feature trace's checklist
becomes executable proof that the implementation matches intent.

## Feature tracing (`cms trace`)

Features are first-class: declare them with `@memory:feature:Name` anchors (the
LLM also discovers undeclared ones from file summaries). For every feature CMS
computes its members, entry points, and *flows* — call chains walked through the
graph with `file:line` at each step — then writes a trace with Purpose, Flow,
Inputs & Outputs, and a **Verification Checklist** of concrete checks to confirm
the implementation does what you intended.

```bash
cms trace                    # build/refresh all feature traces
cms features                 # list features with member/entry counts
cms trace CleanDirectoryScanner   # print one trace
```

Traces live in `.memory/features/*.md`, in the graph (`feature:` nodes, so
`cms query` finds them), and in the UI — pick a feature in the explorer to see
its flow rail and light up its member files on the graph.

Features connect to each other two ways: **declared** links from `@memory:connects:`
anchors, and **inferred** RELATES edges derived from the code (a member of one
feature imports or calls a member of another) — so even LLM-discovered features
join the web. Hit the `feat` button in the UI (or open `?view=features`) for the
feature-level architecture map: amber nodes are declared features, green are
discovered, solid edges declared, dashed inferred. Click any node for its trace.

## Memory viewer (`cms ui`)

A local, zero-dependency web UI over the memory layer at `http://127.0.0.1:7717`:

- **Explorer** — clean file tree, junk-free, colored by top-level directory.
- **Knowledge graph** — force-directed canvas; node size = lines, edges = imports.
  Hover for a summary tooltip, click to inspect, drag/pan/zoom, `ext` toggles
  external modules, `fit` reframes.
- **Inspector** — file stats, anchor chips, the AI summary, every component with
  line ranges, caller/callee counts and expandable source snippets, plus
  imports/imported-by navigation.
- **Search** — press `/` and ask in plain language; results rank via the same
  intent engine as `cms query`.
- Deep-link a file with `?file=cms/scanner.py`. Serves on localhost only.

Everything lands in `.memory/` inside the analysed project:

```
.memory/
├── clean_tree.md      # filtered directory tree with per-file metadata
├── clean_tree.json    # machine-readable version
├── graph.json         # knowledge graph, summaries embedded in nodes
├── index.md           # what's here + how to query
└── summaries/         # per-file markdown summaries mirroring the source layout
```

## Python API (for agents)

```python
from cms import CodebaseMemory

mem = CodebaseMemory.load(".memory/graph.json")
for hit in mem.query_intent("clean directory tree building", top_k=5):
    print(hit.path, hit.lines, hit.summary)
    print("called by:", hit.called_by)

mem.who_imports("cms/scanner.py")   # -> ["file:cms/cli.py", ...]
mem.who_calls("scan")               # -> caller node ids
mem.neighbors("file:cms/scanner.py")
```

## API key setup

```bash
cms config set anthropic_api_key sk-ant-...   # stored in ~/.cms/config.json
cms config show                               # settings with secrets masked
```

Environment variables always take precedence over the config file. Other keys:
`provider`, `anthropic_model`, `openai_api_key`, `openai_base_url`, `openai_model`.

## Memory anchors

Guide the memory layer with `# @memory:` comments — developer-curated intent the
AST can't infer. Anchors land on graph nodes, enrich LLM prompts, and get a
ranking boost in queries.

```python
# @memory:feature:UserAuthentication
# @memory:connects:LoginFlow, TokenService
# @memory:summary:Handles JWT issuance and refresh.
def login_user(...):
    ...

# === @memory:module:GraphLayer ===
# Purpose: Maintains the runtime knowledge graph   (plain comments become notes)
class MemoryEngine:
    ...
```

Line-form anchors attach to the next `def`/`class`; `module` tags (and anchors not
followed by a definition) attach to the file. Only real comments count — anchor-like
text inside strings or docstrings is ignored.

## Summary providers

Selected via `--provider` or the `CMS_PROVIDER` env var (`anthropic` | `openai` | `mock`):

- **anthropic** — default when `ANTHROPIC_API_KEY` is set; uses `claude-haiku-4-5`
  (override with `CMS_ANTHROPIC_MODEL`).
- **openai** — any OpenAI-compatible endpoint (Ollama, LM Studio, xAI, OpenAI).
  Configure `CMS_OPENAI_BASE_URL` (default `http://localhost:11434/v1`),
  `CMS_OPENAI_MODEL`, and `CMS_OPENAI_API_KEY`/`OPENAI_API_KEY` if needed.
- **mock** — deterministic structural summaries from AST facts, no network.
  Automatic fallback when no key is configured, so the pipeline always runs.

## Ignore rules

Built-in defaults (see `cms/config.py`) cover VCS, virtualenvs, `node_modules/`,
build output, IDE and OS junk. Add project-specific patterns to a `.cmsignore`
file in the project root (gitignore syntax). Only whitelisted source extensions
are included (`.py`, `.md`, `.json`, `.ts`, ... — see `LANGUAGE_BY_EXTENSION`).

## Development

```bash
pip install -e ".[dev]"
pytest tests/
cms run-all   # self-hosting check: CMS analysing its own code
```

Current scope (spec Phases 1–4): Python-only AST parsing, keyword+structure query
ranking. Next up (Phase 5+): mtime-based incremental updates, tree-sitter for more
languages, embedding-based semantic search.
