"""Lightweight JavaScript / TypeScript parser (regex-based).

Not a full AST — a pragmatic extractor so JS/TS projects get real structure in
the knowledge graph without a heavy tree-sitter dependency. Pulls out top-level
declarations (functions, classes, interfaces/types/enums, arrow/function
consts) as components, and import / require / export-from specifiers as imports
(resolved to file nodes by the graph builder). ~80% accurate — good enough to
turn a file list into a connected map.
"""

from __future__ import annotations

import re

_CLASS = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"
                    r"(?:\s+extends\s+([A-Za-z_$][\w$.]*))?")
_TYPEISH = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)")
_FUNC = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)")
_CONST = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=]+?)?=\s*(.*)$")

_IMPORT_FROM = re.compile(r"""(?:import|export)\b[^;'"]*?\bfrom\s*['"]([^'"]+)['"]""")
_IMPORT_BARE = re.compile(r"""^\s*import\s+['"]([^'"]+)['"]""", re.M)
_REQUIRE = re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_DYNIMPORT = re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)""")

_LINE_COMMENT = re.compile(r"//.*$")
# named ES import bindings: `import Default, { a, b as c } from './x'`
_IMPORT_CLAUSE = re.compile(
    r"^\s*import\s+(?:type\s+)?([^;'\"]+?)\s+from\s*['\"]([^'\"]+)['\"]", re.M)
_CALL_SITE = re.compile(r"(?:\bnew\s+)?\b([A-Za-z_$][\w$]*)\s*\(")
_JS_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "await",
    "function", "constructor", "super", "import", "require", "new", "throw",
    "delete", "void", "yield", "in", "of", "do", "else", "try", "finally",
}


def _named_bindings(clause: str) -> list[tuple[str, str]]:
    """'D, { a, b as c }' -> [(local, original), ...]; namespace imports skipped."""
    out: list[tuple[str, str]] = []
    brace = re.search(r"\{([^}]*)\}", clause)
    if brace:
        for part in brace.group(1).split(","):
            part = part.strip()
            if not part or part.startswith("type "):
                continue
            m = re.match(r"([A-Za-z_$][\w$]*)(?:\s+as\s+([A-Za-z_$][\w$]*))?$", part)
            if m:
                out.append((m.group(2) or m.group(1), m.group(1)))
    head = clause.split("{")[0].strip().rstrip(",").strip()
    if head and re.fullmatch(r"[A-Za-z_$][\w$]*", head):
        out.append((head, "default"))  # default import: local -> 'default'
    return out


def _looks_functionlike(rhs: str) -> bool:
    r = rhs.strip()
    return ("=>" in r) or r.startswith("function") or " function" in r or r.endswith("(") or r.endswith("{")


def parse_js(rel_path: str, source: str):
    """Return (components, import_specifiers) for a JS/TS source string."""
    from .graph_builder import _Component

    lines = source.splitlines()
    n = len(lines)

    def end_of_block(start_idx: int) -> int:
        depth = 0
        started = False
        for i in range(start_idx, min(n, start_idx + 1200)):
            code = _LINE_COMMENT.sub("", lines[i])
            for ch in code:
                if ch == "{":
                    depth += 1
                    started = True
                elif ch == "}":
                    depth -= 1
            if started and depth <= 0:
                return i + 1
        return start_idx + 1

    comps: list[_Component] = []
    seen: set[str] = set()
    for idx, line in enumerate(lines):
        kind = name = None
        bases: list[str] = []
        mcls = _CLASS.match(line)
        if mcls:
            kind, name = "class", mcls.group(1)
            if mcls.group(2):
                bases = [mcls.group(2).split(".")[-1]]
        elif (mt := _TYPEISH.match(line)):
            kind, name = "class", mt.group(1)
        elif (mf := _FUNC.match(line)):
            kind, name = "func", mf.group(1)
        elif (mc := _CONST.match(line)) and _looks_functionlike(mc.group(2)):
            kind, name = "func", mc.group(1)
        if not (kind and name) or name in seen:
            continue
        seen.add(name)
        start = idx + 1
        end = end_of_block(idx) if "{" in line or kind == "class" else start
        comps.append(_Component(
            kind=kind, name=name, qualname=name,
            start_line=start, end_line=max(end, start),
            signature=line.strip()[:140],
            bases=bases,
        ))

    specs: list[str] = []
    for pat in (_IMPORT_FROM, _REQUIRE, _DYNIMPORT):
        specs += pat.findall(source)
    specs += _IMPORT_BARE.findall(source)
    seen_s: set[str] = set()
    imports = [s for s in specs if not (s in seen_s or seen_s.add(s))]

    # named import bindings: local name -> (specifier, original export name)
    named_imports: dict[str, tuple[str, str]] = {}
    for clause, spec in _IMPORT_CLAUSE.findall(source):
        for local, orig in _named_bindings(clause):
            named_imports[local] = (spec, orig)

    # call sites: names invoked inside each component's span, filtered to
    # KNOWN candidates (same-file declarations + named imports) — precision
    # over recall; these become provenance=heuristic CALLS edges.
    candidates = {c.name for c in comps} | set(named_imports)
    calls: list[tuple[str, tuple]] = []
    seen_calls: set[tuple[str, str]] = set()
    for comp in comps:
        body = "\n".join(lines[comp.start_line - 1:comp.end_line])
        body = _LINE_COMMENT.sub("", body)
        for m in _CALL_SITE.finditer(body):
            callee = m.group(1)
            if callee in _JS_KEYWORDS or callee == comp.name or callee not in candidates:
                continue
            key = (comp.qualname, callee)
            if key not in seen_calls:
                seen_calls.add(key)
                calls.append((comp.qualname, ("name", callee)))

    return comps, imports, named_imports, calls
