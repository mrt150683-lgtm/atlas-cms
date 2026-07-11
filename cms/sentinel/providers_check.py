"""Sentinel Driver/Plugin Validator — mapped onto CMS's LLM provider layer.

CMS has no instrument drivers; its pluggable driver architecture is the
``SummaryProvider`` protocol in cms/providers.py (anthropic / openai-compat /
mock). The spec's driver capabilities map as: get_identity -> ``name``,
run_measurement -> ``summarize(prompt, context)``, connect/disconnect -> N/A
(stateless HTTP), error simulation -> the pipeline's provider-failure
fallbacks. The mapping is documented in docs/HERMES_SENTINEL.md.

Checks are live where safe (mock is instantiated and exercised; network
providers are validated structurally only — no network calls from a scan).
"""

from __future__ import annotations

import inspect
from pathlib import Path

from . import make_finding

REQUIRED_INTERFACE = ("name", "summarize")


def _provider_classes():
    from ..providers import AnthropicProvider, MockProvider, OpenAICompatProvider

    return [AnthropicProvider, OpenAICompatProvider, MockProvider]


# @memory:feature:HermesSentinel
# @memory:connects:LLMProviderAbstraction
# @memory:summary:Driver validator mapped to the LLM provider layer — interface conformance for all providers, live checks that mock output is deterministic and self-labelling, and that provider resolution fails loudly on unknown names.
def check_providers(root: Path) -> list[dict]:
    findings: list[dict] = []
    try:
        classes = _provider_classes()
    except Exception as exc:
        return [make_finding(
            "providers", "high", f"cms.providers failed to import: {exc}",
            area="provider_interface", file="cms/providers.py", pattern="import-error",
            risk="No summaries can be generated at all.",
            recommendation="Fix the import error.",
        )]

    names = set()
    for cls in classes:
        for attr in REQUIRED_INTERFACE:
            member = getattr(cls, attr, None) if attr != "name" else getattr(cls, "name", None)
            if member is None:
                findings.append(make_finding(
                    "providers", "high",
                    f"{cls.__name__} is missing required interface member {attr!r}",
                    area="provider_interface", file="cms/providers.py",
                    pattern="missing-interface-member",
                    risk="get_provider can return an object the pipeline cannot drive.",
                    recommendation=f"Implement {attr} on {cls.__name__} per the SummaryProvider protocol.",
                ))
        if callable(getattr(cls, "summarize", None)):
            params = list(inspect.signature(cls.summarize).parameters)
            if params[:3] != ["self", "prompt", "context"]:
                findings.append(make_finding(
                    "providers", "high",
                    f"{cls.__name__}.summarize signature is {params}, expected (self, prompt, context)",
                    area="provider_interface", file="cms/providers.py",
                    pattern="interface-signature-drift",
                    risk="Callers pass (prompt, context) positionally; drift breaks every summary call.",
                    recommendation="Match the SummaryProvider protocol signature.",
                ))
        name = getattr(cls, "name", None)
        if name in names:
            findings.append(make_finding(
                "providers", "medium",
                f"duplicate provider name {name!r} — provenance stamps become ambiguous",
                area="provider_interface", file="cms/providers.py", pattern="duplicate-name",
                recommendation="Give every provider a unique name.",
            ))
        names.add(name)

    # live checks on the mock driver only (no network)
    try:
        from ..providers import MockProvider, get_provider

        mock = MockProvider()
        if mock.name != "mock":
            findings.append(make_finding(
                "providers", "critical",
                f"MockProvider.name is {mock.name!r} — mock output would not be labelled as mock",
                area="mock_labelling", file="cms/providers.py", pattern="mock-mislabelled",
                risk="Simulated data becomes indistinguishable from live AI output.",
                recommendation="MockProvider.name must be 'mock'; every provenance check keys on it.",
            ))
        ctx = {"path": "probe.py", "language": "python", "line_count": 1,
               "components": [], "imports": []}
        out1, out2 = mock.summarize("p", dict(ctx)), mock.summarize("p", dict(ctx))
        if out1 != out2:
            findings.append(make_finding(
                "providers", "high",
                "MockProvider is not deterministic for identical input",
                area="mock_simulation", file="cms/providers.py", pattern="mock-nondeterministic",
                risk="Mock runs cannot be used as reproducible simulations.",
                recommendation="Keep mock output a pure function of the structural context.",
            ))
        if "mock" not in out1.lower():
            findings.append(make_finding(
                "providers", "critical",
                "MockProvider output does not label itself as mock/simulated",
                area="mock_labelling", file="cms/providers.py", pattern="mock-unlabelled-output",
                evidence=[out1[:160]],
                risk="Fake success state: structural text could pass as AI analysis.",
                recommendation="Include an explicit mock marker in every mock summary.",
            ))
        if get_provider("mock").name != "mock":
            findings.append(make_finding(
                "providers", "high", "get_provider('mock') did not return the mock provider",
                area="provider_resolution", file="cms/providers.py", pattern="resolution-drift",
                recommendation="Fix get_provider dispatch.",
            ))
        try:
            get_provider("no-such-provider")
            findings.append(make_finding(
                "providers", "high",
                "get_provider accepts unknown provider names instead of failing loudly",
                area="provider_resolution", file="cms/providers.py", pattern="silent-unknown-provider",
                risk="A typo silently changes which engine writes the memory.",
                recommendation="Raise ValueError for unknown names (current documented behaviour).",
            ))
        except ValueError:
            pass
    except Exception as exc:
        findings.append(make_finding(
            "providers", "medium", f"live mock-provider checks errored: {exc}",
            area="mock_simulation", file="cms/providers.py", pattern="mock-check-error",
            recommendation="Run `cms sentinel run` locally and inspect the traceback.",
        ))
    return findings
