# Patient Access Agents

Agents that take a specialty referral from receipt to a booked appointment,
and hand every exception to the right person. A FHIR ServiceRequest is
routed to a bookable service, insurance eligibility is checked with a real
X12 270/271 exchange, a constraint solver ranks free FHIR slots against the
patient's availability and the referral's urgency, reminders follow up
without waking anyone at 3 a.m., and anything an agent should not decide
(denials, prior authorization, no capacity, its own bookings) goes to a
human queue with an SLA.

```
Referral ─▶ Eligibility Verification ─▶ Scheduling ─▶ Follow-Up Coordination ─▶ Escalation
```

## Capabilities

| Capability | Implementation |
|---|---|
| Referral intake | FHIR R4 `ServiceRequest` (status, intent, priority, codes, performer, location, occurrence period, insurance); idempotent per ServiceRequest; routing table in `configs/access-rules.yaml` |
| Eligibility | ASC X12 005010X279A1 270 builder (ISA..IEA, subscriber/dependent loops, EQ service types, NPI check digit) and 271 parser (envelope integrity, AAA rejects, TRN echo, EB benefits with MSG/DTP, LS/LE skipping, 999/TA1); conservative interpretation; retry with back-off; manual verification |
| Scheduling | FHIR `Slot` search with `_include` and bounded paging; solver over provider, location, modality, patient availability (IANA time zone, DST-safe), lead time, deadline; contiguous slot chaining; ranked, diversified options; binding-constraint report |
| Booking | `If-Match` on Slot, conditional `Appointment` create, compensation on failure; agent bookings require human approval; requester ≠ approver; prior-auth holds cleared by eligibility staff |
| Follow-up | Outreach and appointment reminders, quiet hours in patient time zone, exponential back-off, delivery via portal inbox or HMAC-signed messaging webhook; wording drafted by Gemini (Vertex AI) from non-identifying facts with a reviewed-template fallback |
| Escalation | Human queues (eligibility, scheduling, care navigation, supervisor) with de-duplication, claim/resolve and SLA at-risk/breach detection |
| Identity-aware access | Patients see only their own records (404 otherwise); staff by role with minimum-necessary projections; agents by scope via agent-identity-fabric (or local JWKS), never allow-all |
| Agent interfaces | MCP tools (official `mcp` SDK, streamable HTTP at `/mcp`), A2A Agent Card and JSON-RPC `message/send` at `/a2a`, Google ADK tool adapter |
| Governance | agentic-core PolicyEngine (deny, require_approval, allow), signed hash-chained audit of every decision and state change, ContentGuard on model input and output |
| Observability | Prometheus metrics, SLO and operations alerts, Grafana dashboard, OpenTelemetry traces, JSON logs with correlation ids |

Shared controls come from [agentic-core](https://github.com/kogunlowo123/agentic-core);
agent identity from [agent-identity-fabric](https://github.com/kogunlowo123/agent-identity-fabric).

## Quick start (local)

Requires Docker. No API keys or cloud credentials are needed.

```bash
docker compose up --build
curl -s localhost:8080/.well-known/agent-card.json | jq '.skills[].id'
```

The stack runs PostgreSQL, a HAPI FHIR R4 server (port 8090), an OIDC
provider for people and agents, and an OpenTelemetry collector. Load your own
synthetic Patient, Coverage, Organization, Practitioner, Location, Schedule,
Slot and ServiceRequest resources into HAPI to try a referral. Without a
clearinghouse, eligibility goes to the manual-verification queue; reminders
land in the patient portal inbox (`GET /v1/messages`).

## Development

```bash
uv sync
make check        # ruff, black, mypy --strict, bandit, contracts, tests (coverage >= 90%)
make integration  # PostgreSQL tests (needs PAA_TEST_DATABASE_URL)
```

Test suites: `tests/unit`, `tests/security` (attacks mapped to the threat
model), `tests/chaos` (FHIR, clearinghouse, messaging, fabric, database and
audit failures), `tests/performance` (latency budgets; `locustfile.py` for
load), `tests/e2e` (REST, MCP and A2A over HTTP, plus a property-based OpenAPI
contract test), `tests/integration` (PostgreSQL). External systems are
in-process fakes speaking their real wire formats (`tests/fakes.py`); all
patient data is synthetic.

## API

| Endpoint | Caller | Purpose |
|---|---|---|
| `POST /v1/referrals` | Staff, agents | Intake a ServiceRequest (idempotent) |
| `POST /v1/referrals/process` | Staff, agents | Intake → eligibility → slot proposal, until a person is needed |
| `GET /v1/cases`, `GET /v1/cases/{id}` | Patients (own), staff, agents | Cases, projected per caller |
| `PUT /v1/cases/{id}/preferences` | Patient, staff | Availability windows, time zone, modalities, locations, channel |
| `POST /v1/cases/{id}/route` | Schedulers, navigators | Route an unrouted referral |
| `POST /v1/cases/{id}/eligibility` | Staff, agents | Real-time 270/271 |
| `POST /v1/cases/{id}/eligibility/manual` | Eligibility specialists | Record a payer-verified result |
| `POST /v1/cases/{id}/slot-search` | Patient, staff, agents | Ranked options with constraint report |
| `POST /v1/cases/{id}/booking` | Patient, staff, agents (approval) | Book an option (202 when approval is required) |
| `POST /v1/cases/{id}/booking/decision` | Schedulers, eligibility staff | Approve or decline a pending booking |
| `POST /v1/cases/{id}/close` | Supervisors | Close with a reason |
| `GET /v1/cases/{id}/reminders`, `POST /v1/reminders/{id}/acknowledge` | Patient, staff | Follow-up |
| `GET /v1/messages` | Patient | Portal inbox |
| `GET /v1/escalations`, `POST .../claim`, `POST .../resolve` | Staff | Human queues |
| `/mcp` | Agents | MCP tools |
| `GET /.well-known/agent-card.json`, `POST /a2a` | Agents | A2A |

Full contract: [docs/api/openapi.json](docs/api/openapi.json); JSON Schemas in [schemas/](schemas/).

## Deployment

| Layer | Location |
|---|---|
| Container image | `Dockerfile` (non-root, read-only root filesystem compatible) |
| Helm chart | `deployments/helm/patient-access-agents` (StatefulSet, PDB, HPA, NetworkPolicy, ServiceMonitor, Ingress) |
| Namespace and secret sync | `deployments/kubernetes` (restricted Pod Security, External Secrets) |
| Cloud infrastructure | `deployments/terraform/gcp` (reference: Cloud SQL, KMS, Secret Manager, retention-locked audit bucket, Workload Identity, Vertex AI access), plus `aws` and `azure` |

See the [startup runbook](docs/runbooks/startup.md).

## Documentation

- [Architecture](docs/architecture/overview.md) (C4, sequences, deployment, MCP/A2A, observability, cost, acceptance criteria) and [agent specification](docs/architecture/agent-specification.md)
- [ADRs](docs/architecture/adr/)
- [Threat model and risk register](docs/security/threat-model.md)
- [Compliance control mapping](docs/compliance/control-mapping.md)
- Runbooks: [startup](docs/runbooks/startup.md), [monitoring](docs/runbooks/monitoring.md), [incident response](docs/runbooks/incident-response.md), [escalation queues](docs/runbooks/escalation-queues.md), [rollback](docs/runbooks/rollback.md), [disaster recovery](docs/runbooks/disaster-recovery.md)
- [Configuration reference](docs/operations/configuration.md)

## License

Apache-2.0
