"""Phase 4: typer CLI — scan, build-graph, summarize, query, run-all."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

if sys.platform == "win32":  # graph data is UTF-8; don't let cp1252 consoles crash on it
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from . import config
from .anchors import anchors_as_text
from .exporter import export_features, export_graph, export_index, export_summaries
from .graph_builder import build_graph
from .memory import CodebaseMemory
from .providers import get_provider
from .scanner import scan as scan_dir
from .summarizer import generate_summaries
from .tree_export import export_tree

app = typer.Typer(
    name="cms",
    help="Codebase Memory System — structural + semantic memory layer for AI agents.",
)


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context) -> None:
    """Run `cms` with no arguments (or double-click CMS.exe) to launch the app:
    memory sync + live file watcher + web UI."""
    if ctx.invoked_subcommand is None:
        from .app import run_app

        run_app(None, echo=typer.echo)  # auto-resolve: cwd -> saved workspace -> setup

RootOption = typer.Option(Path("."), "--root", "-r", help="Project root to analyse.")

config_app = typer.Typer(
    help="Manage CMS settings stored in ~/.cms/config.json (API keys, provider, models).",
    no_args_is_help=True,
)
app.add_typer(config_app, name="config")


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help=f"One of: {', '.join(config.CONFIG_KEYS)}"),
    value: str = typer.Argument(..., help="Value to store (e.g. your API key)."),
) -> None:
    """Store a setting, e.g.:  cms config set anthropic_api_key sk-ant-..."""
    key = key.lower()
    if key not in config.CONFIG_KEYS:
        typer.echo(f"Unknown key {key!r}. Valid keys: {', '.join(config.CONFIG_KEYS)}", err=True)
        raise typer.Exit(1)
    cfg = config.load_user_config()
    cfg[key] = value
    config.save_user_config(cfg)
    typer.echo(f"Saved {key} to {config.USER_CONFIG_PATH}")


@config_app.command("show")
def config_show() -> None:
    """Show current settings (secrets masked)."""
    cfg = config.load_user_config()
    if not cfg:
        typer.echo(f"No settings yet. Use:  cms config set anthropic_api_key <your key>")
        return
    for key, value in sorted(cfg.items()):
        shown = f"{value[:8]}...{value[-4:]}" if "key" in key and len(str(value)) > 14 else value
        typer.echo(f"{key} = {shown}")
    typer.echo(f"\n({config.USER_CONFIG_PATH})")


@config_app.command("path")
def config_path() -> None:
    """Print the config file location."""
    typer.echo(str(config.USER_CONFIG_PATH))


def _memory_dir(root: Path) -> Path:
    return root.resolve() / config.MEMORY_DIR_NAME


@app.command()
def scan(root: Path = RootOption) -> None:
    """Scan the directory and write .memory/clean_tree.md + clean_tree.json."""
    root = root.resolve()
    records = scan_dir(root)
    export_tree(root, records, _memory_dir(root))
    typer.echo(f"Scanned {len(records)} source files -> {_memory_dir(root) / 'clean_tree.md'}")


@app.command("build-graph")
def build_graph_cmd(root: Path = RootOption) -> None:
    """Scan + parse Python files into a knowledge graph -> .memory/graph.json."""
    root = root.resolve()
    records = scan_dir(root)
    export_tree(root, records, _memory_dir(root))
    graph = build_graph(records)
    out = export_graph(graph, _memory_dir(root))
    typer.echo(
        f"Graph: {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges -> {out}"
    )


@app.command()
def summarize(
    root: Path = RootOption,
    provider: str = typer.Option(None, "--provider", "-p", help="anthropic | openai | mock"),
) -> None:
    """Generate low-resolution AI summaries into the graph + .memory/summaries/."""
    root = root.resolve()
    memory_dir = _memory_dir(root)
    graph_path = memory_dir / "graph.json"
    if graph_path.is_file():
        mem = CodebaseMemory.load(graph_path)
        graph = mem.graph
        records = scan_dir(root)
    else:
        typer.echo("No graph.json found — building graph first.")
        records = scan_dir(root)
        export_tree(root, records, memory_dir)
        graph = build_graph(records)

    llm = get_provider(provider)
    typer.echo(f"Summarizing with provider: {llm.name}")

    def progress(path: str, done: int, total: int) -> None:
        typer.echo(f"  [{done}/{total}] {path}")

    count = generate_summaries(graph, root, llm, on_progress=progress)
    export_graph(graph, memory_dir)
    written = export_summaries(graph, memory_dir)
    export_index(graph, memory_dir, file_count=len(records))
    typer.echo(f"Summarized {count} files; wrote {written} summary docs under {memory_dir / 'summaries'}")


@app.command()
def query(
    text: str = typer.Argument(..., help="Natural-language intent, e.g. 'where is the ignore filtering?'"),
    root: Path = RootOption,
    top_k: int = typer.Option(5, "--top-k", "-k", help="Number of results."),
) -> None:
    """Query the memory graph for relevant files/functions/classes."""
    graph_path = _memory_dir(root) / "graph.json"
    if not graph_path.is_file():
        typer.echo(f"No memory found at {graph_path}. Run `cms run-all` first.", err=True)
        raise typer.Exit(1)
    mem = CodebaseMemory.load(graph_path)
    results = mem.query_intent(text, top_k=top_k)
    if not results:
        typer.echo("No matches.")
        return
    for i, hit in enumerate(results, 1):
        location = f"{hit.path}:{hit.lines}" if hit.lines else hit.path
        typer.echo(f"\n{i}. [{hit.kind}] {hit.name}  ({location})  score={hit.score}")
        if hit.anchors:
            typer.echo(f"     anchors: {anchors_as_text(hit.anchors)}")
        if hit.summary:
            for line in hit.summary.strip().splitlines()[:4]:
                typer.echo(f"     {line.strip()}")
        if hit.calls:
            typer.echo(f"     calls: {', '.join(hit.calls[:5])}")
        if hit.called_by:
            typer.echo(f"     called by: {', '.join(hit.called_by[:5])}")


@app.command()
def trace(
    feature: str = typer.Argument(None, help="Feature name to display; omit to (re)build all traces."),
    root: Path = RootOption,
    provider: str = typer.Option(None, "--provider", "-p", help="anthropic | openai | mock"),
) -> None:
    """Build feature traces (flows + narratives + verification checklists), or show one."""
    from .features import build_features, get_features

    root = root.resolve()
    memory_dir = _memory_dir(root)
    graph_path = memory_dir / "graph.json"
    if not graph_path.is_file():
        typer.echo("No graph.json — run `cms run-all` first.", err=True)
        raise typer.Exit(1)
    mem = CodebaseMemory.load(graph_path)

    if feature:
        matches = [f for f in get_features(mem.graph) if f["name"].lower() == feature.lower()]
        if not matches:
            names = ", ".join(f["name"] for f in get_features(mem.graph)) or "(none — run `cms trace`)"
            typer.echo(f"Unknown feature {feature!r}. Known: {names}", err=True)
            raise typer.Exit(1)
        f = matches[0]
        typer.echo(f"\nFeature: {f['name']}   [{f.get('source', '?')}]")
        if f.get("description"):
            typer.echo(f"  {f['description']}")
        if f.get("connects"):
            typer.echo(f"  connects: {', '.join(f['connects'])}")
        typer.echo("\n" + (f.get("summary") or "(no narrative — run `cms trace` to build)"))
        return

    llm = get_provider(provider)
    typer.echo(f"Tracing features with provider: {llm.name}")
    features = build_features(
        mem.graph, llm,
        on_progress=lambda name, d, t: typer.echo(f"  [{d}/{t}] {name}"),
    )
    mem.save(graph_path)
    count = export_features(mem.graph, memory_dir)
    typer.echo(f"Traced {len(features)} features; wrote {count} docs under {memory_dir / 'features'}")


@app.command()
def features(root: Path = RootOption) -> None:
    """List traced features."""
    from .features import get_features

    graph_path = _memory_dir(root) / "graph.json"
    if not graph_path.is_file():
        typer.echo("No graph.json — run `cms run-all` first.", err=True)
        raise typer.Exit(1)
    feats = get_features(CodebaseMemory.load(graph_path).graph)
    if not feats:
        typer.echo("No features traced yet. Run `cms trace`.")
        return
    for f in feats:
        entry = len(f.get("entry_points", []))
        typer.echo(
            f"  {f['name']:<32} [{f.get('source', '?')}] "
            f"{len(f.get('members', []))} members · {entry} entry point(s)"
            + (f" · connects: {', '.join(f['connects'])}" if f.get("connects") else "")
        )


@app.command("app")
def app_cmd(
    root: Path = typer.Option(None, "--root", "-r", help="Project root (default: auto — cwd, saved workspace, or first-run setup)."),
    port: int = typer.Option(7717, "--port", help="UI port."),
    provider: str = typer.Option(None, "--provider", "-p", help="anthropic | openai | mock"),
    interval: float = typer.Option(2.0, "--interval", "-i", help="Watch poll seconds."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open the browser."),
) -> None:
    """Everything in motion: sync memory, watch for changes, serve the UI."""
    from .app import run_app

    run_app(
        root, port=port, provider_name=provider,
        interval=interval, open_browser=not no_browser, echo=typer.echo,
    )


@app.command()
def impact(
    target: str = typer.Argument(..., help="File, bare name, or path::qualname, e.g. cms/scanner.py::scan"),
    root: Path = RootOption,
) -> None:
    """Blast radius: what is affected if this target changes?"""
    from .impact import analyze_impact

    graph_path = _memory_dir(root) / "graph.json"
    if not graph_path.is_file():
        typer.echo("No graph.json — run `cms run-all` first.", err=True)
        raise typer.Exit(1)
    result = analyze_impact(CodebaseMemory.load(graph_path).graph, target)
    if result is None:
        typer.echo(f"Could not resolve {target!r} in the graph.", err=True)
        raise typer.Exit(1)
    typer.echo(f"\nImpact of changing {result.target}  ({result.total} downstream)")
    for title, items in (
        ("Functions/classes", result.functions),
        ("Files", result.files),
        ("Features", result.features),
        ("Tests", result.tests),
    ):
        if items:
            typer.echo(f"\n  {title}:")
            for item in items:
                typer.echo(f"    - {item}")
    if result.tests:
        typer.echo(f"\nSuggested check:  pytest {' '.join(t.split('::')[0] for t in result.tests[:3])}")


@app.command()
def update(
    root: Path = RootOption,
    provider: str = typer.Option(None, "--provider", "-p", help="anthropic | openai | mock"),
    full: bool = typer.Option(False, "--full", help="Ignore caches; redo everything."),
) -> None:
    """Incremental update: only changed files are re-summarized/re-traced."""
    from .update import incremental_update

    llm = get_provider(provider)
    stats = incremental_update(root.resolve(), llm, echo=typer.echo, full=full)
    typer.echo(
        f"Updated: {stats.files} files scanned, {len(stats.changed)} changed, "
        f"{stats.summarized} re-summarized, {stats.features} features traced."
    )


@app.command()
def watch(
    root: Path = RootOption,
    provider: str = typer.Option(None, "--provider", "-p", help="anthropic | openai | mock"),
    interval: float = typer.Option(2.0, "--interval", "-i", help="Poll interval in seconds."),
) -> None:
    """Watch for changes and keep .memory/ in sync automatically."""
    from .update import watch as watch_loop

    watch_loop(root.resolve(), get_provider(provider), interval=interval, echo=typer.echo)


@app.command()
def verify(
    feature: str = typer.Argument(None, help="Feature to verify; omit to (re)map tests to all features."),
    root: Path = RootOption,
) -> None:
    """Map tests to features via coverage, or run the tests proving one feature."""
    from .verify import map_tests_to_features, run_coverage, verify_feature

    root = root.resolve()
    graph_path = _memory_dir(root) / "graph.json"
    if not graph_path.is_file():
        typer.echo("No graph.json — run `cms run-all` first.", err=True)
        raise typer.Exit(1)
    mem = CodebaseMemory.load(graph_path)

    if feature:
        from .features import get_features

        matches = [f for f in get_features(mem.graph) if f["name"].lower() == feature.lower()]
        if not matches:
            typer.echo(f"Unknown feature {feature!r}.", err=True)
            raise typer.Exit(1)
        tests = matches[0].get("verified_by") or []
        if not tests:
            typer.echo("No tests mapped yet — run `cms verify` (no args) first.")
            raise typer.Exit(1)
        typer.echo(f"Running {len(tests)} test(s) verifying {matches[0]['name']}:")
        for t in tests:
            typer.echo(f"  - {t}")
        passed, output = verify_feature(root, tests)
        typer.echo("\n" + output)
        typer.echo(f"\n{'PASS — feature behaves as specified' if passed else 'FAIL — implementation diverges'}")
        raise typer.Exit(0 if passed else 1)

    typer.echo("Running test suite under coverage (per-test contexts)…")
    data = run_coverage(root)
    if data is None:
        typer.echo("coverage/pytest failed — is `pip install cms[dev]` done?", err=True)
        raise typer.Exit(1)
    mapping = map_tests_to_features(mem.graph, root, data)
    mem.save(graph_path)
    for name, tests in sorted(mapping.items()):
        typer.echo(f"  {name:<32} {len(tests)} test(s)")
    typer.echo("Saved to graph. Try:  cms verify <FeatureName>")


@app.command()
def review(
    feature: str = typer.Argument(None, help="Feature to show; omit to (re)build the full review."),
    root: Path = RootOption,
    provider: str = typer.Option(None, "--provider", "-p", help="anthropic | openai | mock"),
) -> None:
    """AI review: does what was built align with what you expect? Per feature + overall."""
    from .exporter import export_graph
    from .review import build_review, export_review

    root = root.resolve()
    memory_dir = _memory_dir(root)
    graph_path = memory_dir / "graph.json"
    if not graph_path.is_file():
        typer.echo("No graph.json — run `cms run-all` first.", err=True)
        raise typer.Exit(1)
    mem = CodebaseMemory.load(graph_path)

    if feature:
        from .features import get_features

        matches = [f for f in get_features(mem.graph) if f["name"].lower() == feature.lower()]
        if not matches or not matches[0].get("review"):
            typer.echo(f"No review for {feature!r} — run `cms review` first.", err=True)
            raise typer.Exit(1)
        r = matches[0]["review"]
        typer.echo(f"\n{matches[0]['name']}  [{r['verdict'].upper()}]")
        typer.echo(f"  {r['headline']}\n")
        typer.echo(f"Expected: {r['expected']}\n")
        typer.echo(f"Built:    {r['built']}")
        if r.get("gaps"):
            typer.echo("\nGaps:")
            for g in r["gaps"]:
                typer.echo(f"  - {g}")
        typer.echo(f"\nHow it works: {r['education']}")
        return

    llm = get_provider(provider)
    typer.echo(f"Reviewing with provider: {llm.name}")
    result = build_review(
        mem.graph, root, llm,
        on_progress=lambda name, d, t: typer.echo(f"  [{d}/{t}] {name}"),
    )
    export_graph(mem.graph, memory_dir)
    out = export_review(mem.graph, memory_dir)
    app_r = result["app"]
    typer.echo(f"\nOverall: {app_r['verdict'].upper()} — {app_r['headline']}")
    typer.echo(f"Verdicts: " + ", ".join(f"{n} {v}" for v, n in app_r["counts"].items() if n))
    typer.echo(f"Written to {out}")


@app.command()
def suggest(
    root: Path = RootOption,
    provider: str = typer.Option(None, "--provider", "-p", help="anthropic | openai | mock"),
    top: int = typer.Option(8, "--top", "-n", help="How many to show."),
) -> None:
    """Plan what's worth building next, ranked by return on investment."""
    from .exporter import export_graph
    from .suggest import build_suggestions, export_suggestions

    root = root.resolve()
    memory_dir = _memory_dir(root)
    graph_path = memory_dir / "graph.json"
    if not graph_path.is_file():
        typer.echo("No graph.json — run `cms run-all` first.", err=True)
        raise typer.Exit(1)
    mem = CodebaseMemory.load(graph_path)
    llm = get_provider(provider)
    typer.echo(f"Planning suggestions with provider: {llm.name}\n")
    suggestions = build_suggestions(mem.graph, root, llm)
    export_graph(mem.graph, memory_dir)
    out = export_suggestions(mem.graph, memory_dir)
    for i, s in enumerate(suggestions[:top], 1):
        typer.echo(f"{i}. [ROI {s['roi']}×] {s['title']}   ({s['kind']} · value {s['value']} · effort {s['effort']})")
        typer.echo(f"     {s['description']}")
        if s["rationale"]:
            typer.echo(f"     why: {s['rationale']}")
    typer.echo(f"\nWritten to {out}")


@app.command()
def prompt(
    task: str = typer.Argument(..., help="What you plan to do, in your own words."),
    root: Path = RootOption,
    as_json: bool = typer.Option(False, "--json", help="Full data pack as JSON instead of markdown."),
    top_k: int = typer.Option(8, "--top-k", "-k", help="How many code targets to include."),
) -> None:
    """Export an ultra-detailed, ready-to-paste task prompt built from the memory."""
    from .prompt_export import export_prompt

    root = root.resolve()
    if not (_memory_dir(root) / "graph.json").is_file():
        typer.echo("No graph.json — run `cms run-all` first.", err=True)
        raise typer.Exit(1)
    content, out = export_prompt(root, task, as_json=as_json, top_k=top_k)
    typer.echo(content)
    typer.echo(f"\n--- written to {out}", err=True)


@app.command()
def mcp(root: Path = RootOption) -> None:
    """Run the MCP server (stdio) so AI agents can query this memory natively."""
    from .mcp import MCPServer

    root = root.resolve()
    if not (_memory_dir(root) / "graph.json").is_file():
        typer.echo("No memory layer. Run `cms run-all` first.", err=True)
        raise typer.Exit(1)
    MCPServer(root).serve()


@app.command()
def ui(
    root: Path = RootOption,
    port: int = typer.Option(7717, "--port", help="Local port to serve on."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open the browser."),
) -> None:
    """Open the memory viewer: file tree + knowledge graph + inspector."""
    root = root.resolve()
    if not (_memory_dir(root) / "graph.json").is_file():
        typer.echo(f"No memory layer at {_memory_dir(root)}. Run `cms run-all` first.", err=True)
        raise typer.Exit(1)
    from .ui import serve

    serve(root, port=port, open_browser=not no_browser)


@app.command("run-all")
def run_all(
    root: Path = RootOption,
    provider: str = typer.Option(None, "--provider", "-p", help="anthropic | openai | mock"),
) -> None:
    """Full pipeline: scan -> graph -> summaries -> export .memory/."""
    root = root.resolve()
    memory_dir = _memory_dir(root)

    from .update import incremental_update

    llm = get_provider(provider)
    stats = incremental_update(root, llm, echo=typer.echo, full=True)
    typer.echo(
        f"Done: {stats.files} files, {stats.summarized} summarized, "
        f"{stats.features} features traced"
        + (f", git stats on {stats.git_files} files" if stats.git_files else "")
        + "."
    )
    typer.echo(f"\nMemory layer ready at {memory_dir}")
    typer.echo('Try:  cms query "where is the directory scanning logic?"')


if __name__ == "__main__":
    app()
