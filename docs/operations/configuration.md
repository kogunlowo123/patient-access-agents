# Configuration reference

All settings are `PAA_*` environment variables (see
`src/patient_access_agents/config.py`). The service refuses to start with
incomplete or inconsistent configuration.

## Core

| Variable | Required | Default | Description |
|---|---|---|---|
| `PAA_ENVIRONMENT` | no | `production` | `production` forbids the ephemeral audit key |
| `PAA_PUBLIC_URL` | yes | | External base URL, used in the A2A Agent Card |
| `PAA_DATABASE_URL` | yes | | SQLAlchemy async URL (`postgresql+asyncpg://...`) |
| `PAA_AUDIT_KEY_FILE` | yes* | | Ed25519 PEM for audit signatures; key id is the file stem |
| `PAA_DEV_EPHEMERAL_AUDIT_KEY` | no | `false` | In-memory audit key, non-production only (replaces *) |
| `PAA_AUDIT_LOG_PATH` | no | `/var/lib/patient-access/audit/audit.jsonl` | Audit chain file |
| `PAA_POLICY_FILE` | no | `policies/patient-access.yaml` | Authorization policy |
| `PAA_RULES_FILE` | no | `configs/access-rules.yaml` | SLAs, retries, routing, queues |
| `PAA_DEFAULT_TIMEZONE` | no | `America/New_York` | IANA zone for new cases until the patient sets one |
| `PAA_WORKERS_ENABLED` | no | `true` | Background eligibility retries, reminders, SLA checks |
| `PAA_WORKER_INTERVAL_S` | no | `60` | Seconds between work cycles (1–3600) |
| `PAA_LOG_LEVEL` | no | `INFO` | |
| `PAA_OTLP_ENDPOINT` | no | | OTLP gRPC endpoint for traces |
| `PAA_HOST` / `PAA_PORT` | no | `127.0.0.1` / `8080` | Container image sets host `0.0.0.0` |
| `PAA_FORWARDED_ALLOW_IPS` | no | `127.0.0.1` | Proxies trusted for `X-Forwarded-*` |

## FHIR

| Variable | Required | Default | Description |
|---|---|---|---|
| `PAA_FHIR_BASE_URL` | yes | | FHIR R4 base URL |
| `PAA_FHIR_AUTH` | no | `none` | `fabric`: obtain an agent credential from the identity fabric for FHIR calls |
| `PAA_FHIR_SCOPES` | no | `["fhir:read:*","fhir:write:slot","fhir:write:appointment"]` | Scopes requested from the fabric (JSON list) |

## People (OIDC)

| Variable | Required | Default | Description |
|---|---|---|---|
| `PAA_OIDC_ISSUER` | yes | | Issuer of patient and staff tokens |
| `PAA_OIDC_AUDIENCE` | yes | | Audience those tokens must carry |
| `PAA_OIDC_JWKS_URL` | yes | | JWKS of the issuer |
| `PAA_ROLES_CLAIM` | no | `roles` | Claim with staff roles |
| `PAA_PATIENT_CLAIM` | no | `patient` | SMART patient-context claim (`fhirUser=Patient/<id>` also accepted) |

## Agents

Agent credentials are introspected by the fabric when `PAA_FABRIC_URL` is
set; otherwise verified against `PAA_AGENT_*`; with neither, refused.

| Variable | Required | Default | Description |
|---|---|---|---|
| `PAA_FABRIC_URL` | no | | agent-identity-fabric base URL |
| `PAA_FABRIC_CLIENT_ID` | with `PAA_FHIR_AUTH=fabric` | | This service's SPIFFE ID |
| `PAA_WORKLOAD_TOKEN_FILE` | with `PAA_FHIR_AUTH=fabric` | | Projected service-account token presented to the fabric |
| `PAA_AGENT_ISSUER`, `PAA_AGENT_AUDIENCE`, `PAA_AGENT_JWKS_URL` | together | | Local verification of agent JWTs |

## Eligibility (X12 270/271)

| Variable | Required | Default | Description |
|---|---|---|---|
| `PAA_CLEARINGHOUSE_URL` | no | | HTTPS endpoint accepting `application/EDI-X12`; unset = manual verification |
| `PAA_CLEARINGHOUSE_API_KEY` | no | | Bearer key for the clearinghouse (secret) |
| `PAA_X12_SENDER_ID` / `PAA_X12_RECEIVER_ID` | with clearinghouse | | ISA06 / ISA08 (≤ 15 chars) |
| `PAA_X12_APPLICATION_SENDER` / `PAA_X12_APPLICATION_RECEIVER` | with clearinghouse | | GS02 / GS03 |
| `PAA_X12_USAGE` | no | `P` | ISA15: `P` production, `T` test |
| `PAA_PROVIDER_NAME`, `PAA_PROVIDER_NPI`, `PAA_PROVIDER_TAX_ID` | with clearinghouse | | Information receiver (NPI check digit validated); TRN03 originator |

## Patient messaging

| Variable | Required | Default | Description |
|---|---|---|---|
| `PAA_NOTIFICATION_WEBHOOK_URL` | no | | SMS/e-mail service; unset = patient portal inbox only |
| `PAA_NOTIFICATION_SIGNING_KEY` | with webhook | | ≥ 32 characters; HMAC-SHA256 of each body in `X-Signature-256` |
| `PAA_PROVIDERS_FILE` | no | | `configs/providers.yaml`; unset = reviewed templates only |
| `PAA_MODEL_TOKEN_SOURCE` | no | `env` | `env`, `file` or `gke-metadata` (Workload Identity) |
| `PAA_MODEL_TOKEN_FILE` | with `file` | | File holding the current access token |
| `PAA_MODEL_TOKEN_ENV` | no | `GCP_ACCESS_TOKEN` | providers.yaml variable the token feeds |
| `PAA_MODEL_REFRESH_MINUTES` | no | `30` | Router rebuild interval for refreshed tokens (1–55) |

`configs/providers.yaml` also requires `GCP_REGION`, `GCP_PROJECT_ID`,
`GEMINI_PRO_INPUT_USD_PER_MTOK`, `GEMINI_PRO_OUTPUT_USD_PER_MTOK`,
`GEMINI_FLASH_INPUT_USD_PER_MTOK`, `GEMINI_FLASH_OUTPUT_USD_PER_MTOK` (your
contracted rates) and, for `env`, `GCP_ACCESS_TOKEN`. Loading fails if any is
unset.

## Operating rules (`configs/access-rules.yaml`)

Versioned with the code and owned by access operations: SLA per
ServiceRequest priority, at-risk threshold, eligibility retry budget,
reminder offsets, quiet hours and retry budget, solver weights and limits,
escalation reason → queue → role mapping, and the referral routing table
(codes → bookable service, slot specialty, X12 service types, visit length,
minimum lead time, allowed modalities). The JSON Schema is published as
`schemas/access-rules.config.schema.json`.
