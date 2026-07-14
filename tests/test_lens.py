"""Comprehension lens: level catalogue, deterministic fallbacks, cache-first
rewrites, and graceful degradation without a real provider."""

import json
import re

import pytest

from cms.lens import (
    CHUNK_SIZE,
    LEVELS,
    LensError,
    _parse_batch_reply,
    fallback_rewrite,
    lens_key,
    load_cache,
    rewrite_batch,
)
from cms.providers import MockProvider

LONG = ("This module builds the knowledge graph from scanned source records. "
        "It resolves imports across files and attaches summaries to nodes.")
OTHER = ("The scanner walks the project tree while honouring three ignore "
         "layers and returns one record per recognisable source file.")


class FakeProvider:
    """Real-provider stand-in: answers each chunk with a JSON array."""

    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def summarize(self, prompt: str, context: dict) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        count = int(re.search(r"JSON array of (\d+) strings", prompt).group(1))
        return json.dumps([f"REWRITTEN {i}" for i in range(count)])


def test_level_catalogue_matches_the_slider() -> None:
    assert list(LEVELS) == ["schoolchild", "tech", "uni", "specialist", "tldr", "adhd"]
    for spec in LEVELS.values():
        assert spec["label"] and spec["audience"] and spec["rules"]


def test_fallback_tldr_is_first_sentence() -> None:
    out = fallback_rewrite(LONG, "tldr")
    assert out == ("This module builds the knowledge graph from scanned "
                   "source records.")


def test_fallback_adhd_is_short_bullets() -> None:
    out = fallback_rewrite("First point here. Second point here. Third. Fourth. Fifth.", "adhd")
    lines = out.splitlines()
    assert 2 <= len(lines) <= 4
    assert all(line.startswith("- ") and len(line) <= 62 for line in lines)


def test_fallback_persona_levels_have_none() -> None:
    for level in ("schoolchild", "tech", "uni", "specialist"):
        assert fallback_rewrite(LONG, level) is None


def test_parse_batch_reply_tolerates_prose_and_rejects_mismatch() -> None:
    assert _parse_batch_reply('Sure! ["a", "b"] hope that helps', 2) == ["a", "b"]
    assert _parse_batch_reply('["only one"]', 2) is None
    assert _parse_batch_reply("no array at all", 1) is None


def test_rewrite_batch_generates_then_serves_from_cache(tmp_path) -> None:
    provider = FakeProvider()
    out = rewrite_batch(tmp_path, "schoolchild",
                        [{"id": "a", "text": LONG}, {"id": "b", "text": OTHER}], provider)
    assert out["real"] is True and out["generated"] == 2 and out["cached"] == 0
    assert out["results"]["a"] == "REWRITTEN 0"
    assert provider.calls == 1  # one chunk covers both items

    on_disk = load_cache(tmp_path, "schoolchild")
    assert on_disk[lens_key(LONG)] == "REWRITTEN 0"

    # second request: cache-first — a dead provider must never be reached
    out2 = rewrite_batch(tmp_path, "schoolchild",
                         [{"id": "a", "text": LONG}], FakeProvider(fail=True))
    assert out2["results"]["a"] == "REWRITTEN 0"
    assert out2["cached"] == 1 and out2["generated"] == 0


def test_rewrite_batch_chunks_large_requests(tmp_path) -> None:
    provider = FakeProvider()
    items = [{"id": str(i), "text": f"{LONG} variant {i}"} for i in range(CHUNK_SIZE + 2)]
    out = rewrite_batch(tmp_path, "tech", items, provider)
    assert provider.calls == 2
    assert out["generated"] == CHUNK_SIZE + 2


def test_rewrite_batch_provider_failure_keeps_original_uncached(tmp_path) -> None:
    out = rewrite_batch(tmp_path, "uni", [{"id": "a", "text": LONG}], FakeProvider(fail=True))
    assert out["results"]["a"] == LONG and out["generated"] == 0
    assert lens_key(LONG) not in load_cache(tmp_path, "uni")  # retryable later


def test_rewrite_batch_mock_provider_degrades_honestly(tmp_path) -> None:
    tldr = rewrite_batch(tmp_path, "tldr", [{"id": "a", "text": LONG}], MockProvider())
    assert tldr["real"] is False
    assert tldr["results"]["a"].endswith("source records.")  # deterministic shortening

    persona = rewrite_batch(tmp_path, "schoolchild", [{"id": "a", "text": LONG}], MockProvider())
    assert persona["results"]["a"] == LONG  # unchanged — UI explains why


def test_rewrite_batch_validates_requests(tmp_path) -> None:
    with pytest.raises(LensError):
        rewrite_batch(tmp_path, "wizard", [{"id": "a", "text": LONG}], MockProvider())
    with pytest.raises(LensError):
        rewrite_batch(tmp_path, "tldr", [{"id": "a", "text": ""}], MockProvider())
    with pytest.raises(LensError):
        rewrite_batch(tmp_path, "tldr",
                      [{"id": str(i), "text": LONG} for i in range(17)], MockProvider())
