"""Deterministic drift checks for developer-authored ``@memory`` anchors.

Anchor Drift compares the human statement stored on a graph node with two
pieces of current structural evidence: the node's source slice and the graph
relationships between declared features.  It intentionally prefers silence
to speculation; every emitted finding is high-confidence and LLM-free.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import networkx as nx

from .anchors import parse_anchors
from .config import LANGUAGE_BY_EXTENSION


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BACKTICK_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
_WORD_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_])")


@dataclass(frozen=True)
class AnchorDriftFinding:
    """One deterministic mismatch between an anchor and current evidence."""

    kind: str
    node_id: str
    message: str
    path: str = ""
    line: int | None = None
    anchor: str = ""
    symbol: str = ""
    feature: str = ""
    related_feature: str = ""
    confidence: str = "high"
    evidence: list[str] = field(default_factory=list)


@dataclass
class AnchorDriftReport:
    """JSON-ready result shared by the CLI, MCP server, HTTP API and Sentinel."""

    target: str | None
    anchored_nodes: int
    findings: list[AnchorDriftFinding] = field(default_factory=list)

    @property
    def high_confidence_count(self) -> int:
        return sum(1 for finding in self.findings if finding.confidence == "high")

    def to_dict(self) -> dict:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.kind] = counts.get(finding.kind, 0) + 1
        return {
            "target": self.target,
            "anchored_nodes": self.anchored_nodes,
            "total": len(self.findings),
            "high_confidence": self.high_confidence_count,
            "signals": counts,
            "findings": [asdict(finding) for finding in self.findings],
        }


def _looks_like_clear_symbol(token: str) -> bool:
    """Conservative bare-token classifier: snake_case or multi-hump CamelCase."""
    if not _IDENTIFIER_RE.fullmatch(token):
        return False
    if re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", token):
        return True
    return (
        token[0].isupper()
        and any(char.islower() for char in token)
        and sum(char.isupper() for char in token) >= 2
    )


def _symbols(summary: str) -> list[str]:
    explicit = _BACKTICK_RE.findall(summary)
    bare = [token for token in _WORD_RE.findall(summary) if _looks_like_clear_symbol(token)]
    return list(dict.fromkeys(explicit + bare))


def _expand_members(graph: nx.DiGraph, members: set[str]) -> set[str]:
    expanded = {node for node in members if graph.has_node(node)}
    frontier = list(expanded)
    while frontier:
        current = frontier.pop()
        for _, child, data in graph.out_edges(current, data=True):
            if data.get("type") == "CONTAINS" and child not in expanded:
                expanded.add(child)
                frontier.append(child)
    return expanded


def _scope(graph: nx.DiGraph, target: str | None) -> tuple[set[str] | None, set[str] | None, str | None]:
    """Return (source nodes, feature ids, canonical target). ``None`` means all."""
    if target is None or not str(target).strip():
        return None, None, None
    raw = str(target).strip()
    node_id = raw if graph.has_node(raw) else None
    if node_id is None and graph.has_node(f"file:{raw}"):
        node_id = f"file:{raw}"
    if node_id is None:
        matches = [
            node for node, attrs in graph.nodes(data=True)
            if attrs.get("type") == "feature" and str(attrs.get("name", "")).lower() == raw.lower()
        ]
        if len(matches) == 1:
            node_id = matches[0]
    if node_id is None:
        raise ValueError(f"unknown anchor-drift target {raw!r}")

    attrs = graph.nodes[node_id]
    feature_ids: set[str] = set()
    if attrs.get("type") == "feature":
        feature_ids.add(node_id)
        nodes = _expand_members(graph, set(attrs.get("members") or []))
    elif attrs.get("type") == "file":
        path = attrs.get("path", "")
        nodes = {node for node, item in graph.nodes(data=True) if item.get("path") == path}
    else:
        nodes = {node_id}

    for node in nodes:
        for _, feature, data in graph.out_edges(node, data=True):
            if data.get("type") == "PART_OF":
                feature_ids.add(feature)
    return nodes, feature_ids, node_id


def _source_slice(root: Path, attrs: dict, cache: dict[str, tuple[list[str], set[int]]]) -> str | None:
    path = str(attrs.get("path") or "")
    if not path:
        return None
    target = (root / path).resolve()
    if root not in target.parents or target.suffix.lower() not in LANGUAGE_BY_EXTENSION or not target.is_file():
        return None
    if path not in cache:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        # The anchor declaration itself must not satisfy its own claim. Other
        # comments remain valid conservative evidence, matching get_source's text.
        anchor_lines = {
            line
            for group in parse_anchors("\n".join(lines))
            for line in range(group.start_line, group.end_line + 1)
        }
        cache[path] = lines, anchor_lines
    lines, anchor_lines = cache[path]
    start = max(1, int(attrs.get("start_line") or 1))
    end = min(int(attrs.get("end_line") or len(lines)), len(lines))
    return "\n".join("" if line in anchor_lines else lines[line - 1] for line in range(start, end + 1))


def _has_symbol(source: str, symbol: str) -> bool:
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])", source
    ) is not None


def _feature_evidence(graph: nx.DiGraph, left: str, right: str) -> list[str]:
    """Static support for a declared feature link, accepted in either direction."""
    for source, target in ((left, right), (right, left)):
        if graph.has_edge(source, target) and graph.edges[source, target].get("type") == "RELATES":
            return [str(graph.edges[source, target].get("via") or f"{source} relates to {target}")]

    left_nodes = _expand_members(graph, set(graph.nodes[left].get("members") or []))
    right_nodes = _expand_members(graph, set(graph.nodes[right].get("members") or []))
    shared = left_nodes & right_nodes
    if shared:
        return [f"shared member {sorted(shared)[0]}"]

    left_files = {f"file:{graph.nodes[node]['path']}" for node in left_nodes if graph.nodes[node].get("path")}
    right_files = {f"file:{graph.nodes[node]['path']}" for node in right_nodes if graph.nodes[node].get("path")}
    for source_nodes, target_nodes, edge_type in (
        (left_nodes, right_nodes, "CALLS"),
        (right_nodes, left_nodes, "CALLS"),
        (left_files, right_files, "IMPORTS"),
        (right_files, left_files, "IMPORTS"),
    ):
        for source in sorted(source_nodes):
            for target in sorted(target_nodes):
                if graph.has_edge(source, target) and graph.edges[source, target].get("type") == edge_type:
                    return [f"{source} {edge_type.lower()} {target}"]
    return []


# @memory:feature:AnchorDrift
# @memory:connects:MemoryAnchors, AgentMemoryAccess, MemoryViewer, HermesSentinel
# @memory:summary:Deterministically flags stale summary symbols and declared feature links that have no current source or graph evidence.
def detect_anchor_drift(
    graph: nx.DiGraph, root: Path, target: str | None = None
) -> AnchorDriftReport:
    """Compare anchored intent with current source and structural graph evidence."""
    root = Path(root).resolve()
    scoped_nodes, scoped_features, canonical_target = _scope(graph, target)
    findings: list[AnchorDriftFinding] = []
    source_cache: dict[str, tuple[list[str], set[int]]] = {}
    anchored_nodes = 0

    for node_id, attrs in sorted(graph.nodes(data=True)):
        if scoped_nodes is not None and node_id not in scoped_nodes:
            continue
        anchors = attrs.get("anchors") or {}
        summaries = anchors.get("summary") or []
        if not summaries:
            continue
        anchored_nodes += 1
        source = _source_slice(root, attrs, source_cache)
        if source is None:
            continue  # unavailable source is uncertainty, not drift
        for summary in summaries:
            for symbol in _symbols(str(summary)):
                if _has_symbol(source, symbol):
                    continue
                findings.append(AnchorDriftFinding(
                    kind="summary-symbol-drift",
                    node_id=node_id,
                    path=str(attrs.get("path") or ""),
                    line=attrs.get("start_line"),
                    anchor=str(summary),
                    symbol=symbol,
                    message=(f"The summary names `{symbol}`, but that identifier no longer "
                             "appears in this node's current source."),
                    evidence=[f"summary: {summary}", f"source node: {node_id}"],
                ))

    for left, right, data in sorted(graph.edges(data=True)):
        if data.get("type") != "CONNECTS":
            continue
        if scoped_features is not None and left not in scoped_features and right not in scoped_features:
            continue
        if not graph.has_node(left) or not graph.has_node(right):
            continue
        if _feature_evidence(graph, left, right):
            continue
        left_name = str(graph.nodes[left].get("name") or left.removeprefix("feature:"))
        right_name = str(graph.nodes[right].get("name") or right.removeprefix("feature:"))
        findings.append(AnchorDriftFinding(
            kind="connect-without-evidence",
            node_id=left,
            feature=left_name,
            related_feature=right_name,
            anchor=f"@memory:connects:{right_name}",
            message=(f"{left_name} declares a connection to {right_name}, but Atlas can find "
                     "no RELATES, CALLS or IMPORTS evidence between their members."),
            evidence=[f"declared edge: {left} -> {right}", "no member-level structural edge found"],
        ))

    findings.sort(key=lambda item: (item.path, item.line or 0, item.node_id, item.kind, item.symbol, item.related_feature))
    return AnchorDriftReport(
        target=canonical_target,
        anchored_nodes=anchored_nodes,
        findings=findings,
    )
