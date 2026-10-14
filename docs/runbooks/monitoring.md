# Runbook: monitoring

## Signals

| Signal | Source | Normal |
|---|---|---|
| `paa:request_errors:ratio_5m` | Prometheus recording rule | < 0.001 |
| p95 latency by route | `paa_http_request_duration_seconds` | case read < 300 ms; referral processing < 5 s |
| `paa_eligibility_outcomes_total{outcome}` | Prometheus | `unavailable` share low and steady |
| `paa_open_escalations{queue}` | Gauge refreshed every worker cycle | Stable, drained daily |
| `paa_escalations_total{reason}` | Prometheus | `sla_breach` rare |
| `paa_reminder_deliveries_total{channel,outcome}` | Prometheus | Failures < 5% |
| `paa_message_composer_total{composer,reason}` | Prometheus | `model` dominant when drafting is enabled |
| `paa_worker_runs_total{outcome}` | Prometheus | `failed` = 0 |
| `paa_authorization_decisions_total{action,decision}` | Prometheus | Deny share stable per release |
| Traces | OTLP | Span per request, FHIR and model calls as children |
| Logs | JSON on stdout | `correlation_id` on every line; no FHIR URLs |

Alerts are defined in `configs/prometheus/alerts.yaml`.

## Latency

1. Dashboard panel "p95 latency by route" shows which route.
2. `/v1/referrals/process` slow, reads normal: an upstream is slow. Traces
   show whether the FHIR slot search (pages) or the clearinghouse exchange
   dominates. Check the clearinghouse status page and FHIR server metrics.
3. All routes slow: check Cloud SQL CPU and connections, then pod CPU
   throttling. Scale with the HPA ceiling if CPU-bound.

## Tracing a request

Every response carries `X-Correlation-ID`; audit events carry the same id:
`jq 'select(.correlation_id=="<id>")' <pod>.jsonl`.

## Daily checks for the access-centre lead

- Open escalations by queue and age (dashboard and
  `GET /v1/escalations?status=open`).
- Cases in `eligibility_pending` without a scheduled retry.
- Composer fallback reasons, if model drafting is enabled.
