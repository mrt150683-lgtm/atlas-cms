from cms.library_usage import LibraryUsageStore


def _asset(asset_id="focus-mode", version=2):
    return {"id": asset_id, "version": version, "content_hash": "abc123",
            "scope": "project", "trust": "user", "type": "mode"}


def test_usage_ledger_keeps_agent_and_human_judgement_separate(tmp_path):
    store = LibraryUsageStore(tmp_path / ".memory")
    event = store.record([_asset()], task="Fix a difficult bug", outcome="success",
                         effectiveness=4, efficiency=3, duration_ms=1200,
                         input_tokens=100, output_tokens=50, model="reasoner-v2",
                         notes="Found the cross-file cause")
    store.rate(event["id"], rating=5, effectiveness=5, efficiency=4,
               comment="It kept the investigation on track")

    summary = store.summary("focus-mode")
    assert summary["uses"] == 1
    assert summary["agent"] == {"effectiveness": 4.0, "efficiency": 3.0}
    assert summary["human"]["rating"] == 5.0
    assert summary["human"]["effectiveness"] == 5.0
    assert summary["recent"][0]["assets"][0]["hash"] == "abc123"
    assert summary["recent"][0]["model"] == "reasoner-v2"


def test_usage_validation_and_per_asset_summaries(tmp_path):
    store = LibraryUsageStore(tmp_path / ".memory")
    store.record([_asset("one"), _asset("two")], task="Task", outcome="partial")
    summaries = store.summaries()
    assert summaries["one"]["outcomes"]["partial"] == 1
    assert summaries["two"]["uses"] == 1

    try:
        store.record([_asset()], task="Task", effectiveness=6)
    except ValueError as exc:
        assert "between 1 and 5" in str(exc)
    else:
        raise AssertionError("invalid score accepted")
