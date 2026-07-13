"""Phase 3: pluggable LLM providers for summary generation.

Every provider implements ``summarize(prompt, context) -> str``. ``context``
carries structural facts (components, imports, metadata) so the MockProvider
can produce deterministic summaries without any network access.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Protocol

from . import config
from .anchors import anchors_as_text


class SummaryProvider(Protocol):
    name: str

    def summarize(self, prompt: str, context: dict) -> str: ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str | None = None) -> None:
        import anthropic  # optional dependency: pip install cms[anthropic]

        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self.model = model or config.ANTHROPIC_MODEL

    def summarize(self, prompt: str, context: dict) -> str:
        kwargs = {}
        if context.get("temperature") is not None:
            kwargs["temperature"] = float(context["temperature"])  # brainstorm dial
        response = self._client.messages.create(
            model=self.model,
            # callers with structured output (e.g. feature discovery over a
            # large repo) can raise the ceiling so JSON doesn't truncate
            max_tokens=int(context.get("max_tokens") or config.MAX_TOKENS),
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        return "".join(b.text for b in response.content if b.type == "text").strip()


class OpenAICompatProvider:
    """Works with any OpenAI-compatible endpoint: Ollama, LM Studio, xAI, OpenAI."""

    name = "openai"

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        self.base_url = (base_url or config.OPENAI_BASE_URL).rstrip("/")
        self.model = model or config.OPENAI_MODEL
        self.api_key = os.environ.get("CMS_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")

    def summarize(self, prompt: str, context: dict) -> str:
        body = {
            "model": self.model,
            "max_tokens": int(context.get("max_tokens") or config.MAX_TOKENS),
            "messages": [{"role": "user", "content": prompt}],
        }
        if context.get("temperature") is not None:
            body["temperature"] = float(context["temperature"])
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()


class MockProvider:
    """Deterministic structural summaries built from graph facts — no LLM, no network."""

    name = "mock"

    def summarize(self, prompt: str, context: dict) -> str:
        path = context.get("path", "?")
        language = context.get("language", "?")
        line_count = context.get("line_count", 0)
        components = context.get("components", [])
        imports = context.get("imports", [])

        funcs = [c for c in components if c["kind"] == "func"]
        classes = [c for c in components if c["kind"] == "class"]
        lines = [
            "1. **File Purpose**",
            f"   - `{path}`: {language} file, {line_count} lines, defining "
            f"{len(funcs)} function(s) and {len(classes)} class(es). "
            "(Auto-generated structural summary — mock provider, no LLM.)",
            "",
            "2. **Key Components**",
        ]
        if components:
            for c in components:
                head = c.get("signature") or f"class {c['name']}"
                doc = (c.get("docstring") or "").strip().splitlines()
                doc_note = f" — {doc[0]}" if doc else ""
                lines.append(f"   - `{head}` {c['start_line']}-{c['end_line']}{doc_note}")
                if c.get("anchors"):
                    lines.append(f"     - anchors: {anchors_as_text(c['anchors'])}")
        else:
            lines.append("   - (no top-level functions or classes)")
        lines += [
            "",
            "3. **Important Connections**",
            f"   - Imports: {', '.join(imports) if imports else '(none)'}",
        ]
        if context.get("anchors"):
            lines.append(f"   - File anchors: {anchors_as_text(context['anchors'])}")
        return "\n".join(lines)


def provider_identity(name: str | None = None) -> dict:
    """Which provider WOULD be used — name/model/real — resolved from config
    and env only. Never imports provider SDKs (a cold `import anthropic` can
    take minutes under AV/disk load), so status endpoints stay instant."""
    name = (name or os.environ.get(config.ENV_PROVIDER) or "").lower()
    if not name:
        name = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "mock"
    model = {"anthropic": config.ANTHROPIC_MODEL,
             "openai": config.OPENAI_MODEL}.get(name)
    return {"name": name, "model": model, "real": name != "mock"}


def get_provider(name: str | None = None) -> SummaryProvider:
    """Resolve a provider by explicit name, env var, or availability fallback."""
    name = name or os.environ.get(config.ENV_PROVIDER)
    if name is None:
        name = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "mock"
        if name == "mock":
            print(
                "cms: no API key configured — using the mock provider (deterministic "
                "structural summaries, no LLM). To enable real AI summaries run:\n"
                "  cms config set anthropic_api_key <your key>",
                file=sys.stderr,
            )
    name = name.lower()
    if name == "anthropic":
        try:
            return AnthropicProvider()
        except Exception as exc:  # missing SDK or key — degrade gracefully
            print(f"cms: anthropic provider unavailable ({exc}); falling back to mock.", file=sys.stderr)
            return MockProvider()
    if name == "openai":
        return OpenAICompatProvider()
    if name == "mock":
        return MockProvider()
    raise ValueError(f"Unknown provider {name!r}; expected anthropic, openai, or mock")
