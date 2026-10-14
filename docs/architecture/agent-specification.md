# Agent specification

## Role and mission

- **Purpose:** move a specialty referral from receipt to a booked, confirmed
  appointment, and put every exception in front of the right person quickly.
- **Business objective:** fewer referrals lost between hand-offs; shorter time
  from referral to visit; access-centre staff spend their time on the cases
  that need judgement.
- **Protected resources:** FHIR ServiceRequest, Patient, Coverage, Schedule,
  Slot and Appointment resources; eligibility responses; patient contact
  details; the human work queues.
- **Operating boundaries:** reads referrals and coverage; sends eligibility
  inquiries; proposes appointment options; books only when a person (the
  patient or staff) books or approves; never cancels referrals, never
  denies care, never gives clinical advice, never writes clinical data.
- **Delegated responsibility:** coverage interpretation is delegated to the
  payer's 271 response; scheduling rules to `configs/access-rules.yaml`
  (owned by access operations); authorization to `policies/patient-access.yaml`
  (owned by privacy and security) and, for agents, to agent-identity-fabric.

## Delegated scope

| Allowed | Disallowed | Risk | Escalation |
|---|---|---|---|
| Intake active ServiceRequests (`case:create`) | Intake draft, revoked or non-order requests | MODERATE | Unroutable codes → care-navigation queue |
| Real-time eligibility (`eligibility:verify`) | Record or override eligibility (`eligibility:override`) | MODERATE | Denials, rejections, manual cases → eligibility queue |
| Search slots and rank options (`slots:search`) | Book outside the proposed options | LOW | No capacity → scheduling; availability conflict → care navigation |
| Request a booking (`appointment:book`) | Book without approval when the caller is an agent; approve its own request | MODERATE (agents: require_approval) | Booking approval → scheduling queue; prior auth → eligibility queue |
| Send reminders through the configured channel | Contact during quiet hours; include anything but the reviewed message | LOW | Delivery failure after retries → care navigation |
| Read the escalation queue (`escalation:read`) | Claim or resolve escalations, close cases | HIGH (people only) | SLA breach → supervisor |

## Objectives

| Type | Definition | Target |
|---|---|---|
| SLO availability | Non-5xx share of `/v1/*`, `/mcp`, `/a2a` | 99.9% over 30 days |
| SLO latency | p95 `GET /v1/cases/{id}` | < 300 ms |
| SLO latency | p95 `POST /v1/referrals/process` (includes FHIR and clearinghouse) | < 5 s |
| SLI | `paa:request_errors:ratio_5m`, latency histograms | Prometheus |
| KPI | Referral-to-booked within the priority SLA (stat 4 h, asap 24 h, urgent 72 h, routine 7 d) | Measured from case history |
| KPI | Share of referrals that reach `slots_proposed` with no human touch | Measured; target set by access operations |
| KPI | Open escalations older than one working day | Trending down |
| Completion | A case is complete when an Appointment is booked in the EHR and reminders are scheduled, or a supervisor closes it with a reason | Case state + audit chain |

## Permissions

| Mechanism | Use |
|---|---|
| RBAC | Staff roles from the OIDC roles claim: `access-scheduler`, `eligibility-specialist`, `care-navigator`, `access-supervisor`, `access-auditor` |
| ABAC | agentic-core PolicyEngine over subject (kind, roles, patient_id, scopes), resource (patient_id, status, prior_auth_pending, queue), environment (purpose of use) |
| Patient context | SMART `patient` claim or `fhirUser=Patient/<id>`; every patient action requires `resource.patient_id == subject.patient_id` |
| Agent identity | SPIFFE IDs and scopes from agent-identity-fabric; introspection and `/v1/authorize` (most restrictive of fabric and local decision wins) |
| OAuth 2.0 / OIDC | Bearer tokens (RFC 6750); fabric jwt-bearer grant (RFC 7523) for outbound FHIR credentials; introspection (RFC 7662) |
| Workload identity | GKE Workload Identity for Vertex AI and Cloud Storage; projected service-account token presented to the fabric |

## Inputs and outputs

All bodies are Pydantic models; JSON Schemas are published in `schemas/` and
the OpenAPI document in `docs/api/openapi.json` (`contracts --check` keeps
them current).

| Input | Validation |
|---|---|
| `IntakeRequest.service_request_id` | FHIR id pattern `[A-Za-z0-9\-.]{1,64}`; ServiceRequest must be `active`, an order intent, subject a Patient |
| `SchedulingPreferences` | IANA time zone, ISO weekdays, window end after start, FHIR ids for preferred/excluded resources, channel in portal/sms/email |
| `ManualEligibilityRequest` | Note and payer reference required |
| `BookingRequest.proposal_id` | Must be one of the case's current proposals |
| X12 271 | Envelope counts and control numbers, TRN echo must match the inquiry trace |
| Model drafts | ContentGuard, exact placeholders, no URLs or digit runs, length cap |

| Output | Content |
|---|---|
| `CaseView` | Projection per caller (minimum necessary: member id masked and benefits hidden for schedulers, navigators and agents) |
| `SlotSearchView` | Options with score breakdown; considered/eliminated counts per constraint; binding constraint when infeasible |
| `BookingView` | `booked` / `pending_approval` / `declined` with the policy decision |
| A2A `Task` | State, agent message and the case as a data artifact |

### Provenance and audit metadata

Every state change and decision writes an agentic-core audit event (hash
chained, Ed25519 signed) before the new state is persisted: actor, actor kind,
delegation chain, action, resource, outcome, correlation id, policy name,
version and digest, and — for eligibility — the X12 trace number and Coverage
reference. Eligibility results keep their source (`x12-271`, `manual`,
`unavailable`); reminders keep the composer used (`model:<provider>` or
`template`) and the fallback reason. Demographics never enter the audit chain.
