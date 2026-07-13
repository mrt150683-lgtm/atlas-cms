"""Durable semantic-state evidence — positive proof that each semantic stage ran.

Atlas must never infer semantic completion from incidental graph contents
(a graph existing, summaries existing, judgment nodes existing, zero
features, the absence of mock labels, or the absence of an exception).
This module persists, per stage, an explicit record of what was attempted,
with which provider, over which inputs, producing which outputs — written
atomically to ``.memory/semantic_state.json``.

Stages: ``summaries``, ``features`` (LLM discovery), ``review``,
``suggestions``. Statuses: ``complete | failed | skipped | never_run``
(staleness is *derived* by comparing recorded hashes against the current
graph — see :func:`derive_staleness` — so a frozen-but-valid judgment keeps
its ``complete`` record and is merely *exposed* as stale, per the
review-freeze policy).

No secrets, prompts, or provider credentials are stored here.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from . import config

SCHEMA_VERSION = 1
STATE_FILENAME = "semantic_state.json"
STAGES = ("summaries", "features", "review", "suggestions")
# bump when DISCOVERY_PROMPT / feature semantics change enough that old
# discovery output should be considered non-current
DISCOVERY_SCHEMA_VERSION = 1

NEVER_RUN = {"status": "never_run"}


def state_path(memory_dir: Path) -> Path:
    return memory_dir / STATE_FILENAME


def load_state(memory_dir: Path) -> dict:
    try:
        return json.loads(state_path(memory_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def stage(state: dict, name: str) -> dict:
    return (state.get("stages") or {}).get(name) or dict(NEVER_RUN)


def record_stage(memory_dir: Path, name: str, **fields) -> dict:
    """Read-modify-write one stage record; atomic replace on save."""
    state = load_state(memory_dir)
    state["schema_version"] = SCHEMA_VERSION
    stages = state.setdefault("stages", {})
    record = dict(fields)
    record.setdefault("generated_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    record["schema_version"] = SCHEMA_VERSION
    stages[name] = record
    memory_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(state_path(memory_dir), state)
    return state


def atomic_write_json(dest: Path, payload) -> None:
    """Atomic replace safe under concurrent writers on Windows: a UNIQUE
    temp name per writer (a shared '.tmp' name lets two threads truncate
    each other mid-replace), plus a short retry for transient sharing
    violations (readers/AV holding the destination)."""
    tmp = dest.with_suffix(f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if attempt == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.03 * (attempt + 1))


def _sha(items) -> str:
    return hashlib.sha256(
        json.dumps(items, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:24]


def discovery_input_hash(graph) -> str:
    """Deterministic hash of everything feature discovery evaluates: the
    file set, the summaries fed into the prompt, declared-feature anchors,
    and the discovery schema version. Ordering-stable; no timestamps."""
    files = sorted(
        (a.get("path", ""), (a.get("summary") or "").strip())
        for _, a in graph.nodes(data=True) if a.get("type") == "file"
    )
    anchors = sorted(
        (a.get("path") or a.get("qualname") or n, json.dumps(a.get("anchors"), sort_keys=True))
        for n, a in graph.nodes(data=True) if a.get("anchors")
    )
    return _sha([DISCOVERY_SCHEMA_VERSION, files, anchors])


def feature_set_hash(graph) -> str:
    """Deterministic hash of the semantic feature set a judgment evaluates:
    names, aliases, declared/discovered source, members, entry points, connections
    and descriptions. No unstable ordering, no timestamps."""
    feats = sorted(
        [
            a.get("name", ""), a.get("source", ""),
            sorted(a.get("members") or []),
            sorted(a.get("entry_points") or []),
            sorted(a.get("connects") or []),
            sorted(a.get("aliases") or []),
            (a.get("description") or "").strip(),
        ]
        for _, a in graph.nodes(data=True) if a.get("type") == "feature"
    )
    return _sha(feats)


def feature_counts(graph) -> dict:
    total = declared = discovered = 0
    for _, a in graph.nodes(data=True):
        if a.get("type") != "feature":
            continue
        total += 1
        if a.get("source") == "discovered":
            discovered += 1
        else:
            declared += 1
    return {"feature_count": total, "declared_feature_count": declared,
            "discovered_feature_count": discovered}


# ── judgment validity ────────────────────────────────────────────────────

def pipeline_status(state: dict) -> dict:
    """The FINISHED contract, derived from stage evidence (never stored as a
    separate flag that could drift from the facts):

    - ``finished``    — every semantic stage is positively complete; the
      pipeline has nothing left to do and only waits for changes (watcher).
    - ``attention``   — a stage FAILED; it will retry (input change or
      cooldown) but the human should know.
    - ``in_progress`` — stages remain (never_run/skipped); a build with a
      real provider will continue exactly where the evidence says it
      stopped, until finished.
    """
    remaining = []
    failed = []
    for name in STAGES:
        status = stage(state, name).get("status")
        if status == "failed":
            failed.append(name)
        elif status != "complete":
            remaining.append(name)
    if failed:
        return {"status": "attention", "failed": failed, "remaining": remaining}
    if remaining:
        return {"status": "in_progress", "remaining": remaining}
    return {"status": "finished", "remaining": []}


def judgment_validity(state: dict, graph, node_id: str, stage_name: str) -> tuple[str, str]:
    """Classify a judgment artifact (review:app / suggestions:app).

    Returns (verdict, reason) with verdict one of:
      - ``missing``  — node absent: build it (initialization).
      - ``invalid``  — node exists but is not a real judgment of a real
        feature set: mock/structural output, no semantic-state evidence
        (legacy), or generated against an empty pre-discovery feature set
        while features now exist. Rebuild automatically.
      - ``stale``    — a VALID real-provider judgment whose recorded
        feature_set_hash no longer matches the current one. Deliberately
        frozen: exposed, never silently regenerated (refresh via
        `cms review` / `cms suggest`).
      - ``valid``    — real, evidenced, hash-current. No-op.
    """
    if not graph.has_node(node_id):
        return "missing", "artifact absent"
    rec = stage(state, stage_name)
    if rec.get("status") != "complete":
        return "invalid", f"no positive completion evidence (state: {rec.get('status')})"
    if not rec.get("real_provider"):
        return "invalid", "generated without a real provider"
    counts = feature_counts(graph)
    if rec.get("feature_count", 0) == 0 and counts["feature_count"] > 0:
        return "invalid", "generated against an empty pre-discovery feature set"
    if rec.get("feature_set_hash") != feature_set_hash(graph):
        return "stale", "feature set changed since this judgment was generated"
    return "valid", "current"


def derive_staleness(state: dict, graph) -> dict:
    """Live view: per-stage currency, computed against the current graph.
    Serves the UI/API; never mutates the durable record."""
    out = {}
    cur_input = discovery_input_hash(graph)
    cur_fsh = feature_set_hash(graph)
    feats = stage(state, "features")
    out["features"] = {
        "current": feats.get("status") == "complete" and feats.get("input_hash") == cur_input,
        "current_input_hash": cur_input,
    }
    for name, node_id in (("review", "review:app"), ("suggestions", "suggestions:app")):
        verdict, reason = judgment_validity(state, graph, node_id, name)
        out[name] = {"validity": verdict, "reason": reason,
                     "current_feature_set_hash": cur_fsh}
    return out


def live_pipeline_status(state: dict, graph) -> dict:
    """Combine durable completion with the currency of judgment artifacts.

    Stale paid judgments remain frozen for inspection, but Atlas must not call
    the overall pipeline finished while they require an explicit refresh.
    """
    status = pipeline_status(state)
    if status["status"] != "finished":
        return status
    live = derive_staleness(state, graph)
    remaining = [
        name for name in ("review", "suggestions")
        if live[name]["validity"] != "valid"
    ]
    if remaining:
        return {
            "status": "in_progress",
            "remaining": remaining,
            "reason": "judgment artifacts require an explicit refresh",
        }
    return status


def artifact_provenance(state: dict) -> dict:
    """Summarize who generated the durable semantic artifacts.

    Runtime provider availability is a separate fact; this describes only
    positively completed, real-provider stage output already on disk.
    """
    completed = []
    for name in STAGES:
        rec = stage(state, name)
        if rec.get("status") == "complete" and rec.get("real_provider"):
            completed.append({
                "stage": name,
                "provider": rec.get("provider"),
                "model": rec.get("model"),
                "generated_at": rec.get("generated_at"),
            })
    identities = sorted({
        (rec["provider"], rec["model"])
        for rec in completed if rec["provider"]
    })
    return {
        "available": bool(completed),
        "identities": [
            {"provider": provider, "model": model}
            for provider, model in identities
        ],
        "stages": completed,
    }
