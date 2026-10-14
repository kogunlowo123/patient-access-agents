# ADR-0006: Vertex AI credentials from GKE Workload Identity, refreshed in process

- Status: Accepted
- Date: 2026-10-14

## Context

The agentic-core v0.1.0 Gemini adapter takes a bearer token at construction.
Workload Identity access tokens last about an hour; a token baked into an
environment variable would expire under a running process.

## Decision

- `ModelAccess` owns the `ModelRouter` and rebuilds it every
  `PAA_MODEL_REFRESH_MINUTES` (default 30) when the token source is
  `gke-metadata` (token from the metadata server) or `file` (token written by
  the platform). The previous router is closed after replacement.
- Configuration is validated at start-up: a bad `providers.yaml` or a missing
  variable stops the pod instead of failing on the first reminder.
- The NetworkPolicy opens the metadata server only when models are enabled.

## Consequences

- No long-lived Google credentials anywhere; the service account needs only
  `roles/aiplatform.user`.
- Router circuit-breaker state resets at each refresh, an acceptable cost.
