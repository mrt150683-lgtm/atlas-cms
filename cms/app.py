"""App mode — everything in motion with one command (or one double-click).

Runs the full living-memory experience against the current .memory/ data:
  - initial build if no memory exists yet
  - background watcher: file changes -> incremental update (only changed
    files re-summarized, mock summaries upgraded when a real key appears)
  - the web UI served locally, opened in the browser
Ctrl+C stops everything. The MCP server is not run here — it's stdio-based
and gets spawned by the agent harness (`claude mcp add cms -- cms mcp`).
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from . import config
from .providers import get_provider
from .ui import serve
from .update import incremental_update, watch


def _workspace_config_path() -> Path:
    """Per-install workspace file, next to the exe when frozen — so a fresh
    copy of CMS.exe per codebase remembers its own root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "cms.workspace.json"
    return Path.cwd() / "cms.workspace.json"


def _load_workspace_root() -> Path | None:
    ws = _workspace_config_path()
    try:
        saved = Path(json.loads(ws.read_text(encoding="utf-8"))["root"])
        return saved.resolve() if saved.is_dir() else None
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _save_workspace_root(root: Path) -> None:
    try:
        _workspace_config_path().write_text(
            json.dumps({"root": str(root)}, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _first_run_setup(echo) -> Path | None:
    """Installer-style first run: ask which codebase this copy works on.

    Tries a native folder-picker first (works from a double-click even with no
    console), then falls back to a console prompt.
    """
    from .picker import pick_folder
    from .scanner import scan

    echo("Opening a folder picker — choose your codebase…")
    chosen = pick_folder("Choose the codebase folder for Atlas")
    if chosen:
        root = Path(chosen).expanduser().resolve()
        if root.is_dir() and scan(root):
            echo(f"  Linking Atlas to: {root}")
            _save_workspace_root(root)
            return root
        echo(f"  No recognisable source under {root} — try manual entry.")

    if not sys.stdin or not sys.stdin.isatty():
        echo("No project root configured and no interactive console to ask.")
        echo("Run:  CMS.bat app --root <path-to-your-codebase>")
        return None
    echo("")
    echo("=" * 62)
    echo("  CMS — first run setup")
    echo("  This copy will be linked to one codebase.")
    echo("=" * 62)
    while True:
        try:
            raw = input("\nProject root directory (or Q to quit): ").strip().strip('"').strip("'")
        except (KeyboardInterrupt, EOFError):
            return None
        if raw.lower() in ("q", "quit", "exit"):
            return None
        if not raw:
            continue
        root = Path(raw).expanduser()
        if not root.is_dir():
            echo(f"  Not a directory: {root}")
            continue
        root = root.resolve()
        found = len(scan(root))
        if not found:
            echo(f"  No recognisable source files under {root} — pick the codebase root.")
            continue
        echo(f"  Found {found} source files. Linking this CMS to: {root}")
        _save_workspace_root(root)
        echo(f"  (Saved to {_workspace_config_path().name} — delete it to re-run setup.)")
        return root


def resolve_root(explicit: Path | None, echo=print) -> Path | None:
    """Explicit --root wins; else the saved workspace (the last codebase you
    chose / switched to — this must beat cwd, since CMS.bat runs from the repo
    dir); else cwd if it looks like a project; else interactive first-run setup."""
    from .scanner import scan

    if explicit is not None:
        return explicit.resolve()
    saved = _load_workspace_root()
    if saved is not None:
        return saved
    cwd = Path.cwd().resolve()
    if (cwd / config.MEMORY_DIR_NAME / "graph.json").is_file() or scan(cwd):
        return cwd
    return _first_run_setup(echo)


# @memory:feature:AppMode
# @memory:connects:IncrementalUpdates, MemoryViewer
# @memory:summary:Everything in motion with one command or double-click — installer-style first-run root selection saved per exe copy, then startup sync, background watcher, UI server + browser.
def run_app(
    root: Path | None,
    port: int = 7717,
    provider_name: str | None = None,
    interval: float = 2.0,
    open_browser: bool = True,
    echo=print,
) -> None:
    root = resolve_root(root, echo=echo)
    if root is None:
        _pause_if_frozen()
        return
    memory_dir = root / config.MEMORY_DIR_NAME

    if not (memory_dir / "graph.json").is_file():
        from .scanner import scan

        if not scan(root):
            echo(f"No source files found in {root} — is this the right folder?")
            echo("Run:  CMS.exe app --root <path>   (or delete cms.workspace.json to re-run setup)")
            _pause_if_frozen()
            return

    provider = get_provider(provider_name)
    echo(f"Atlas — {root.name}  (provider: {provider.name})")
    first_run = not (memory_dir / "graph.json").is_file()
    landing = "/"
    if first_run and open_browser:
        # Let the user choose scope in the browser BEFORE paying to process.
        echo("first run: opening setup — choose which folders/files to process…")
        landing = "/setup"
    elif first_run:
        echo("first run: building the memory layer…")
        incremental_update(root, provider, echo=echo)
    else:
        echo("syncing memory with current files…")
        stats = incremental_update(root, provider, echo=echo)
        if stats.changed:
            echo(f"  refreshed {len(stats.changed)} changed file(s)")
        else:
            echo("  memory already current")

    watcher = threading.Thread(
        target=watch, args=(root, provider), kwargs={"interval": interval, "echo": echo},
        daemon=True, name="cms-watch",
    )
    watcher.start()
    try:
        serve(root, port=port, open_browser=open_browser, open_path=landing)  # blocks until Ctrl+C
    except OSError as exc:
        echo(f"Could not start the UI server on port {port}: {exc}")
        echo("Is another CMS already running? Try:  CMS.exe app --port 7718")
        _pause_if_frozen()


def _pause_if_frozen() -> None:
    """Keep the console window open after messages when double-clicked."""
    if getattr(sys, "frozen", False) and sys.stdin and sys.stdin.isatty():
        try:
            input("\nPress Enter to close...")
        except (KeyboardInterrupt, EOFError):
            pass
