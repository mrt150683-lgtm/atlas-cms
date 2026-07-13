"""Codebase chat: evidence assembly, intent-vs-reality prompting, transcript,
honesty guards, and the MCP surface."""

import json
from pathlib import Path

import pytest

from cms.chat import (ChatError, ask, build_evidence, load_transcript,
                      validate_answer_commands)
from cms.providers import MockProvider
from cms.update import incremental_update

SOURCE = '''\
# @memory:feature:Constellation
# @memory:summary:Cross-project fusion engine.
def fuse_projects():
    return helper()


def helper():
    return 1
'''


class ChatProvider:
    name = "chat-test"
    model = "m1"

    def __init__(self, reply="Constellation matches its declared intent. (cms/x.py:1-4)"):
        self.reply = reply
        self.prompts: list[str] = []

    def summarize(self, prompt, context):
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _project(tmp_path: Path) -> Path:
    (tmp_path / "fusion.py").write_text(SOURCE, encoding="utf-8")

    class P(ChatProvider):
        def summarize(self, prompt, context):
            if "FEATURE TRACE" in prompt:
                return "## Purpose\nx\n## Flow\nx\n## Verification Checklist\n- x"
            if "named FEATURES" in prompt:
                return "[]"
            return "Summary: fusion module."

    incremental_update(tmp_path, P(), echo=lambda *a: None)
    return tmp_path


def test_evidence_pack_matches_named_feature(tmp_path):
    root = _project(tmp_path)
    evidence, nodes = build_evidence(root, "Is the Constellation feature aligned with its core idea?")
    assert evidence["project"] == root.name
    matched = evidence["matched_features"]
    assert len(matched) == 1 and matched[0]["feature"] == "Constellation"
    assert matched[0]["declared_intent"].startswith("Cross-project fusion")
    assert "feature:Constellation" in nodes
    assert evidence["pipeline"]["status"] in ("in_progress", "finished", "attention")
    assert evidence["ranked_hits"]  # query hits present


def test_ask_grounds_prompt_and_appends_transcript(tmp_path):
    root = _project(tmp_path)
    p = ChatProvider()
    entry = ask(root, "Is Constellation doing what it is supposed to do?", p)
    prompt = p.prompts[-1]
    # intent-vs-reality contract present in the instruction
    assert "SUPPOSED to do" in prompt and "ASK the owner" in prompt
    assert "LIVE CLI CONTRACT" in prompt and "cms review" in prompt
    assert '"Constellation"' in prompt        # matched feature in evidence
    assert entry["matched_features"] == ["Constellation"]
    assert entry["a"].startswith("Constellation matches")

    # transcript persisted and fed back as history
    saved = load_transcript(root)
    assert len(saved) == 1 and saved[0]["q"].startswith("Is Constellation")
    p2 = ChatProvider("Follow-up answer.")
    ask(root, "and the flows?", p2, history=saved)
    assert "RECENT CONVERSATION" in p2.prompts[-1]
    assert "Is Constellation doing" in p2.prompts[-1]
    assert len(load_transcript(root)) == 2


def test_generated_cli_commands_are_checked_against_live_surface(tmp_path):
    valid, findings = validate_answer_commands("Run `cms review Constellation` next.")
    assert valid == "Run `cms review Constellation` next."
    assert findings == []
    assert validate_answer_commands("Run `cms sentinel run --help`.")[1] == []
    assert validate_answer_commands("Run `cms --help`.")[1] == []

    invalid, findings = validate_answer_commands(
        "Run `cms review --feature Constellation` next."
    )
    assert "blocked an invalid generated command" in invalid
    assert "cms review --help" in invalid
    assert findings[0]["command"] == "cms review --feature Constellation"

    root = _project(tmp_path)
    entry = ask(root, "how do I review it?", ChatProvider(
        "Run `cms review --feature Constellation` next."
    ))
    assert entry["command_validation"]["checked"] is True
    assert len(entry["command_validation"]["blocked"]) == 1
    assert "--feature" not in entry["a"].split("Use `", 1)[-1]


def test_honesty_guards(tmp_path):
    root = _project(tmp_path)
    with pytest.raises(ChatError, match="real provider"):
        ask(root, "anything", MockProvider())
    with pytest.raises(ChatError, match="ask something"):
        ask(root, "   ", ChatProvider())
    with pytest.raises(ChatError, match="provider call failed"):
        ask(root, "q", ChatProvider(RuntimeError("down")))
    with pytest.raises(ChatError, match="empty answer"):
        ask(root, "q", ChatProvider("   "))
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(ChatError, match="no memory layer"):
        ask(bare, "q", ChatProvider())
    # failures never wrote to the transcript
    assert load_transcript(root) == []


def test_ask_codebase_over_mcp(tmp_path, monkeypatch):
    from cms.mcp import MCPServer

    root = _project(tmp_path)
    monkeypatch.setattr("cms.providers.get_provider", lambda *_: ChatProvider())
    server = MCPServer(root)
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "ask_codebase",
                                     "arguments": {"question": "is Constellation aligned?"}}})
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["answer"].startswith("Constellation matches")
    assert payload["matched_features"] == ["Constellation"]
    assert "feature:Constellation" in payload["evidence_nodes"]

    monkeypatch.setattr("cms.providers.get_provider", lambda *_: MockProvider())
    resp = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "ask_codebase", "arguments": {"question": "x"}}})
    assert "real provider" in json.loads(resp["result"]["content"][0]["text"])["error"]


def test_sessions_isolate_history_and_list_logically(tmp_path):
    root = _project(tmp_path)
    from cms.chat import list_sessions, session_history

    ask(root, "first question about fusion", ChatProvider("A1"), session="s-one")
    ask(root, "follow up", ChatProvider("A2"), session="s-one")
    ask(root, "unrelated new topic", ChatProvider("B1"), session="s-two")

    # continuity is per-session: s-two must NOT see s-one's turns
    assert [t["q"] for t in session_history(root, "s-one")] == \
        ["first question about fusion", "follow up"]
    assert [t["q"] for t in session_history(root, "s-two")] == ["unrelated new topic"]

    sessions = list_sessions(root)
    assert [s["id"] for s in sessions] == ["s-two", "s-one"]  # newest first
    named = {s["id"]: s for s in sessions}
    assert named["s-one"]["name"] == "first question about fusion"  # logical name
    assert named["s-one"]["turns"] == 2
