"""Phase 4: CodebaseMemory — the query API agents import.

Loads graph.json and answers intent queries with ranked nodes (path, lines,
summary, call connections), plus structural helpers for graph traversal.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from .anchors import anchors_as_text
from .graph_builder import graph_from_json, graph_to_json

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "be", "where", "what", "which", "who",
    "of", "for", "in", "on", "to", "and", "or", "that", "this", "it", "its",
    "find", "show", "me", "all", "any", "code", "logic", "how", "does", "do",
    "responsible", "place", "places", "handle", "handles", "with", "by", "at",
}


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t and t not in _STOPWORDS]


def _name_tokens(name: str) -> set[str]:
    """Split snake_case and camelCase identifiers into searchable tokens."""
    parts = re.split(r"[_\W]+", name)
    tokens: set[str] = {name.lower()}
    for part in parts:
        for sub in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", part):
            tokens.add(sub.lower())
    return tokens


@dataclass
class QueryResult:
    node_id: str
    kind: str
    name: str
    path: str
    start_line: int | None
    end_line: int | None
    score: float
    summary: str = ""
    anchors: dict = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    called_by: list[str] = field(default_factory=list)

    @property
    def lines(self) -> str:
        if self.start_line is None:
            return ""
        return f"{self.start_line}-{self.end_line}"


class CodebaseMemory:
    def __init__(self, graph: nx.DiGraph) -> None:
        self.graph = graph

    # -- persistence ----------------------------------------------------

    @classmethod
    def load(cls, graph_path: Path | str) -> "CodebaseMemory":
        data = json.loads(Path(graph_path).read_text(encoding="utf-8"))
        return cls(graph_from_json(data))

    def save(self, graph_path: Path | str) -> None:
        Path(graph_path).write_text(
            json.dumps(graph_to_json(self.graph), indent=2), encoding="utf-8"
        )

    # -- intent query -----------------------------------------------------

    # @memory:feature:QueryEngine
    # @memory:connects:KnowledgeGraphConstruction, SummaryGenerator, MemoryAnchors
    # @memory:summary:Weighted keyword ranking over names, anchors, summaries, docstrings and paths, with a graph-degree boost.
    def query_intent(
        self,
        text: str,
        top_k: int = 5,
        include_summaries: bool = True,
        include_connections: bool = True,
    ) -> list[QueryResult]:
        tokens = _tokenize(text)
        if not tokens:
            return []
        scored: list[tuple[float, str]] = []
        for node_id, attrs in self.graph.nodes(data=True):
            if attrs.get("type") == "external":
                continue
            name_toks = _name_tokens(attrs.get("name", ""))
            summary = (attrs.get("summary") or "").lower()
            docstring = (attrs.get("docstring") or "").lower()
            path = (attrs.get("path") or "").lower()
            anchor_text = anchors_as_text(attrs.get("anchors") or {}).lower()
            score = 0.0
            for tok in tokens:
                if tok in name_toks:
                    score += 3.0
                elif any(tok in nt for nt in name_toks):
                    score += 1.5
                if tok in anchor_text:
                    score += 2.5  # developer-curated intent outranks generated text
                if tok in summary:
                    score += 2.0
                if tok in docstring:
                    score += 1.5
                if tok in path:
                    score += 1.0
            if score > 0:
                score += 0.2 * math.log1p(self.graph.degree(node_id))
                scored.append((score, node_id))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [
            self._result(node_id, score, include_summaries, include_connections)
            for score, node_id in scored[:top_k]
        ]

    def _result(
        self, node_id: str, score: float, include_summaries: bool, include_connections: bool
    ) -> QueryResult:
        attrs = self.graph.nodes[node_id]
        calls, called_by = [], []
        if include_connections:
            calls = [
                t for _, t, d in self.graph.out_edges(node_id, data=True)
                if d.get("type") == "CALLS"
            ]
            called_by = [
                s for s, _, d in self.graph.in_edges(node_id, data=True)
                if d.get("type") == "CALLS"
            ]
        return QueryResult(
            node_id=node_id,
            kind=attrs.get("type", "?"),
            name=attrs.get("name", ""),
            path=attrs.get("path", ""),
            start_line=attrs.get("start_line"),
            end_line=attrs.get("end_line"),
            score=round(score, 2),
            summary=(attrs.get("summary") or "") if include_summaries else "",
            anchors=attrs.get("anchors") or {},
            calls=calls,
            called_by=called_by,
        )

    # -- structural helpers ------------------------------------------------

    def neighbors(self, node_id: str) -> dict[str, list[str]]:
        """Outgoing and incoming edges grouped by edge type."""
        out: dict[str, list[str]] = {}
        for _, t, d in self.graph.out_edges(node_id, data=True):
            out.setdefault(d.get("type", "?"), []).append(t)
        for s, _, d in self.graph.in_edges(node_id, data=True):
            out.setdefault(d.get("type", "?") + "_by", []).append(s)
        return out

    def who_imports(self, rel_path: str) -> list[str]:
        target = f"file:{rel_path}"
        if not self.graph.has_node(target):
            return []
        return [
            s for s, _, d in self.graph.in_edges(target, data=True)
            if d.get("type") == "IMPORTS"
        ]

    def who_calls(self, func_name: str) -> list[str]:
        """Callers of any function node matching `func_name` (bare name or node id)."""
        callers: list[str] = []
        for node_id, attrs in self.graph.nodes(data=True):
            if attrs.get("type") != "func":
                continue
            if node_id == func_name or attrs.get("name") == func_name:
                callers += [
                    s for s, _, d in self.graph.in_edges(node_id, data=True)
                    if d.get("type") == "CALLS"
                ]
        return callers
