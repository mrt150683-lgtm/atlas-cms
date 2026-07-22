"""AI Review layer — does the app as built align with what the user expects?

For every feature, the reviewer takes the *declared intent* (anchor description,
connects) as "expected", and the *evidence* (traced flows, member summaries,
tests mapped as exercising the feature) as "built", then judges alignment and explains it at three zoom
levels: a one-line verdict, an expected-vs-built explanation with gaps, and an
education note on how it actually works. An app-level rollup summarises the whole.

Stored on feature nodes as ``review`` plus a ``review:app`` node; exported to
``.memory/review.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx

from .features import get_features, get_planning_features
from .providers import SummaryProvider

VERDICTS = ("aligned", "partial", "drift", "unverified")
REVIEW_MAX_TOKENS = 1800

FEATURE_REVIEW_PROMPT = """You are reviewing one feature of an application on behalf of its END USER.
Your job: judge whether what was BUILT matches what the user EXPECTS, then explain it
so simply that a non-programmer understands — while staying strictly factual.

App context:
{app_context}

Feature: {name}
Expected (declared intent by the user/developer):
{expected}

Built (evidence from the code graph):
- Members: {members}
- Traced call flows:
{flows}
- Member summaries:
{member_summaries}
- Verified by {test_count} test(s): {tests}

Respond with ONLY a JSON object, no prose, exactly these keys:
{{
  "verdict": "aligned" | "partial" | "drift" | "unverified",
  "headline": "<ONE plain-English sentence a non-programmer gets, e.g. 'Scans your project and correctly ignores junk folders.'>",
  "expected": "<2-3 sentences: what the user asked for / expects this to do>",
  "built": "<2-3 sentences: what the code actually does, in plain words>",
  "gaps": ["<each concrete mismatch, missing piece, or risk — empty list if none>"],
  "education": "<3-5 sentences teaching the user how this really works under the hood and why it was built this way>"
}}

Rules: verdict 'aligned' only when evidence clearly covers the intent; 'partial' when core intent is met with gaps; 'drift' when built behaviour contradicts intent; 'unverified' when evidence is too thin to judge. Never invent behaviour not in the evidence.
"""

APP_REVIEW_PROMPT = """You are writing the END USER a top-level review of their application, based on per-feature reviews.

App context:
{app_context}

Feature verdicts:
{verdict_lines}

Respond with ONLY a JSON object:
{{
  "verdict": "aligned" | "partial" | "drift" | "unverified",
  "headline": "<one plain sentence: overall, does the app do what the user expects?>",
  "summary": "<4-6 sentences: the honest state of the app vs expectations — what is solid, what needs attention, what to check next>"
}}
"""


def _app_context(root: Path) -> str:
    readme = root / "README.md"
    if readme.is_file():
        lines = readme.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[:30])
    return "(no README)"


def _flow_lines(feat: dict) -> str:
    out = []
    for flow in (feat.get("flows") or [])[:5]:
        out.append("  " + " -> ".join(f"{s['name']} ({s['path']}:{s['line']})" for s in flow))
    return "\n".join(out) or "  (none)"


def _member_summaries(graph: nx.DiGraph, feat: dict) -> str:
    out = []
    for m in (feat.get("members") or [])[:8]:
        if graph.has_node(m):
            a = graph.nodes[m]
            head = (a.get("summary") or a.get("docstring") or "").strip().splitlines()
            if head:
                out.append(f"  {a.get('qualname', a.get('name'))}: {head[0][:150]}")
    return "\n".join(out) or "  (none)"


def _parse_json(raw: str) -> dict | None:
    """Return the first complete JSON object, even around fences or prose."""
    if not isinstance(raw, str):
        return None
    decoder = json.JSONDecoder()
    for offset, char in enumerate(raw):
        if char != "{":
            continue
        try:
            data, _end = decoder.raw_decode(raw[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _valid_feature_review(review: dict | None) -> bool:
    return bool(
        isinstance(review, dict)
        and review.get("verdict") in VERDICTS
        and all(isinstance(review.get(key), str) and review.get(key).strip()
                for key in ("headline", "expected", "built", "education"))
        and isinstance(review.get("gaps"), list)
    )


def _valid_app_review(review: dict | None) -> bool:
    return bool(
        isinstance(review, dict)
        and review.get("verdict") in VERDICTS
        and all(isinstance(review.get(key), str) and review.get(key).strip()
                for key in ("headline", "summary"))
    )


def _sanitize(review: dict) -> dict:
    clean = {
        "verdict": review.get("verdict") if review.get("verdict") in VERDICTS else "unverified",
        "headline": str(review.get("headline", ""))[:300],
        "expected": str(review.get("expected", ""))[:1200],
        "built": str(review.get("built", ""))[:1200],
        "gaps": [str(g)[:300] for g in (review.get("gaps") or [])[:8]],
        "education": str(review.get("education", ""))[:2000],
        "evidence_kind": "structural" if review.get("structural") else "semantic",
    }
    if review.get("structural"):
        clean["structural"] = True
    if review.get("provider_error"):
        clean["provider_error"] = str(review["provider_error"])[:300]
    return clean


def _structural_review(feat: dict, provider_error: str | None = None) -> dict:
    """No-LLM fallback: assemble evidence, refuse to judge."""
    tests = len(feat.get("exercised_by") or [])
    flows = len(feat.get("flows") or [])
    review = {
        "verdict": "unverified",
        "headline": feat.get("description") or f"{feat['name']} — no AI review yet (run `cms review` with an API key).",
        "expected": feat.get("description") or "(no declared intent)",
        "built": f"{len(feat.get('members') or [])} member(s), {flows} traced flow(s), "
                 f"{tests} exercising test(s). Structural evidence only — no AI judgement.",
        "gaps": [] if tests else ["No tests currently exercise this feature."],
        "education": "Run `cms review` with a configured provider for a full plain-English review.",
        "structural": True,  # positive marker: this is NOT semantic output
    }
    if provider_error:
        review["headline"] = f"AI review unavailable for {feat['name']}; structural evidence only."
        review["provider_error"] = provider_error[:300]
        review["gaps"] = [f"AI review failed: {provider_error[:240]}"] + review["gaps"]
    return review


def _provider_error(exc: Exception) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def build_review(graph: nx.DiGraph, root: Path, provider: SummaryProvider, on_progress=None) -> dict:
    """Review every feature + app rollup. Returns {"features": {...}, "app": {...}}."""
    app_context = _app_context(root)
    all_features = get_features(graph)
    features = get_planning_features(graph)
    excluded = [feature["name"] for feature in all_features
                if feature["planning_scope"] == "reference"]
    reviews: dict[str, dict] = {}
    provider_errors: list[str] = []

    for i, feat in enumerate(features, 1):
        if provider.name == "mock":
            review = _structural_review(feat)
        else:
            expected = feat.get("description") or "(no declared description)"
            if feat.get("connects"):
                expected += f"\nDeclared connections: {', '.join(feat['connects'])}"
            prompt = FEATURE_REVIEW_PROMPT.format(
                app_context=app_context,
                name=feat["name"],
                expected=expected,
                members=", ".join((feat.get("members") or [])[:10]),
                flows=_flow_lines(feat),
                member_summaries=_member_summaries(graph, feat),
                test_count=len(feat.get("exercised_by") or []),
                tests=", ".join((feat.get("exercised_by") or [])[:6]) or "(none)",
            )
            try:
                parsed = _parse_json(provider.summarize(
                    prompt, {"max_tokens": REVIEW_MAX_TOKENS}))
                if not _valid_feature_review(parsed):
                    reason = "provider returned malformed or incomplete feature-review JSON"
                    provider_errors.append(f"{feat['name']}: {reason}")
                    review = _structural_review(feat, reason)
                else:
                    review = parsed
            except Exception as exc:
                reason = _provider_error(exc)
                provider_errors.append(f"{feat['name']}: {reason}")
                review = _structural_review(feat, reason)
        review = _sanitize(review)
        review["feature"] = feat["name"]
        reviews[feat["name"]] = review
        if on_progress:
            on_progress(feat["name"], i, len(features))

    counts = {v: sum(1 for r in reviews.values() if r["verdict"] == v) for v in VERDICTS}
    semantic_count = sum(r.get("evidence_kind") == "semantic" for r in reviews.values())
    fallback_count = len(reviews) - semantic_count
    complete = provider.name != "mock" and fallback_count == 0
    if provider.name == "mock":
        app_review = {
            "verdict": "unverified",
            "headline": "Structural pass only — run `cms review` with an API key for the full alignment review.",
            "summary": f"{len(reviews)} features assembled with evidence. "
                       + ", ".join(f"{n} {v}" for v, n in counts.items() if n),
        }
    elif not reviews:
        app_review = {
            "verdict": "unverified",
            "headline": "No core product features are currently available to review.",
            "summary": (f"Atlas kept {len(excluded)} Library/reference feature(s) in the map, "
                        "but excluded them from the application alignment verdict."),
        }
    elif fallback_count:
        app_review = {
            "verdict": "unverified",
            "headline": "AI review did not complete; existing semantic review artifacts were preserved.",
            "summary": (f"{semantic_count} of {len(reviews)} feature reviews returned usable semantic output; "
                        f"{fallback_count} fell back to structural evidence. The run is failed, not partial, "
                        "because a complete app verdict cannot be earned from an incomplete provider pass."),
        }
    else:
        verdict_lines = "\n".join(
            f"- {name}: {r['verdict']} — {r['headline']}" for name, r in sorted(reviews.items())
        )
        try:
            parsed = _parse_json(provider.summarize(
                APP_REVIEW_PROMPT.format(app_context=app_context, verdict_lines=verdict_lines),
                {"max_tokens": REVIEW_MAX_TOKENS}
            ))
        except Exception as exc:
            provider_errors.append(f"app rollup: {_provider_error(exc)}")
            parsed = None
        if not _valid_app_review(parsed):
            if not any(error.startswith("app rollup:") for error in provider_errors):
                provider_errors.append("app rollup: provider returned malformed or incomplete JSON")
            complete = False
            app_review = {
                "verdict": "unverified",
                "headline": "Feature reviews completed, but the app-level AI review failed.",
                "summary": "No overall semantic verdict was stored. Existing review artifacts were preserved.",
            }
        else:
            app_review = {
                "verdict": parsed["verdict"],
                "headline": str(parsed["headline"])[:300],
                "summary": str(parsed["summary"])[:2000],
            }

    app_review["counts"] = counts
    app_review["semantic_features"] = semantic_count
    app_review["fallback_features"] = fallback_count
    app_review["excluded_reference_features"] = len(excluded)
    status = "complete" if complete else ("structural" if provider.name == "mock" else "failed")

    # A real-provider review is transactional: incomplete output must not
    # overwrite the last complete semantic review on the graph or on disk.
    if provider.name == "mock" or complete:
        for feat in features:
            graph.nodes[feat["id"]]["review"] = reviews[feat["name"]]
        graph.add_node("review:app", type="review", name="App Review", summary=app_review["summary"], **{
            "verdict": app_review["verdict"], "headline": app_review["headline"], "counts": counts,
            "review_status": status, "semantic_features": semantic_count,
            "fallback_features": fallback_count,
            "excluded_reference_features": len(excluded),
        })
    return {"features": reviews, "app": app_review, "status": status,
            "provider_errors": provider_errors, "excluded_features": excluded}


def export_review(graph: nx.DiGraph, memory_dir: Path) -> Path | None:
    features = [f for f in get_planning_features(graph) if f.get("review")]
    if not features and not graph.has_node("review:app"):
        return None
    lines = ["# App Review — built vs expected\n"]
    if graph.has_node("review:app"):
        app = graph.nodes["review:app"]
        lines += [f"**Overall: {app.get('verdict', '?').upper()}** — {app.get('headline', '')}\n",
                  app.get("summary", ""), ""]
    for f in features:
        r = f["review"]
        lines += [
            f"## {f['name']} — {r['verdict'].upper()}",
            f"*{r['headline']}*\n",
            f"**Expected:** {r['expected']}\n",
            f"**Built:** {r['built']}\n",
        ]
        if r.get("gaps"):
            lines.append("**Gaps:**")
            lines += [f"- {g}" for g in r["gaps"]]
            lines.append("")
        lines += [f"**How it works:** {r['education']}\n"]
    out = memory_dir / "review.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
