"""Intent capture — the missing agent→memory input channel.

Everything else in CMS derives intent statically (anchors, docs, the feature
ledger). This records the *live* intent of the current unit of work: an
explicit goal, or, failing that, the git branch name or the last commit
subject. The goal is enriched into the same task pack ``cms prompt`` produces
(relevant code, features, blast radius, verification) so the alignment check
has an "expected" side to judge the diff against, and persisted as the active
session intent under ``.memory/align/intent.json``.
"""

from __future__ import annotations

from pathlib import Path

from . import config
from .align import AlignStore
from .githistory import _git
from .memory import CodebaseMemory
from .prompt_export import build_task_pack


def _infer_goal(root: Path) -> tuple[str, str]:
    """(goal, source) from the current git branch, else the last commit subject."""
    branch = (_git(root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    if branch and branch not in ("HEAD", ""):
        readable = branch.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").strip()
        if readable:
            return readable, "branch"
    subject = (_git(root, "log", "-1", "--format=%s") or "").strip()
    if subject:
        return subject, "commit"
    return "", "none"


def capture_intent(root: Path, goal: str | None = None, base: str = "HEAD", top_k: int = 8,
                   assets: list[str] | None = None) -> dict:
    """Resolve, enrich and persist the active intent; return the task pack.

    ``assets`` names the Library refs this change runs under; their exact
    versions are recorded in the intent, so the alignment history answers
    "which reusable context was this agent working from?".
    """
    root = root.resolve()
    source = "explicit"
    if not goal:
        goal, source = _infer_goal(root)
    if not goal:
        goal = "(unstated)"
        source = "none"

    memory_dir = root / config.MEMORY_DIR_NAME
    mem = CodebaseMemory.load(memory_dir / "graph.json")
    pack = build_task_pack(mem, root, goal, top_k=top_k, assets=assets)
    pack["intent_source"] = source
    pack["base"] = base

    AlignStore(memory_dir).save_intent(pack)
    return pack
