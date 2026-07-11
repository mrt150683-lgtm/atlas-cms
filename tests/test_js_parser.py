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
    comps, imports = parse_js("src/main.tsx", TS_MAIN)
    kinds = {c.name: c.kind for c in comps}
    assert {"greet", "Widget", "Root", "Props", "Id"} <= set(kinds)
    assert kinds["greet"] == "func" and kinds["Widget"] == "func"
    assert kinds["Root"] == "class" and kinds["Props"] == "class" and kinds["Id"] == "class"
    assert set(imports) == {"react", "./util", "./app/App", "@scope/pkg/styles.css"}
    root = next(c for c in comps if c.name == "Root")
    assert root.end_line >= root.start_line  # block end resolved


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


def test_non_code_file_still_gets_bare_node(tmp_path):
    (tmp_path / "notes.md").write_text("# hi\n", encoding="utf-8")
    g = build_graph(scan(tmp_path))
    assert g.has_node("file:notes.md")
    # markdown isn't structurally parsed -> no components, but node exists for summaries
    assert not any(d.get("type") == "CONTAINS" for _, _, d in g.out_edges("file:notes.md", data=True))
