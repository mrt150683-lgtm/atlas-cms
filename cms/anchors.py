"""Embedded Memory Anchors — developer-curated intent markers in source comments.

Syntax (line form, attaches to the next def/class):

    # @memory:feature:UserAuthentication
    # @memory:connects:LoginFlow, TokenService
    # @memory:summary:Handles JWT issuance and refresh.
    def login_user(...):

Block form (``===`` fences; plain comment lines that follow become notes,
``module`` tags attach to the file itself):

    # === @memory:module:GraphLayer ===
    # Purpose: Maintains the runtime knowledge graph
    # Key flows: scan -> parse -> summarize -> persist
    class MemoryEngine:

Anchors enrich graph nodes beyond what AST analysis can infer and boost
query ranking for the tagged components.
"""

from __future__ import annotations

import io
import re
import tokenize
from dataclasses import dataclass, field

_ANCHOR_RE = re.compile(r"^\s*#\s*(={2,}\s*)?@memory:([A-Za-z_][\w-]*):\s*(.*?)\s*(?:={2,})?\s*$")
_COMMENT_RE = re.compile(r"^\s*#\s?(.*)$")

# tags whose values are comma-separated lists
_LIST_TAGS = {"connects"}
# tags that always describe the whole file, never a component
FILE_LEVEL_TAGS = {"module"}

# an anchor group binds to a component starting within this many lines below it
MAX_ATTACH_GAP = 6


@dataclass
class AnchorGroup:
    start_line: int
    end_line: int
    tags: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def is_file_level(self) -> bool:
        return any(tag in FILE_LEVEL_TAGS for tag in self.tags)

    def to_dict(self) -> dict:
        data: dict = dict(self.tags)
        if self.notes:
            data["notes"] = list(self.notes)
        return data


def _add_tag(group: AnchorGroup, key: str, raw: str) -> None:
    values = (
        [v.strip() for v in raw.split(",") if v.strip()] if key in _LIST_TAGS else [raw.strip()]
    )
    group.tags.setdefault(key, []).extend(v for v in values if v)


def _comment_rows(source: str) -> dict[int, str]:
    """Map line number -> comment text, using real COMMENT tokens only so
    anchor-like text inside strings/docstrings is never picked up."""
    rows: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                rows[tok.start[0]] = tok.string
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass  # keep whatever was tokenized before the error
    return rows


# @memory:feature:MemoryAnchors
# @memory:connects:KnowledgeGraphConstruction, SummaryGenerator, QueryEngine
# @memory:summary:Extracts @memory: developer intent tags from comments and merges them into graph nodes.
def parse_anchors(source: str) -> list[AnchorGroup]:
    """Extract anchor groups from source text. Line numbers are 1-based."""
    rows = _comment_rows(source)
    groups: list[AnchorGroup] = []
    current: AnchorGroup | None = None
    collecting_notes = False  # inside a === block: plain comments become notes

    for lineno in sorted(rows):
        line = rows[lineno]
        contiguous = current is not None and lineno == current.end_line + 1
        match = _ANCHOR_RE.match(line)
        if match:
            fenced, key, value = match.group(1), match.group(2).lower(), match.group(3)
            if not contiguous:
                current = AnchorGroup(start_line=lineno, end_line=lineno)
                groups.append(current)
                collecting_notes = False
            current.end_line = lineno
            _add_tag(current, key, value)
            if fenced:
                collecting_notes = True
        elif contiguous and collecting_notes:
            text = _COMMENT_RE.match(line).group(1).strip()
            if text:
                current.notes.append(text)
            current.end_line = lineno
        else:
            current = None
            collecting_notes = False

    return groups


def merge_anchor_dicts(target: dict, extra: dict) -> dict:
    """Merge two anchor dicts, concatenating value lists."""
    for key, values in extra.items():
        target.setdefault(key, []).extend(v for v in values if v not in target.get(key, []))
    return target


def anchors_as_text(anchors: dict) -> str:
    """Flatten an anchors dict to one searchable/displayable string."""
    parts: list[str] = []
    for key, values in anchors.items():
        parts.append(f"{key}: {', '.join(values)}")
    return " | ".join(parts)
