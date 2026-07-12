# Atlas operations — this machine's setup

*How Atlas (the `cms` package) is installed here, how Codex talks to it, and
how to maintain it. Written 2026-07-11.*

## Runtime

- **Authoritative environment:** `C:\Users\banan\Desktop\CodeCrawl\.venv`
  (Python 3.11, created from the user install at
  `%LOCALAPPDATA%\Programs\Python\Python311`). The repo is installed editable
  with `dev` + `anthropic` extras, so code edits apply immediately.
- A second editable install exists in the global Python 3.11 (`cms` on PATH
  from `...\Python311\Scripts\cms.exe`). Both point at the same source; the
  `.venv` is the one Codex's MCP entry and `CMS.bat` use.
- **Launcher health:** `CMS.bat` does not trust `python.exe` merely because the
  file exists. It import-probes `CMS_PYTHON` (an optional explicit override),
  the project `.venv`, `py -3.11`, then `python` on `PATH`. A copied or stale
  venv is rejected with the repair commands below instead of producing an
  opaque Windows launcher error.
- **Recreate the venv** (e.g. after a Python upgrade):

  ```powershell
  cd C:\Users\banan\Desktop\CodeCrawl
  py -3.11 -m venv .venv
  python -m pip --python .venv\Scripts\python.exe install -U pip   # pip >= 26 trusts the Windows cert store
  .venv\Scripts\python -m pip install -e ".[dev,anthropic]"
  ```

  The `pip --python` upgrade step matters behind SSL-inspecting proxies: the
  venv's bundled pip 23 only trusts certifi and fails; pip 26 uses the system
  trust store.

## Codex integration (MCP)

- The entry lives in `C:\Users\banan\.codex\config.toml`:

  ```toml
  [mcp_servers.cms]
  command = 'C:\Users\banan\Desktop\CodeCrawl\.venv\Scripts\python.exe'
  args = ["-m", "cms.cli", "mcp"]
  ```

  Manage it with `codex mcp list | get cms | remove cms`, or re-add:
  `codex mcp add cms -- C:\Users\banan\Desktop\CodeCrawl\.venv\Scripts\python.exe -m cms.cli mcp`
- **Multi-repo:** no `--root` is configured on purpose. `cms mcp` walks up
  from its launch directory to the nearest ancestor containing
  `.memory/graph.json` and serves that project. In a repo with no memory
  layer the server stays alive and every tool answers
  "no memory layer — run `cms run-all`" (it will not create `.memory/` there).
  To map a new repo: run `cms run-all` (or `cms app`) once inside it.
- **Switching mid-session:** ask Codex to call `switch_project(path)` — the
  server rebinds to that project (must contain `.memory/` or `.git`). If the
  target isn't mapped yet, the tool returns the exact `cms run-all` command;
  Codex runs it in its shell and the new graph is picked up with no restart.
- **Restart rule:** Codex reads `config.toml` at session start. After adding
  or changing an MCP entry, start a **new Codex session/conversation**
  (running sessions keep their old server set).
- **Confirm Codex sees Atlas:** `codex mcp list` should show `cms` enabled;
  in a session, ask Codex to call `list_features`. Non-interactively:
  `codex exec "call the cms list_features tool and report the count"`.

## Skill

- **Source of truth:** `C:\Users\banan\Desktop\CodeCrawl\SKILL.md`.
- Installed copies (update both after editing the repo copy):
  - `C:\Users\banan\.agents\skills\atlas\SKILL.md` (Codex discovery)
  - `C:\Users\banan\.claude\skills\atlas\SKILL.md` (Claude Code)

  ```powershell
  Copy-Item SKILL.md C:\Users\banan\.agents\skills\atlas\SKILL.md -Force
  Copy-Item SKILL.md C:\Users\banan\.claude\skills\atlas\SKILL.md -Force
  ```

## Daily commands

Run from the repo (or use `--root`); `CMS.bat <cmd>` works from Explorer or
any shell — it prefers `.venv` and never pauses when given arguments.

| Task | Command |
|---|---|
| Refresh memory after edits | `cms update` (add `-p mock` if offline) |
| Full rebuild | `cms update --full` or `cms run-all` |
| Quality gate | `cms sentinel` (exit 1 on active criticals) |
| Change gate | `cms align "<goal>"` → edit → `cms align status` (exit 1 on drift) |
| Tests | `.venv\Scripts\python -m pytest tests -q` |
| UI | `cms app` (or double-click `CMS.bat`) |
| Diagnose | `cms features` (memory loads?) · `codex mcp list` (entry present?) · `.venv\Scripts\python -m cms.cli --help` (runtime OK?) |

## Mock vs semantic output

- Provider comes from `~/.cms/config.json` (an Anthropic key is configured;
  env vars override). With `-p mock`, summaries/narratives are **structural
  placeholders and are labelled** `provider: mock` in the graph.
- The next `cms update` with a real provider automatically re-summarizes
  everything mock-labelled (`upgrade_mock` in `cms/update.py`).
- Sandboxed agents (Claude Code, possibly Codex) may be unable to reach the
  Anthropic API (SSL-inspecting proxy + certifi): run `cms update` from a
  normal terminal for semantic output; agents should use `-p mock`.
- The AI review (`cms review`) verdict is only *semantic* when produced by a
  real provider — a structural/mock run is labelled and must not be treated
  as a verified review.

## Semantic-stage evidence (`.memory/semantic_state.json`)

Atlas persists positive, versioned evidence that each semantic stage ran:
`summaries`, `features` (LLM discovery), `review`, `suggestions`. Each
record carries status (`complete | failed | skipped | never_run`),
provider + model, `real_provider`, timestamps, input/output hashes, the
`feature_set_hash` a judgment evaluated, and feature counts. Written
atomically; no secrets. Staleness is *derived* live (hash comparison),
never guessed from node existence.

Rules:
- **Real-provider-only semantic completion.** Mock builds never create
  completion markers, judgment nodes, or a "successful" zero-feature
  state — they record an explicit `skipped` with the reason, and the UI
  says so. Provider errors / malformed output record `failed` (prior
  good evidence preserved under `last_success`) and retry on the next
  update after inputs change.
- **Legacy migration.** Projects that pre-date this record (real
  summaries, zero features, judgment nodes built pre-discovery) read as
  `never_run` and recover on a NORMAL `cms update` / app launch / UI
  build — no `--full`, no special-casing.
- **A legitimate zero-feature discovery is recorded `complete`** and is
  not re-charged while inputs are unchanged.
- **Frozen vs invalid judgment.** Invalid judgment (mock/structural, no
  evidence, or generated against an empty pre-discovery feature set) is
  rebuilt automatically once real discovery completes — that is
  *initialization recovery*. A VALID real-provider judgment whose
  feature-set hash has drifted is **frozen**: exposed as stale in the UI
  and refreshed only by explicit `cms review` / `cms suggest`. Routine
  updates never silently re-charge judgment.

Diagnosing "zero features": open the UI — the Features section is never
hidden; it states which case you are in (never ran / needs a real
provider / failed with the error / ran and legitimately found zero /
stale). `GET /api/semantic` gives the same as JSON. The active project
root is on the header (hover the project name) with a provider chip.

## Known limitations

- One MCP server process serves one project root (chosen at launch via the
  cwd walk-up). Codex launches it per session, so different repos in
  different sessions each get their own correctly-rooted instance.
- TS/JS parsing is declaration+import level (no call edges); query ranking is
  keyword-based, not embeddings.
- The PyInstaller `CMS.exe` is blocked by AVG (unsigned) — use `CMS.bat`.
  If you rebuild it, kill running instances first (locked file).
