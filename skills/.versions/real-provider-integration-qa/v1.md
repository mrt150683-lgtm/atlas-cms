---
id: real-provider-integration-qa
name: Verify Real Provider Integrations
type: skill
description: Verify integrations with AI models, media services, APIs, authentication, embeds, voice, search, or other external providers using real behavior. Load when a feature claims live data, real playback, model output, provider fallback, authentication, retries, streaming, or production readiness.
tags: [providers, integration, live, auth, resilience]
---

# Verify Real Provider Integrations

A mock proves wiring. It does not prove the product claim.

## Establish the contract

For each provider, record capability, API or protocol, authentication mode, scopes, rate and cost limits, data handling, expected latency, and supported failure behavior. Separate required provider behavior from product fallback.

## Verify the real path

Exercise with authorized credentials and non-sensitive test data:

- first authentication and restored session;
- success with representative real content;
- streaming or partial results;
- cancellation and navigation away;
- slow response and timeout;
- quota, rate limit, expired token, revoked consent, and unavailable region;
- malformed, empty, and unexpectedly large provider output;
- provider schema or capability mismatch;
- retry, deduplication, and idempotency;
- cache freshness and cross-user isolation;
- offline and recovery after reconnect.

Capture request identity, timing, status, provider/model version when exposed, and user-visible result without leaking secrets.

## Enforce honesty

- Label mock, cached, fallback, and live output accurately.
- Do not silently replace a failed semantic provider with templated content and call it success.
- Preserve the last known good result only when the UI makes staleness visible.
- Distinguish provider failure from product failure.
- Make retry and account repair actionable.

## Assess experience

Judge whether the real content is useful, varied, correctly rendered or played, and integrated into the product's actual workflow. A 200 response with unusable content is not success.

## Output

Provide a provider matrix, real evidence, failure-path results, fallback truthfulness, cost/latency observations, security concerns, and exact unverified paths.
