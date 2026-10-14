# Threat model

## Assets

| Asset | Impact if compromised |
|---|---|
| Patient demographics and coverage (read from FHIR) | HIPAA breach, identity theft |
| Case data (referral codes, eligibility, appointments) | PHI disclosure |
| Booking capability (FHIR Slot/Appointment writes) | Denial of care, schedule disruption |
| Human work queues | Missed escalations, delayed care |
| Audit chains and audit key | Loss of accountability |
| Clearinghouse API key, webhook signing key | Fraudulent inquiries, spoofed patient messages |

## Trust boundaries

1. Patients, staff and agents → service API (bearer tokens).
2. Service → EHR FHIR API, clearinghouse, messaging service, Vertex AI.
3. Service → identity fabric and OIDC JWKS.
4. Service → PostgreSQL, Secret Manager, audit storage.
5. Model output → patient-facing messages (untrusted content).

## STRIDE

| ID | Threat | Category | Control | Test |
|---|---|---|---|---|
| T1 | Token signed with an attacker key | Spoofing | JWKS verification, kid pinning | `tests/security::test_t1_forged_token_signed_by_unknown_key` |
| T2 | `alg: none` / HMAC confusion | Spoofing | Asymmetric algorithms only | `test_t2_alg_none_and_hmac_confusion` |
| T3 | Claim tampering (switch patient context) | Tampering | Signature covers claims | `test_t3_tampered_claims` |
| T4 | Token for another app or issuer | Spoofing | Issuer and audience checks | `test_t4_wrong_audience_or_issuer` |
| T5 | Replay of expired token | Spoofing | `exp`/`iat` against injected clock, 30 s leeway | `test_t5_expired_token` |
| T6 | Patient reads or acts on another patient's case (IDOR) | Information disclosure | `eq_attr` policy on patient_id; denials concealed as 404; list forced to own patient | `test_t6_patient_cannot_reach_other_patients_data` |
| T7 | Agent books, approves or overrides on its own | Elevation | Policy `require_approval` for agent bookings, deny for approvals/overrides/closure; requester ≠ approver | `test_t7_agent_cannot_book_or_self_approve` |
| T8 | Agent exceeds its scopes | Elevation | Scope check per action; fabric decision combined (most restrictive) | `test_t8_scope_limits_agents` |
| T9 | X12 delimiter injection through FHIR demographics or identifiers | Tampering | Free text sanitised and truncated; identifiers with delimiters rejected and routed to manual verification | `test_t9_x12_injection_through_fhir_demographics`, `tests/unit/test_x12.py` |
| T10 | Audit tampering, PHI in audit | Repudiation / disclosure | Signed hash chain; demographics never audited | `test_t10_audit_chain_is_tamper_evident` |
| T11 | Prompt injection or harmful content in model drafts reaching patients | Tampering | Model sees no PHI; output guard; strict template validation; reviewed fallback | `test_t11_prompt_injection_cannot_reach_patients`, `tests/unit/test_messaging.py` |
| T12 | Oversized or malformed input, path traversal in ids | DoS / tampering | Pydantic limits, id patterns, paging caps, schemathesis fuzzing | `test_t12_oversized_and_malformed_inputs`, `tests/e2e/test_api_contract.py` |
| T13 | Over-disclosure to staff and agents | Information disclosure | Role-based projections (member id masked, benefits hidden) | `test_t13_minimum_necessary_projection` |
| T14 | Acknowledging or reading another patient's reminders | Information disclosure | Patient-bound policy, concealment | `test_t14_patient_cannot_acknowledge_or_read_others_messages` |
| T15 | Double booking through concurrent requests | Tampering | `If-Match` on Slot, conditional Appointment create, compensation | `tests/unit/test_workflow.py::test_stale_slot_is_detected_and_released` |
| T16 | Identity fabric outage lets agents through | Elevation | Fail closed (503); people unaffected | `tests/chaos::test_identity_fabric_outage_fails_closed` |
| T17 | Paging link redirects to attacker host (SSRF / data exfiltration) | Information disclosure | `next` links must stay on the configured FHIR origin | `tests/unit/test_integrations.py::test_paging_is_bounded_and_stays_on_server` |
| T18 | Spoofed messaging requests | Spoofing | HMAC-SHA256 body signature, idempotency key | `test_sms_reminders_use_the_messaging_webhook` |

## MITRE ATT&CK (Enterprise)

| Technique | Mitigation |
|---|---|
| T1078 Valid Accounts | Short-lived OIDC and fabric credentials, role-scoped policy, deny spike alert |
| T1528 Steal Application Access Token | Bearer tokens never logged; fabric credentials cached in memory only |
| T1530 Data from Cloud Storage | Private buckets, CMEK, retention lock; no public access |
| T1565.001 Stored Data Manipulation | Optimistic concurrency, signed audit chain |
| T1562.008 Disable Cloud Logs | Audit failure fails the operation; WORM storage |
| T1190 Exploit Public-Facing Application | Input validation, schemathesis fuzzing, Trivy/Grype/Semgrep in CI |

## MITRE ATLAS (AI-specific)

| Technique | Mitigation |
|---|---|
| AML.T0051 LLM Prompt Injection | Model input is operator-built JSON of non-identifying facts; injection detector on input; output treated as untrusted and validated |
| AML.T0054 LLM Jailbreak | Same: any non-conforming output falls back to a reviewed template |
| AML.T0057 LLM Data Leakage | No PHI in prompts; classification CONFIDENTIAL caps which providers may serve |
| AML.T0048 External Harms | Agents cannot book, cancel or deny on their own; people approve |

## OWASP Top 10 for LLM Applications (2025)

| Risk | Control |
|---|---|
| LLM01 Prompt Injection | ContentGuard input screening; untrusted output validation; templates as fallback |
| LLM02 Sensitive Information Disclosure | No identifiers sent to the model; output PII rejects the draft |
| LLM05 Improper Output Handling | Exact placeholder contract, no URLs/digit runs, length cap, local filling |
| LLM06 Excessive Agency | Agents limited to scopes; bookings need approval; human-only actions denied |
| LLM09 Misinformation | Model never states clinical content, coverage or times — those come from FHIR/X12 data |
| LLM10 Unbounded Consumption | `max_tokens=300`, provider timeouts, retries capped, circuit breakers, cost metric |

## OWASP API Security Top 10 (2023)

| Risk | Control |
|---|---|
| API1 Broken Object Level Authorization | Per-object policy with patient binding; 404 concealment |
| API2 Broken Authentication | JWKS verification, issuer/audience, injected-clock expiry |
| API3 Broken Object Property Level Authorization | `extra="forbid"` inputs; role-based output projection |
| API4 Unrestricted Resource Consumption | Paging limits, bounded FHIR paging, HPA, ingress rate limiting |
| API5 Broken Function Level Authorization | Distinct actions per role; human-only actions denied to agents |
| API6 Unrestricted Access to Sensitive Business Flows | Booking approval and segregation of duties |
| API7 Server Side Request Forgery | Fixed upstream URLs from configuration; paging origin check |
| API8 Security Misconfiguration | Production refuses ephemeral audit keys; Helm `required`; restricted Pod Security |
| API10 Unsafe Consumption of APIs | Strict FHIR and X12 parsing; envelope integrity; trace matching |

## Risk register

| ID | Risk | Likelihood | Impact | Owner | Treatment | Residual |
|---|---|---|---|---|---|---|
| R1 | Payer 271 misinterpreted (e.g. plan-level vs service-level coverage) | Medium | High | Revenue cycle | Conservative evaluation; ambiguous → manual; prior auth flags hold bookings | Medium |
| R2 | FHIR Slot data stale in the EHR | Medium | Medium | Scheduling ops | Re-read and `If-Match` at booking; conflicts reported, not hidden | Low |
| R3 | Escalation queues overwhelmed | Medium | High | Access supervisor | Backlog alert, SLA breach to supervisor, queue gauges | Medium |
| R4 | Clearinghouse outage | Medium | Medium | Platform | Retries with back-off, unavailability alert, manual path | Low |
| R5 | Model vendor change or outage | Low | Low | Platform | Templates are complete; drafting is optional | Low |
| R6 | Patient contact at night or to a stale number | Low | Medium | Patient experience | Quiet hours in patient time zone; failures escalate | Low |
| R7 | Policy misconfiguration widens access | Low | High | Privacy and security | CODEOWNERS on policies, digest in audit, policy matrix tests | Low |
