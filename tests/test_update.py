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
