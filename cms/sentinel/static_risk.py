"""Sentinel Static Risk Scanner — risky patterns, classified by context.

Two layers:
  1. Pattern sweep over executable project source (TODO/FIXME/HACK, fake/force/
     bypass verbs, placeholder/dummy/hardcoded markers). Documentation, test
     fixtures, and Sentinel's detector definitions are evidence/reference text,
     not active product risks, so they are excluded instead of reported as noise.
  2. AST pass over Python files for trivial validators — a function whose name
     says it checks/validates/verifies something but whose body is a bare
     ``return True``/``return False`` is a fake implementation (critical).

Every occurrence is NOT critical by default; classification is the point.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from . import make_finding
from ..scanner import FileRecord, scan

# (regex, base severity in production code, short risk note)
PATTERNS: list[tuple[str, str, str]] = [
    (r"\bTODO\b", "low", "acknowledged unfinished work"),
    (r"\bFIXME\b", "medium", "known defect left in place"),
    (r"\bHACK\b", "medium", "shortcut that may violate assumptions"),
    (r"\bXXX\b", "low", "flagged suspicious spot"),
    (r"fake[_ ]?(success|pass|result|data)", "critical", "fabricated success/result path"),
    (r"(force|mock)[_ ]?pass", "critical", "pass/fail logic can be forced"),
    (r"(skip|bypass)[_ ]?(validation|check|audit|verify|calibration)", "critical",
     "guard logic can be bypassed"),
    (r"\bnot implemented\b", "medium", "declared-but-missing behaviour"),
    # exclude UI plumbing: placeholder= attributes, ::placeholder / .placeholder
    # CSS selectors, and "placeholder" class-name strings
    (r"(?<![:.\"'])\bplaceholder\b(?!\s*=|\")", "medium", "stand-in logic or data"),
    (r"\bdummy\b", "low", "stand-in value"),
    (r"\bhard[- ]?coded\b", "low", "value that should come from config/data"),
    (r"(sample|demo)[_ ]?data", "low", "canned data that must not leak into real output"),
]

_TRIVIAL_VALIDATOR = re.compile(r"(valid|verif|check|ensure|guard|enforce)", re.I)
# variable names that signal the compared value is a filesystem path
_PATHY = re.compile(r"(path|rel|target|file|dir|loc)", re.I)

_DOWNGRADE = {"critical": "medium", "high": "medium", "medium": "low", "low": "info", "info": "info"}


def _classify(rel_path: str, base: str) -> str:
    if rel_path.startswith("cms/sentinel/") or rel_path == "cms/ui_assets/sentinel.html":
        return "info"  # Sentinel's own modules name risky patterns to describe them;
        # the AST fake-validator check below still applies to this package
    if rel_path.startswith(("tests/", "docs/")) or rel_path.endswith((".md", ".txt")):
        return "info"
    if not rel_path.startswith("cms/"):
        return _DOWNGRADE[base]  # scripts/spec files: real but not production logic
    return base


def _is_reference_context(rel_path: str) -> bool:
    """Text that describes or exercises a risk is not itself an active risk."""
    return (
        rel_path.startswith(("tests/", "docs/", "cms/sentinel/"))
        or rel_path == "cms/ui_assets/sentinel.html"
        or rel_path.endswith((".md", ".txt", ".rst"))
    )


def _weak_path_guards(rel_path: str, text: str) -> list[dict]:
    """A path-traversal guard implemented as a `'..' in path` (or `'..' not in
    path`) substring test is bypassable: absolute paths, drive-letter paths,
    symlinks, and percent-encoded traversal contain no literal '..'. The sound
    guard resolves the path and checks parent-containment. Flags the substring
    idiom so the bypass is caught statically, on every file, without executing.
    Security-relevant even in tests/docs, so it is not context-downgraded."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    findings = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Compare) and node.ops
                and isinstance(node.ops[0], (ast.In, ast.NotIn))):
            continue
        left = node.left
        if not (isinstance(left, ast.Constant) and left.value == ".."):
            continue
        # only when the tested value looks like a filesystem path — a bare
        # `'..' in some_string` on non-path data is not a traversal guard
        names = [
            getattr(c, "id", None) or getattr(c, "attr", None)
            for c in node.comparators
        ]
        if any(n and _PATHY.search(n) for n in names):
            findings.append(make_finding(
                "static_risk", "high",
                f"weak path-traversal guard in {rel_path}:{node.lineno} — "
                "'..' substring check is bypassable by absolute/encoded paths",
                area="weak_path_guard",
                file=rel_path,
                line=node.lineno,
                pattern="substring-traversal-guard",
                evidence=["'..' membership test used as a containment guard"],
                risk="Absolute paths, drive letters, symlinks, or %2e%2e bypass a '..' "
                     "substring check, allowing reads/writes outside the intended root.",
                recommendation="Resolve then check containment: `root in target.resolve().parents` "
                               "(or Path.is_relative_to(root)) — never a substring test.",
                fingerprint_of=f"weak-guard-{rel_path}",
            ))
    return findings


def _trivial_validators(rel_path: str, text: str) -> list[dict]:
    """Functions named like guards whose whole body is `return True/False`."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _TRIVIAL_VALIDATOR.search(node.name):
            continue
        body = [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        if len(body) == 1 and isinstance(body[0], ast.Return) and \
                isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, bool):
            verdict = body[0].value.value
            findings.append(make_finding(
                "static_risk",
                _classify(rel_path, "critical"),
                f"{node.name}() always returns {verdict} — validation that never validates",
                area="fake_validation",
                file=rel_path,
                line=node.lineno,
                pattern="trivial-validator",
                evidence=[f"def {node.name}(...): return {verdict}"],
                risk="Anything gated on this check passes (or fails) unconditionally.",
                recommendation="Implement the real check or delete the function so callers cannot rely on it.",
            ))
    return findings


# @memory:feature:HermesSentinel
# @memory:summary:Static risk scanner — context-classified pattern sweep (TODO/FIXME/fake/bypass/placeholder) plus AST detection of trivial always-True validators.
def scan_static_risks(root: Path, records: list[FileRecord] | None = None,
                      max_per_pattern: int = 25) -> list[dict]:
    records = records if records is not None else scan(root)
    findings: list[dict] = []
    per_pattern: dict[str, int] = {}
    for record in records:
        try:
            text = Path(record.abs_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _is_reference_context(record.rel_path):
            for lineno, line in enumerate(text.splitlines(), 1):
                for pattern, base, risk in PATTERNS:
                    if per_pattern.get(pattern, 0) >= max_per_pattern:
                        continue
                    match = re.search(pattern, line, re.I)
                    if match:
                        severity = _classify(record.rel_path, base)
                        per_pattern[pattern] = per_pattern.get(pattern, 0) + 1
                        findings.append(make_finding(
                            "static_risk", severity,
                            f"'{match.group(0)}' in {record.rel_path}:{lineno}",
                            area="risky_pattern",
                            file=record.rel_path,
                            line=lineno,
                            pattern=pattern,
                            evidence=[line.strip()[:200]],
                            risk=risk,
                            recommendation="Resolve the marked gap, or move the marker into an issue/ledger entry with a plan.",
                            fingerprint_of=line.strip()[:200],
                        ))
        if record.language == "python" and not record.rel_path.startswith(("tests/", "docs/")):
            findings += _trivial_validators(record.rel_path, text)
            findings += _weak_path_guards(record.rel_path, text)
    return findings
