# Compliance control mapping

Evidence column names the artifact an auditor can inspect. This mapping
supports, and does not replace, the deploying organisation's own risk
analysis and control testing.

| Control objective | HIPAA (45 CFR 164) | HITRUST CSF v11 | SOC 2 (TSC 2017) | ISO/IEC 27001:2022 | NIST SP 800-53 r5 | NIST AI RMF 1.0 | Evidence |
|---|---|---|---|---|---|---|---|
| Unique identification of people and agents | 164.312(a)(2)(i), 164.312(d) | 01.q | CC6.1 | 5.16, 5.17 | IA-2, IA-9 | GOVERN 1.4 | OIDC/fabric verification (`security/authn.py`), audit `actor` |
| Minimum necessary use and disclosure | 164.502(b), 164.514(d) | — | C1.1 | 5.12, 8.11 | AC-3, AC-21 | MAP 4.1 | `application/views.py` projections; tests T13 |
| Access control by role and patient | 164.308(a)(4), 164.312(a)(1) | 01.c, 01.v | CC6.3 | 5.15, 8.3 | AC-3, AC-6 | MANAGE 2.4 | `policies/patient-access.yaml`, policy matrix tests |
| Human oversight of consequential actions | — | — | CC5.2 | 5.2 | AC-5 | GOVERN 1.3, MANAGE 2.4 | `require_approval` rules, requester ≠ approver, escalation queues |
| Audit controls | 164.312(b) | 09.aa | CC7.2 | 8.15 | AU-2, AU-9, AU-10 | MEASURE 2.8 | Signed hash-chained audit; WORM bucket; `verify_chain` |
| Integrity of ePHI | 164.312(c)(1) | — | PI1.4 | 8.3 | SI-7 | — | Optimistic concurrency, `If-Match`, audit before persist |
| Transmission security | 164.312(e)(1) | 09.m | CC6.7 | 8.24 | SC-8, SC-13 | — | HTTPS to FHIR/clearinghouse/fabric/Vertex; TLS-only Cloud SQL |
| Encryption at rest and key management | 164.312(a)(2)(iv) | 06.d | CC6.1 | 8.24 | SC-12, SC-28 | — | CMEK in Terraform; secrets in Secret Manager |
| Contingency and availability | 164.308(a)(7) | 12.c | A1.2 | 8.14 | CP-9, CP-10 | — | Regional Cloud SQL, PITR, PDB, DR runbook |
| Security incident procedures | 164.308(a)(6) | 11.a | CC7.3 | 5.24 | IR-4, SI-4 | MANAGE 4.1 | Alert rules, incident runbook |
| Change management of rules and policy | 164.308(a)(8) | 10.k | CC8.1 | 8.32 | CM-3 | GOVERN 1.4 | Git history, CODEOWNERS, rules/policy versions in audit |
| AI model risk: data minimisation and output control | 164.502(b) | — | CC3.2 | 5.34 | SI-10, SR-3 | MAP 2.3, MEASURE 2.6, MANAGE 2.2 | No PHI to model; ContentGuard; template validation; composer recorded per reminder |
| Third-party (business associate) management | 164.308(b)(1) | 05.k | CC9.2 | 5.19 | SA-9 | GOVERN 6.1 | Clearinghouse, messaging and cloud vendors under BAA (deployment prerequisite) |

## Transactions and code sets

Eligibility uses ASC X12 005010X279A1 (270/271), the HIPAA-adopted standard
for eligibility inquiries (45 CFR 162.1202). CAQH CORE operating rules
(connectivity, response time) are the clearinghouse's obligations; this
service consumes the synchronous response and treats timeouts as
"unavailable", never as a coverage decision.

## GxP

Not applicable: the service performs administrative scheduling and
eligibility, not GxP-regulated activities.
