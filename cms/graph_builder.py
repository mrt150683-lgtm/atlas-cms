"""Phase 2: AST-based knowledge graph builder.

Parses Python files into a networkx DiGraph of File / Class / Function nodes
connected by CONTAINS / IMPORTS / CALLS / INHERITS edges. Call and inheritance
resolution is best-effort static analysis: bare names, ``self.method``, and
``module.func`` via each file's import table.

Node id scheme:
    file:{rel_path}
    class:{rel_path}::{qualname}
    func:{rel_path}::{qualname}
    ext:{top_level_module}
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import networkx as nx

from .anchors import MAX_ATTACH_GAP, AnchorGroup, merge_anchor_dicts, parse_anchors
from .scanner import FileRecord


@dataclass
class _Component:
    kind: str  # "class" | "func"
    name: str
    qualname: str
    start_line: int
    end_line: int
    signature: str = ""
    docstring: str = ""
    bases: list[str] = field(default_factory=list)
    parent_scope: str | None = None  # qualname of enclosing class/func, None if top-level
    parent_is_class: bool = False


@dataclass
class _FileInfo:
    record: FileRecord
    components: list[_Component] = field(default_factory=list)
    imported_modules: list[str] = field(default_factory=list)  # full dotted names
    alias_to_module: dict[str, str] = field(default_factory=dict)  # "np" -> "numpy"
    from_imports: dict[str, tuple[str, str]] = field(default_factory=dict)  # local -> (module, orig)
    calls: list[tuple[str, tuple]] = field(default_factory=list)  # (caller qualname, callee descriptor)
    anchor_groups: list[AnchorGroup] = field(default_factory=list)
    js_imports: list[str] = field(default_factory=list)  # raw JS/TS import specifiers
    parse_error: str | None = None


JS_LANGS = {"javascript", "typescript", "javascript-react", "typescript-react"}


def _module_name(rel_path: str) -> str:
    p = PurePosixPath(rel_path)
    parts = p.parts[:-1] if p.name == "__init__.py" else p.parts[:-1] + (p.stem,)
    return ".".join(parts)


class _Collector(ast.NodeVisitor):
    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.components: list[_Component] = []
        self.imported_modules: list[str] = []
        self.alias_to_module: dict[str, str] = {}
        self.from_imports: dict[str, tuple[str, str]] = {}
        self.calls: list[tuple[str, tuple]] = []
        self._scopes: list[tuple[str, str]] = []  # (kind, qualname)

    # -- scopes --------------------------------------------------------

    def _add_component(self, node: ast.AST, kind: str, name: str, **extra) -> _Component:
        parent = self._scopes[-1] if self._scopes else None
        comp = _Component(
            kind=kind,
            name=name,
            qualname=(parent[1] + "." + name) if parent else name,
            start_line=node.lineno,
            end_line=getattr(node, "end_lineno", node.lineno),
            docstring=ast.get_docstring(node) or "",
            parent_scope=parent[1] if parent else None,
            parent_is_class=bool(parent and parent[0] == "class"),
            **extra,
        )
        self.components.append(comp)
        return comp

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        comp = self._add_component(
            node, "class", node.name,
            bases=[ast.unparse(b) for b in node.bases],
        )
        for dec in node.decorator_list:
            self.visit(dec)
        self._scopes.append(("class", comp.qualname))
        for child in node.body:
            self.visit(child)
        self._scopes.pop()

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        comp = self._add_component(
            node, "func", node.name,
            signature=f"{prefix} {node.name}({ast.unparse(node.args)})",
        )
        for dec in node.decorator_list:
            self.visit(dec)
        self._scopes.append(("func", comp.qualname))
        for child in node.body:
            self.visit(child)
        self._scopes.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    # -- imports -------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imported_modules.append(alias.name)
            local = alias.asname or alias.name.split(".")[0]
            self.alias_to_module[local] = alias.name if alias.asname else alias.name.split(".")[0]

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:  # relative import: resolve against this file's package
            pkg_parts = list(PurePosixPath(self.rel_path).parts[:-1])
            base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
            module = ".".join(base + ([module] if module else []))
        if module:
            self.imported_modules.append(module)
            for alias in node.names:
                if alias.name != "*":
                    self.from_imports[alias.asname or alias.name] = (module, alias.name)

    # -- calls ---------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        caller = next((q for k, q in reversed(self._scopes) if k == "func"), None)
        if caller is not None:
            fn = node.func
            if isinstance(fn, ast.Name):
                self.calls.append((caller, ("name", fn.id)))
            elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                self.calls.append((caller, ("attr", fn.value.id, fn.attr)))
        self.generic_visit(node)


def _parse_file(rec: FileRecord) -> _FileInfo | None:
    """Parse a source file into structure. Python via AST, JS/TS via the light
    regex parser; returns None for languages we don't structurally parse (they
    still get a bare file node + an AI summary)."""
    if rec.language == "python":
        info = _FileInfo(record=rec)
        try:
            source = Path(rec.abs_path).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (SyntaxError, OSError) as exc:
            info.parse_error = str(exc)
            return info
        collector = _Collector(rec.rel_path)
        collector.visit(tree)
        info.anchor_groups = parse_anchors(source)
        info.components = collector.components
        info.imported_modules = collector.imported_modules
        info.alias_to_module = collector.alias_to_module
        info.from_imports = collector.from_imports
        info.calls = collector.calls
        return info
    if rec.language in JS_LANGS:
        from .js_parser import parse_js

        info = _FileInfo(record=rec)
        try:
            source = Path(rec.abs_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            info.parse_error = str(exc)
            return info
        info.components, info.js_imports, named, info.calls = parse_js(rec.rel_path, source)
        # named import bindings share from_imports' shape: local -> (module, orig);
        # resolve_from_import routes JS "modules" (specifiers) via _resolve_js_import
        info.from_imports = named
        info.anchor_groups = parse_anchors(source)
        return info
    return None


def _resolve_js_import(from_rel: str, spec: str, file_set: set[str]) -> str | None:
    """Resolve a relative JS/TS import specifier to a scanned file, trying the
    usual extension and /index resolutions. Bare specifiers return None."""
    from posixpath import dirname, join, normpath

    if not spec.startswith("."):
        return None
    target = normpath(join(dirname(from_rel), spec))
    for ext in ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".css", ".scss"):
        if (cand := target + ext) in file_set:
            return cand
    for ext in (".ts", ".tsx", ".js", ".jsx", ".json"):
        if (cand := normpath(join(target, "index" + ext))) in file_set:
            return cand
    return None


def _bare_pkg(spec: str) -> str:
    if spec.startswith("@"):
        return "/".join(spec.split("/")[:2])
    return spec.split("/")[0]


# @memory:feature:KnowledgeGraphConstruction
# @memory:connects:CleanDirectoryScanner, SummaryGenerator, QueryEngine, MemoryAnchors
# @memory:summary:Two-pass build — register file/class/function nodes and attach anchors, then resolve IMPORTS/CALLS/INHERITS edges via module and definition indexes.
def build_graph(records: list[FileRecord]) -> nx.DiGraph:
    graph = nx.DiGraph()
    infos: dict[str, _FileInfo] = {}
    module_index: dict[str, str] = {}  # dotted module -> rel_path

    # pass 1: parse and register nodes
    for rec in records:
        graph.add_node(
            f"file:{rec.rel_path}",
            type="file",
            name=PurePosixPath(rec.rel_path).name,
            path=rec.rel_path,
            language=rec.language,
            line_count=rec.line_count,
            size_bytes=rec.size_bytes,
            mtime=rec.mtime,
            summary="",
        )
        if rec.language == "python":
            module_index[_module_name(rec.rel_path)] = rec.rel_path
        info = _parse_file(rec)
        if info is None:  # unparsed language — bare file node only (still summarised)
            continue
        infos[rec.rel_path] = info
        # Edge provenance: exact syntax tree vs pattern-based extraction. Every
        # edge carries where it came from so consumers can weigh confidence.
        prov = "ast" if rec.language == "python" else "heuristic"
        for comp in info.components:
            node_id = f"{comp.kind}:{rec.rel_path}::{comp.qualname}"
            graph.add_node(
                node_id,
                type=comp.kind,
                name=comp.name,
                qualname=comp.qualname,
                path=rec.rel_path,
                start_line=comp.start_line,
                end_line=comp.end_line,
                signature=comp.signature,
                docstring=comp.docstring,
                summary="",
            )
            if comp.parent_scope:
                # link from the enclosing class/func; its node kind is unknown here,
                # so probe both id forms (class first — methods are the common case)
                for parent_kind in ("class", "func"):
                    parent_id = f"{parent_kind}:{rec.rel_path}::{comp.parent_scope}"
                    if graph.has_node(parent_id):
                        graph.add_edge(parent_id, node_id, type="CONTAINS", provenance=prov)
                        break
            else:
                graph.add_edge(f"file:{rec.rel_path}", node_id, type="CONTAINS", provenance=prov)

        # attach memory anchors: line-form groups bind to the component that
        # starts just below them; module tags and orphans bind to the file
        file_anchors: dict = {}
        for group in info.anchor_groups:
            target_comp = None
            if not group.is_file_level:
                candidates = [
                    c for c in info.components
                    if 0 < c.start_line - group.end_line <= MAX_ATTACH_GAP
                ]
                if candidates:
                    target_comp = min(candidates, key=lambda c: c.start_line)
            if target_comp is not None:
                comp_id = f"{target_comp.kind}:{rec.rel_path}::{target_comp.qualname}"
                merged = merge_anchor_dicts(
                    graph.nodes[comp_id].get("anchors", {}), group.to_dict()
                )
                graph.nodes[comp_id]["anchors"] = merged
            else:
                merge_anchor_dicts(file_anchors, group.to_dict())
        if file_anchors:
            graph.nodes[f"file:{rec.rel_path}"]["anchors"] = file_anchors

    # helper indexes for resolution
    top_level: dict[tuple[str, str], str] = {}  # (rel_path, name) -> node_id
    by_qualname: dict[tuple[str, str], str] = {}  # (rel_path, qualname) -> node_id
    comp_by_qual: dict[tuple[str, str], _Component] = {}
    for rel, info in infos.items():
        for comp in info.components:
            node_id = f"{comp.kind}:{rel}::{comp.qualname}"
            by_qualname[(rel, comp.qualname)] = node_id
            comp_by_qual[(rel, comp.qualname)] = comp
            if comp.parent_scope is None:
                top_level[(rel, comp.name)] = node_id

    def resolve_module(module: str) -> str | None:
        """Dotted module -> rel_path of a scanned file, trying parent packages."""
        parts = module.split(".")
        for i in range(len(parts), 0, -1):
            rel = module_index.get(".".join(parts[:i]))
            if rel is not None:
                return rel
        return None

    def resolve_from_import(info: _FileInfo, local_name: str) -> str | None:
        """Local name bound by an import -> node id in a scanned file.
        Python: `from mod import name` via the dotted-module index.
        JS/TS: `import { name } from './x'` via specifier resolution; a
        default import falls back to the target file's same-named export."""
        entry = info.from_imports.get(local_name)
        if not entry:
            return None
        module, orig = entry
        if info.record.language in JS_LANGS:
            target_rel = _resolve_js_import(info.record.rel_path, module, file_set)
            if target_rel is None:
                return None
            return (top_level.get((target_rel, orig))
                    or top_level.get((target_rel, local_name)))
        target_rel = resolve_module(module)
        if target_rel is None:
            return None
        return top_level.get((target_rel, orig))

    file_set = {rec.rel_path for rec in records}

    # pass 2: IMPORTS / CALLS / INHERITS edges
    for rel, info in infos.items():
        file_id = f"file:{rel}"

        # JS/TS: resolve relative specifiers to files, bare specifiers to externals
        # (regex-extracted + convention-resolved -> provenance "heuristic")
        for spec in info.js_imports:
            target_rel = _resolve_js_import(rel, spec, file_set)
            if target_rel is not None:
                if target_rel != rel:
                    graph.add_edge(file_id, f"file:{target_rel}", type="IMPORTS",
                                   provenance="heuristic")
            elif not spec.startswith("."):
                pkg = _bare_pkg(spec)
                ext_id = f"ext:{pkg}"
                if not graph.has_node(ext_id):
                    graph.add_node(ext_id, type="external", name=pkg, summary="")
                graph.add_edge(file_id, ext_id, type="IMPORTS", provenance="heuristic")

        # Python import statements are exact AST facts -> provenance "ast"
        for module in info.imported_modules:
            target_rel = resolve_module(module)
            if target_rel is not None:
                if target_rel != rel:
                    graph.add_edge(file_id, f"file:{target_rel}", type="IMPORTS",
                                   provenance="ast")
            else:
                ext_id = f"ext:{module.split('.')[0]}"
                if not graph.has_node(ext_id):
                    graph.add_node(ext_id, type="external", name=module.split(".")[0], summary="")
                graph.add_edge(file_id, ext_id, type="IMPORTS", provenance="ast")

        for caller_qual, callee in info.calls:
            caller_id = by_qualname.get((rel, caller_qual))
            caller_comp = comp_by_qual.get((rel, caller_qual))
            if caller_id is None or caller_comp is None:
                continue
            target_id: str | None = None
            if callee[0] == "name":
                name = callee[1]
                target_id = top_level.get((rel, name)) or resolve_from_import(info, name)
            else:  # ("attr", base, attr)
                _, base, attr = callee
                if base == "self" and caller_comp.parent_is_class:
                    target_id = by_qualname.get((rel, f"{caller_comp.parent_scope}.{attr}"))
                elif base in info.alias_to_module:
                    target_rel = resolve_module(info.alias_to_module[base])
                    if target_rel is not None:
                        target_id = top_level.get((target_rel, attr))
            if target_id is not None and target_id != caller_id:
                # call SITES are AST facts, but target resolution is name-based
                # best-effort -> the edge as a whole is "heuristic"
                graph.add_edge(caller_id, target_id, type="CALLS", provenance="heuristic")

        for comp in info.components:
            if comp.kind != "class":
                continue
            class_id = f"class:{rel}::{comp.qualname}"
            for base_expr in comp.bases:
                target_id = None
                if "." not in base_expr:
                    target_id = top_level.get((rel, base_expr)) or resolve_from_import(info, base_expr)
                else:
                    mod_part, _, cls_part = base_expr.rpartition(".")
                    if mod_part in info.alias_to_module:
                        target_rel = resolve_module(info.alias_to_module[mod_part])
                        if target_rel is not None:
                            target_id = top_level.get((target_rel, cls_part))
                if target_id is not None and graph.nodes[target_id].get("type") == "class":
                    graph.add_edge(class_id, target_id, type="INHERITS", provenance="heuristic")

    return graph


# v2: system:/component: hierarchy nodes + their PART_OF edges may be present.
# Purely additive — v1 readers ignore them, and absence of the field means v1.
GRAPH_SCHEMA_VERSION = 2


def graph_to_json(graph: nx.DiGraph) -> dict:
    graph.graph["schema_version"] = GRAPH_SCHEMA_VERSION
    try:
        return nx.node_link_data(graph, edges="links")
    except TypeError:  # older networkx without the `edges` kwarg
        return nx.node_link_data(graph)


def graph_from_json(data: dict) -> nx.DiGraph:
    try:
        graph = nx.node_link_graph(data, directed=True, edges="links")
    except TypeError:
        graph = nx.node_link_graph(data, directed=True)
    # Migrate graphs written before the rename: coverage evidence used to be
    # stored as "verified_by"; the honest name is "exercised_by" (coverage
    # proves execution, not correctness).
    for _, attrs in graph.nodes(data=True):
        if "verified_by" in attrs and "exercised_by" not in attrs:
            attrs["exercised_by"] = attrs.pop("verified_by")
    return graph
