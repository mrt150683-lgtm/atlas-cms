"""Phase 4: typer CLI — scan, build-graph, summarize, query, run-all."""

from __future__ import annotations

import json
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
        tests = matches[0].get("exercised_by") or []
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
    if llm.name != "mock":  # manual refresh keeps the provenance record truthful
        from . import semantic_state as ss

        ss.record_stage(
            memory_dir, "review", status="complete", provider=llm.name,
            model=getattr(llm, "model", None), real_provider=True,
            feature_set_hash=ss.feature_set_hash(mem.graph),
            **ss.feature_counts(mem.graph),
        )
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
    if llm.name != "mock":
        from . import semantic_state as ss

        ss.record_stage(
            memory_dir, "suggestions", status="complete", provider=llm.name,
            model=getattr(llm, "model", None), real_provider=True,
            feature_set_hash=ss.feature_set_hash(mem.graph),
            items=len(suggestions), **ss.feature_counts(mem.graph),
        )
    for i, s in enumerate(suggestions[:top], 1):
        typer.echo(f"{i}. [ROI {s['roi']}×] {s['title']}   ({s['kind']} · value {s['value']} · effort {s['effort']})")
        typer.echo(f"     {s['description']}")
        if s["rationale"]:
            typer.echo(f"     why: {s['rationale']}")
    typer.echo(f"\nWritten to {out}")


@app.command()
def fuse(
    projects: list[Path] = typer.Argument(None, help="Project roots to fuse (default: every registered mapped project)."),
    provider: str = typer.Option(None, "--provider", "-p", help="anthropic | openai (mock is refused)."),
    list_only: bool = typer.Option(False, "--list", help="Show the project registry and readiness, then exit."),
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON."),
    refine: str = typer.Option(None, "--refine", help="Revise the latest report per this direction instead of rebuilding."),
) -> None:
    """Constellation: fuse multiple mapped projects — integration opportunities,
    emergent features, and conflicts across codebases (real provider required)."""
    from .fuse import FUSION_DIR, FusionError, build_card, build_fusion, load_registry

    if refine:
        from .fuse import refine_fusion

        llm = get_provider(provider)
        try:
            report = refine_fusion(refine, llm)
        except FusionError as exc:
            typer.echo(f"refine failed: {exc}", err=True)
            raise typer.Exit(1)
        typer.echo(f"Refined ({report['generated_at']}): "
                   f"{len(report['integrations'])} integrations, "
                   f"{len(report['emergent'])} emergent, {len(report['conflicts'])} conflicts")
        typer.echo(f"Written to {FUSION_DIR / 'latest.md'}")
        return

    roots = [p.resolve() for p in (projects or [])]
    if not roots:
        roots = [Path(r) for r in (load_registry().get("projects") or {})]
    if list_only or not roots:
        if not roots:
            typer.echo("No registered projects yet — build a memory layer somewhere first (cms run-all).")
            raise typer.Exit(1)
        for r in sorted(roots):
            card = build_card(r)
            mark = "ready" if card.get("ready") else f"NOT READY — {card.get('reason')}"
            feats = len(card.get("features", [])) if card.get("ready") else "-"
            typer.echo(f"  {card['name']:<24} {str(feats):>3} features   {mark}")
        if list_only:
            return

    llm = get_provider(provider)
    typer.echo(f"Fusing {len(roots)} project(s) with provider: {llm.name}")
    try:
        report = build_fusion(roots, llm)
    except FusionError as exc:
        typer.echo(f"fusion failed: {exc}", err=True)
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(report, indent=1))
        return
    typer.echo(f"\nProjects: {', '.join(report['projects'])}")
    for c in report.get("excluded", []):
        typer.echo(f"  excluded: {c['name']} — {c['reason']}")
    for key, label in (("integrations", "INTEGRATE"), ("emergent", "EMERGENT"), ("conflicts", "CONFLICT")):
        for i in report.get(key) or []:
            typer.echo(f"  [{label}] {i.get('title')}  ({', '.join(i.get('projects', []))})")
    typer.echo(f"\nWritten to {FUSION_DIR / 'latest.md'}")


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
    from .mcp import MCPServer, discover_root

    # Walk up to the nearest mapped project (a global MCP config launches us
    # with cwd = whatever workspace the agent has open, possibly a subdir).
    root = discover_root(root.resolve())
    if not (_memory_dir(root) / "graph.json").is_file():
        # Keep serving: a dead server breaks the agent session everywhere,
        # while a live one can explain how to build the memory layer.
        typer.echo(f"cms mcp: no memory layer under {root} — serving anyway; "
                   "tools will report how to build it (cms run-all).", err=True)
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


sentinel_app = typer.Typer(
    help="Hermes Sentinel — bug finding, feature auditing and the completion quality gate.",
    no_args_is_help=False,
    invoke_without_command=True,
)
app.add_typer(sentinel_app, name="sentinel")


@sentinel_app.callback()
def _sentinel_default(ctx: typer.Context) -> None:
    """`cms sentinel` with no subcommand runs a full scan (quality-gate mode)."""
    if ctx.invoked_subcommand is None:
        sentinel_run(root=Path("."), as_json=False)


@sentinel_app.command("run")
def sentinel_run(
    root: Path = RootOption,
    as_json: bool = typer.Option(False, "--json", help="Print the scan result as JSON."),
) -> None:
    """Run every Sentinel module; exit non-zero if the quality gate fails."""
    from .sentinel.runner import run_scan

    root = root.resolve()
    scan_result, findings = run_scan(root, echo=typer.echo)
    if as_json:
        import json as _json

        typer.echo(_json.dumps({k: v for k, v in scan_result.items() if k != "inventory"}, indent=1))
    else:
        gate = scan_result["gate"]
        counts = gate["active_counts"]
        typer.echo(
            f"\nSentinel scan {scan_result['scan_id']}  ({scan_result['duration_s']}s, "
            f"mode: {scan_result['execution_mode']})"
        )
        for check in scan_result.get("workflow_checks", []):
            mark = {True: "pass", False: "FAIL", None: "missing"}[check["passed"]]
            typer.echo(f"  workflow {check['name']:<38} {mark}")
        typer.echo(
            "  active findings: "
            + ", ".join(f"{counts.get(s, 0)} {s}" for s in ("critical", "high", "medium", "low", "info"))
        )
        for err_module, err in (scan_result.get("module_errors") or {}).items():
            typer.echo(f"  module error: {err_module}: {err}")
        if gate["failed"]:
            typer.echo("\nQUALITY GATE FAILED on: " + "; ".join(gate["reasons"][:5]))
        elif gate["warnings"]:
            typer.echo(f"\nGate passed with {len(gate['warnings'])} warning-level finding(s).")
        else:
            typer.echo("\nGate passed.")
        typer.echo("Details:  cms sentinel findings   ·   UI: /sentinel   ·   export: cms sentinel export")
    raise typer.Exit(1 if scan_result["gate"]["failed"] else 0)


@sentinel_app.command("findings")
def sentinel_findings(
    root: Path = RootOption,
    severity: str = typer.Option(None, "--severity", "-s", help="Filter: critical|high|medium|low|info"),
    status: str = typer.Option(None, "--status", help="Filter: open|acknowledged|fixed_pending_verification|resolved|false_positive"),
) -> None:
    """List persistent Sentinel findings."""
    from . import config as _config
    from .sentinel import SEVERITIES
    from .sentinel.store import SentinelStore

    store = SentinelStore(root.resolve() / _config.MEMORY_DIR_NAME)
    findings = store.load_findings()
    if not findings:
        typer.echo("No findings recorded. Run `cms sentinel` first.")
        return
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    shown = 0
    for f in sorted(findings.values(), key=lambda f: (rank.get(f["severity"], 9), f.get("bug_id", ""))):
        if severity and f["severity"] != severity:
            continue
        if status and f["status"] != status:
            continue
        shown += 1
        loc = f"{f['file']}:{f['line']}" if f.get("line") else f.get("file") or f.get("module")
        typer.echo(f"{f.get('bug_id', f['id']):<12} {f['severity']:<8} {f['status']:<26} {loc}")
        typer.echo(f"             {f['summary']}")
    typer.echo(f"\n{shown} finding(s). Detail: cms sentinel show <bug-id>")


@sentinel_app.command("show")
def sentinel_show(finding_id: str = typer.Argument(..., help="Bug id, SEN- id, or fingerprint."),
                  root: Path = RootOption) -> None:
    """Full detail of one finding as a bug report."""
    import json as _json

    from . import config as _config
    from .sentinel.reports import as_bug_report
    from .sentinel.store import SentinelStore

    store = SentinelStore(root.resolve() / _config.MEMORY_DIR_NAME)
    for fp, f in store.load_findings().items():
        if finding_id in (fp, f.get("id"), f.get("bug_id")):
            typer.echo(_json.dumps(as_bug_report(f), indent=2))
            return
    typer.echo(f"No finding {finding_id!r}.", err=True)
    raise typer.Exit(1)


@sentinel_app.command("status")
def sentinel_status(
    finding_id: str = typer.Argument(..., help="Bug id, SEN- id, or fingerprint."),
    new_status: str = typer.Argument(..., help="open | acknowledged | fixed_pending_verification | resolved | false_positive"),
    reason: str = typer.Option("", "--reason", help="Required when marking false_positive."),
    root: Path = RootOption,
) -> None:
    """Change a finding's status (false positives require --reason)."""
    from . import config as _config
    from .sentinel.store import SentinelStore

    store = SentinelStore(root.resolve() / _config.MEMORY_DIR_NAME)
    try:
        updated = store.set_status(finding_id, new_status, reason)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    if updated is None:
        typer.echo(f"No finding {finding_id!r}.", err=True)
        raise typer.Exit(1)
    typer.echo(f"{updated.get('bug_id', updated['id'])} -> {new_status}"
               + (f" ({reason})" if reason else ""))


@sentinel_app.command("export")
def sentinel_export(
    root: Path = RootOption,
    fmt: str = typer.Option("md", "--format", "-f", help="md | json"),
) -> None:
    """Export the Sentinel report to .memory/sentinel/reports/."""
    from . import config as _config
    from .sentinel.reports import write_export
    from .sentinel.store import SentinelStore

    root = root.resolve()
    store = SentinelStore(root / _config.MEMORY_DIR_NAME)
    out = write_export(root / _config.MEMORY_DIR_NAME, store.latest_scan(), store.load_findings(), fmt=fmt)
    typer.echo(f"Written {out}")


@sentinel_app.command("ledger-init")
def sentinel_ledger_init(
    root: Path = RootOption,
    overwrite: bool = typer.Option(False, "--overwrite", help="Regenerate over an existing ledger."),
) -> None:
    """Generate docs/feature_ledger.json from real graph evidence (conservative statuses)."""
    from .sentinel.ledger import init_ledger

    try:
        out = init_ledger(root.resolve(), overwrite=overwrite)
    except FileExistsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo(f"Ledger written to {out}")


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


align_app = typer.Typer(
    help="Change alignment — did this change do what it was meant to? (intent vs the diff)",
    no_args_is_help=False,
    invoke_without_command=True,
)
app.add_typer(align_app, name="align")


@align_app.callback(invoke_without_command=True)
def _align_default(
    ctx: typer.Context,
    goal: str = typer.Argument(None, help="What this change is meant to do (else inferred from branch/commit)."),
    root: Path = RootOption,
    base: str = typer.Option("HEAD", "--base", help="Git base to diff against (e.g. main for a branch/PR)."),
    scan: bool = typer.Option(False, "--scan", help="Refresh Sentinel before judging."),
    as_json: bool = typer.Option(False, "--json", help="Print the alignment record as JSON."),
) -> None:
    """`cms align "<goal>"` — capture intent, verdict the diff, gate on drift."""
    if ctx.invoked_subcommand is not None:
        return
    root = root.resolve()
    if not (_memory_dir(root) / "graph.json").is_file():
        typer.echo("No graph.json — run `cms run-all` first.", err=True)
        raise typer.Exit(1)

    from .align import AlignStore, build_alignment
    from .intent import capture_intent
    from .memory import CodebaseMemory

    pack = capture_intent(root, goal=goal, base=base)
    mem = CodebaseMemory.load(_memory_dir(root) / "graph.json")
    record = build_alignment(mem, root, pack, base=base, scan=scan)
    AlignStore(_memory_dir(root)).save_alignment(record)

    if as_json:
        import json as _json

        typer.echo(_json.dumps(record, indent=1))
    else:
        typer.echo(f"\nIntent: {record['intent']}  (source: {record['intent_source']}, base: {record['base']})")
        typer.echo(f"Verdict: {record['verdict'].upper()} — {record['headline']}")
        typer.echo(f"\nChanged files ({len(record['changed'])}):")
        for p in record["changed"][:20]:
            typer.echo(f"  - {p}")
        if record["touched_features"]:
            typer.echo(f"\nTouched features: {', '.join(record['touched_features'])}")
        if record["gaps"]:
            typer.echo("\nGaps:")
            for g in record["gaps"][:12]:
                typer.echo(f"  ! {g}")
        if record["findings"]:
            typer.echo("\nSentinel findings on changed files:")
            for f in record["findings"][:8]:
                typer.echo(f"  [{f['severity']}] {f['id']} {f['file']} — {f['summary']}")
        if record["tests_to_run"]:
            typer.echo("\nProve it landed:\n  pytest " + " ".join(record["tests_to_run"][:8]))
        else:
            typer.echo("\nProve it landed:  (no mapped tests — add coverage for the change)")
    raise typer.Exit(1 if record["verdict"] == "drift" else 0)


@align_app.command("status")
def align_status(root: Path = RootOption) -> None:
    """Show the active captured intent and the latest alignment verdict."""
    from .align import AlignStore

    store = AlignStore(_memory_dir(root.resolve()))
    intent = store.load_intent()
    latest = store.latest()
    if intent:
        typer.echo(f"Active intent: {intent.get('task')}  (source: {intent.get('intent_source', '?')})")
    else:
        typer.echo("No intent captured yet — run `cms align \"<goal>\"`.")
    if latest:
        typer.echo(f"Latest verdict: {latest['verdict'].upper()} — {latest['headline']}")
        typer.echo(f"  at {latest['generated_at']}, {len(latest.get('changed', []))} file(s), {len(latest.get('gaps', []))} gap(s)")


@align_app.command("history")
def align_history(
    root: Path = RootOption,
    limit: int = typer.Option(15, "--limit", "-n", help="How many past sessions to show."),
) -> None:
    """Trajectory: past alignment verdicts, newest last."""
    from .align import AlignStore

    history = AlignStore(_memory_dir(root.resolve())).history()
    if not history:
        typer.echo("No alignment history yet.")
        return
    for h in history[-limit:]:
        typer.echo(
            f"{h.get('generated_at', '?')}  {h.get('verdict', '?').upper():<10} "
            f"{h.get('changed', 0)} file(s)  {h.get('gaps', 0)} gap(s)  — {h.get('intent', '')[:60]}"
        )


scope_app = typer.Typer(help="Scope — limit which subdirs/files the memory processes (saves API cost).",
                        no_args_is_help=True)
app.add_typer(scope_app, name="scope")


@scope_app.command("show")
def scope_show(root: Path = RootOption) -> None:
    """Print the active scope (which dirs/files are processed)."""
    from .scope import load_scope

    inc = load_scope(root.resolve())
    if not inc:
        typer.echo("Scope: whole codebase (no .cmsscope.json).")
    else:
        typer.echo(f"Scope: {len(inc)} selection(s) — only these are processed:")
        for x in sorted(inc):
            typer.echo(f"  - {x}")


@scope_app.command("set")
def scope_set(
    paths: list[str] = typer.Argument(..., help="Dirs (end with /) or files to include, relative to root."),
    root: Path = RootOption,
) -> None:
    """Restrict processing to these paths. Re-run `cms update` to apply."""
    from .scope import save_scope

    out = save_scope(root.resolve(), paths)
    typer.echo(f"Scope saved to {out.name} ({len(paths)} selection(s)). Run `cms update` to apply.")


@scope_app.command("clear")
def scope_clear(root: Path = RootOption) -> None:
    """Remove the scope — process the whole codebase again."""
    from .scope import clear_scope

    typer.echo("Scope cleared." if clear_scope(root.resolve()) else "No scope was set.")


bundle_app = typer.Typer(help="Bundle — share the AI-generated memory so others view it without re-processing.",
                         no_args_is_help=True)
app.add_typer(bundle_app, name="bundle")


@bundle_app.command("export")
def bundle_export(
    root: Path = RootOption,
    source: bool = typer.Option(False, "--source", help="Include a snapshot of the scoped source (fully self-contained)."),
    out: Path = typer.Option(None, "--out", "-o", help="Output .cmsbundle path."),
) -> None:
    """Package .memory/ (+ optional source) into a shareable .cmsbundle."""
    from .bundle import export_bundle

    root = root.resolve()
    if not (_memory_dir(root) / "graph.json").is_file():
        typer.echo("No graph.json — run `cms run-all` first.", err=True)
        raise typer.Exit(1)
    path = export_bundle(root, out_path=out, include_source=source, echo=typer.echo)
    typer.echo(f"\nShare {path.name} — the recipient runs `cms bundle open {path.name}` (no API key needed).")


@bundle_app.command("info")
def bundle_info(bundle: Path = typer.Argument(..., help="Path to a .cmsbundle.")) -> None:
    """Show a bundle's manifest without unpacking it."""
    from .bundle import read_manifest

    m = read_manifest(bundle)
    if not m:
        typer.echo("Not an Atlas bundle (no manifest).", err=True)
        raise typer.Exit(1)
    typer.echo(f"Project     : {m.get('name')}")
    typer.echo(f"Generated   : {m.get('generated_at')}  (Atlas {m.get('cms_version')})")
    typer.echo(f"Memory files: {m.get('memory_file_count')}")
    typer.echo(f"Source      : {'included (' + str(m.get('source_file_count')) + ' files)' if m.get('has_source') else 'not included'}")
    if m.get("scope"):
        typer.echo(f"Scope       : {', '.join(m['scope'])}")


@bundle_app.command("open")
def bundle_open(
    bundle: Path = typer.Argument(..., help="Path to a .cmsbundle."),
    dest: Path = typer.Option(None, "--dest", help="Where to unpack (default: alongside the bundle)."),
    port: int = typer.Option(7717, "--port", help="Port to serve the viewer on."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open the browser."),
    serve_ui: bool = typer.Option(True, "--serve/--no-serve", help="Serve the viewer after opening."),
) -> None:
    """Unpack a received bundle and view it — no API key, no re-processing."""
    from .bundle import open_bundle, read_manifest

    manifest = read_manifest(bundle)
    if not manifest:
        typer.echo("Not an Atlas bundle (no manifest).", err=True)
        raise typer.Exit(1)
    if dest is None:
        dest = bundle.resolve().parent / f"{manifest.get('name', 'atlas')}-bundle"
    out = open_bundle(bundle, dest, echo=typer.echo)
    if not manifest.get("has_source"):
        typer.echo("Note: no source in this bundle — summaries/graph/features work; raw-code view is unavailable.")
    if serve_ui:
        from .ui import serve
        serve(out, port=port, open_browser=not no_browser)
    else:
        typer.echo(f"Opened at {out}. View with:  cms ui --root {out}")


if __name__ == "__main__":
    app()
