"""JavaScript / TypeScript parsing + graph construction."""

from pathlib import Path

from cms.graph_builder import build_graph
from cms.js_parser import parse_js
from cms.scanner import scan

TS_MAIN = '''\
import React from "react";
import { helper } from "./util";
import App from "./app/App";
import "@scope/pkg/styles.css";

export function greet(name: string) { return helper(name); }
export const Widget = () => { return null; };
export default class Root { render() { return null; } }
interface Props { x: number; }
type Id = string;
'''

UTIL = "export function helper(s) { return s; }\n"
APP = "export default function App() { return null; }\n"


def test_parse_js_components_and_imports():
    comps, imports, named, calls = parse_js("src/main.tsx", TS_MAIN)
    kinds = {c.name: c.kind for c in comps}
    assert {"greet", "Widget", "Root", "Props", "Id"} <= set(kinds)
    assert kinds["greet"] == "func" and kinds["Widget"] == "func"
    assert kinds["Root"] == "class" and kinds["Props"] == "class" and kinds["Id"] == "class"
    assert set(imports) == {"react", "./util", "./app/App", "@scope/pkg/styles.css"}
    root = next(c for c in comps if c.name == "Root")
    assert root.end_line >= root.start_line  # block end resolved
    # named import bindings resolved to (specifier, original)
    assert named["helper"] == ("./util", "helper")
    assert named["App"] == ("./app/App", "default")
    # greet's body calls the imported helper — captured as a call site
    assert ("greet", ("name", "helper")) in calls


def test_parse_js_calls_and_extends():
    src = (
        "import { fetchData } from './api';\n"
        "export class Base { run() { return 1; } }\n"
        "export class Worker extends Base {\n"
        "  async work() { const d = await fetchData(); return process(d); }\n"
        "}\n"
        "function process(d) { return d; }\n"
        "function lonely() { return unknownFn(); }\n"
    )
    comps, _, named, calls = parse_js("w.ts", src)
    worker = next(c for c in comps if c.name == "Worker")
    assert worker.bases == ["Base"]
    assert ("Worker", ("name", "fetchData")) in calls   # imported callee
    assert ("Worker", ("name", "process")) in calls     # same-file callee
    # unknown names are NOT guessed into edges (precision over recall)
    assert not any(c[1][1] == "unknownFn" for c in calls)


def _ts_project(tmp_path: Path) -> Path:
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "main.tsx").write_text(TS_MAIN, encoding="utf-8")
    (tmp_path / "src" / "util.ts").write_text(UTIL, encoding="utf-8")
    (tmp_path / "src" / "app" / "App.tsx").write_text(APP, encoding="utf-8")
    return tmp_path


def test_build_graph_resolves_ts_structure(tmp_path):
    g = build_graph(scan(_ts_project(tmp_path)))
    # components become nodes
    assert g.has_node("func:src/main.tsx::greet")
    assert g.has_node("class:src/main.tsx::Root")
    assert g.has_edge("file:src/main.tsx", "func:src/main.tsx::greet")  # CONTAINS
    # relative imports resolve to real files (with extension / nested path)
    assert g.has_edge("file:src/main.tsx", "file:src/util.ts")
    assert g.has_edge("file:src/main.tsx", "file:src/app/App.tsx")
    # bare specifiers become external nodes (scoped packages keep @scope/pkg)
    assert g.has_node("ext:react") and g.has_edge("file:src/main.tsx", "ext:react")
    assert g.has_node("ext:@scope/pkg")


def test_build_graph_ts_calls_and_inherits(tmp_path):
    (tmp_path / "api.ts").write_text("export function fetchData() { return 1; }\n",
                                     encoding="utf-8")
    (tmp_path / "base.ts").write_text("export class Base { }\n", encoding="utf-8")
    (tmp_path / "worker.ts").write_text(
        "import { fetchData } from './api';\n"
        "import { Base } from './base';\n"
        "export class Worker extends Base {\n"
        "  work() { return fetchData(); }\n"
        "}\n"
        "export function local() { return fetchData(); }\n",
        encoding="utf-8")
    g = build_graph(scan(tmp_path))
    # cross-file CALLS via named import, provenance heuristic
    assert g.edges["func:worker.ts::local", "func:api.ts::fetchData"]["type"] == "CALLS"
    assert g.edges["func:worker.ts::local", "func:api.ts::fetchData"]["provenance"] == "heuristic"
    assert g.has_edge("class:worker.ts::Worker", "func:api.ts::fetchData")
    # extends across files -> INHERITS
    assert g.edges["class:worker.ts::Worker", "class:base.ts::Base"]["type"] == "INHERITS"


def test_non_code_file_still_gets_bare_node(tmp_path):
    (tmp_path / "notes.md").write_text("# hi\n", encoding="utf-8")
    g = build_graph(scan(tmp_path))
    assert g.has_node("file:notes.md")
    # markdown isn't structurally parsed -> no components, but node exists for summaries
    assert not any(d.get("type") == "CONTAINS" for _, _, d in g.out_edges("file:notes.md", data=True))
