import os
import time
from pathlib import Path

from cms.providers import MockProvider
from cms.update import incremental_update


class CountingProvider(MockProvider):
    """Mock provider that counts summarize calls, but claims not to be 'mock'
    so feature narratives also route through it."""

    name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def summarize(self, prompt: str, context: dict) -> str:
        self.calls += 1
        if "FEATURE TRACE" in prompt:
            return "## Purpose\nx\n## Flow\nx\n## Verification Checklist\n- x"
        return super().summarize(prompt, context)


def _project(tmp_path: Path) -> Path:
    (tmp_path / "one.py").write_text("def f1():\n    pass\n", encoding="utf-8")
    (tmp_path / "two.py").write_text(
        "# @memory:feature:Two\ndef f2():\n    pass\n", encoding="utf-8"
    )
    return tmp_path


def test_second_update_skips_unchanged(tmp_path: Path) -> None:
    root = _project(tmp_path)
    p1 = CountingProvider()
    stats1 = incremental_update(root, p1, echo=lambda *a: None)
    assert stats1.summarized == 2
    first_calls = p1.calls
    assert first_calls >= 3  # 2 file summaries + 1 feature narrative

    p2 = CountingProvider()
    stats2 = incremental_update(root, p2, echo=lambda *a: None)
    assert stats2.summarized == 0          # nothing changed
    assert stats2.changed == []
    assert p2.calls == 0                   # no LLM cost at all


def test_mock_summaries_upgrade_when_real_provider_arrives(tmp_path: Path) -> None:
    root = _project(tmp_path)
    incremental_update(root, MockProvider(), echo=lambda *a: None)  # first pass: mock

    p = CountingProvider()  # a "real" provider appears
    stats = incremental_update(root, p, echo=lambda *a: None)
    assert stats.summarized == 2            # mock summaries treated as stale
    assert p.calls >= 3                     # 2 file summaries + feature narrative redone

    from cms.memory import CodebaseMemory
    mem = CodebaseMemory.load(root / ".memory" / "graph.json")
    assert mem.graph.nodes["file:one.py"]["summary_meta"]["provider"] == "counting"
    assert mem.graph.nodes["feature:Two"]["narrative_provider"] == "counting"

    # and a further real-provider update is again a no-op
    p3 = CountingProvider()
    stats3 = incremental_update(root, p3, echo=lambda *a: None)
    assert stats3.summarized == 0 and p3.calls == 0


def test_changed_file_is_reprocessed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    incremental_update(root, CountingProvider(), echo=lambda *a: None)

    target = root / "two.py"
    time.sleep(0.02)
    target.write_text("# @memory:feature:Two\ndef f2():\n    return 1\n", encoding="utf-8")
    os.utime(target, (time.time() + 5, time.time() + 5))  # force distinct mtime

    p = CountingProvider()
    stats = incremental_update(root, p, echo=lambda *a: None)
    assert stats.changed == ["two.py"]
    assert stats.summarized == 1
    # feature Two's member changed -> narrative regenerated (1 summary + 1 narrative)
    assert p.calls == 2

    # unchanged file kept its summary carried over from the old graph
    from cms.memory import CodebaseMemory
    mem = CodebaseMemory.load(root / ".memory" / "graph.json")
    assert mem.graph.nodes["file:one.py"]["summary"]


class DiscoveringProvider(CountingProvider):
    """Non-mock provider that answers the feature-discovery prompt with one
    feature, so LLM discovery is testable offline."""

    name = "discovering"

    def summarize(self, prompt: str, context: dict) -> str:
        if "named FEATURES" in prompt:
            self.calls += 1
            return '[{"name": "Numbers", "description": "number utils", "files": ["one.py"]}]'
        if "top-level review" in prompt:
            self.calls += 1
            return '{"verdict": "aligned", "headline": "matches", "summary": "complete review"}'
        if "reviewing one feature" in prompt:
            self.calls += 1
            return ('{"verdict": "aligned", "headline": "matches", "expected": "numbers", '
                    '"built": "numbers", "gaps": [], "education": "number utilities"}')
        if "return on investment" in prompt.lower() or "suggest" in prompt.lower():
            self.calls += 1
            return "[]"  # fall back to structural suggestions
        return super().summarize(prompt, context)


def test_mock_build_then_real_provider_triggers_discovery(tmp_path: Path) -> None:
    """A project first built with mock has no discovered features (mock skips
    LLM discovery). The first real-provider update must re-discover, not just
    upgrade summaries — otherwise anchor-less projects stay feature-less."""
    root = tmp_path
    (root / "one.py").write_text("def f1():\n    pass\n", encoding="utf-8")  # no anchors

    incremental_update(root, MockProvider(), echo=lambda *a: None)
    from cms.memory import CodebaseMemory
    mem = CodebaseMemory.load(root / ".memory" / "graph.json")
    assert not [n for n, a in mem.graph.nodes(data=True)
                if a.get("type") == "feature"], "mock build must not invent features"

    incremental_update(root, DiscoveringProvider(), echo=lambda *a: None)
    mem = CodebaseMemory.load(root / ".memory" / "graph.json")
    discovered = [a for n, a in mem.graph.nodes(data=True)
                  if a.get("type") == "feature" and a.get("source") == "discovered"]
    assert [f["name"] for f in discovered] == ["Numbers"]


def test_ensure_judgment_builds_once_and_skips_mock(tmp_path: Path) -> None:
    from cms.memory import CodebaseMemory
    from cms.update import ensure_judgment

    root = _project(tmp_path)
    incremental_update(root, DiscoveringProvider(), echo=lambda *a: None)

    # mock never builds the judgment layer (its output must not pose as review)
    assert ensure_judgment(root, MockProvider(), echo=lambda *a: None) == \
        {"review": False, "suggestions": False}

    ran = ensure_judgment(root, DiscoveringProvider(), echo=lambda *a: None)
    assert ran == {"review": True, "suggestions": True}
    mem = CodebaseMemory.load(root / ".memory" / "graph.json")
    assert mem.graph.has_node("review:app") and mem.graph.has_node("suggestions:app")
    assert (root / ".memory" / "review.md").is_file()
    assert (root / ".memory" / "suggestions.md").is_file()

    # second call: already present -> nothing rebuilt
    assert ensure_judgment(root, DiscoveringProvider(), echo=lambda *a: None) == \
        {"review": False, "suggestions": False}


def test_ensure_judgment_concurrent_callers_build_once(tmp_path: Path) -> None:
    """App startup sync and the UI build worker can race ensure_judgment while
    artifacts are absent. The lock + re-check must make exactly one caller pay
    for the build; the loser sees the winner's nodes and no-ops."""
    import threading

    from cms.update import ensure_judgment

    root = _project(tmp_path)
    incremental_update(root, DiscoveringProvider(), echo=lambda *a: None)

    class SlowProvider(DiscoveringProvider):
        name = "slow"

        def summarize(self, prompt: str, context: dict) -> str:
            time.sleep(0.05)  # widen the race window
            return super().summarize(prompt, context)

    provider = SlowProvider()
    results: list[dict] = []
    threads = [
        threading.Thread(target=lambda: results.append(
            ensure_judgment(root, provider, echo=lambda *a: None)))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == 2
    built = [r for r in results if r["review"] or r["suggestions"]]
    noop = [r for r in results if not r["review"] and not r["suggestions"]]
    assert len(built) == 1 and len(noop) == 1, results
    assert built[0] == {"review": True, "suggestions": True}

    from cms.memory import CodebaseMemory
    mem = CodebaseMemory.load(root / ".memory" / "graph.json")
    assert mem.graph.has_node("review:app") and mem.graph.has_node("suggestions:app")
