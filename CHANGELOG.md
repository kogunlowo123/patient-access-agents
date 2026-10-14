# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.1.0] - 2026-10-14

### Added

- Referral intake from FHIR R4 ServiceRequest with routing to bookable services and
  priority-based SLAs.
- Eligibility verification: ASC X12 005010X279A1 270 builder and 271 parser (envelope
  integrity, AAA, TRN, EB, MSG, DTP, 999/TA1), clearinghouse transport, conservative
  interpretation, retries with back-off and manual verification.
- Constraint-based scheduling against FHIR Schedule/Slot: contiguous slot chaining,
  provider, location, modality, patient availability (time-zone aware), lead time and
  deadline, ranked options with binding-constraint reporting.
- Booking with optimistic concurrency (`If-Match`) and conditional Appointment create;
  agent bookings require human approval; segregation of duties.
- Follow-up reminders with quiet hours, retry and back-off; portal inbox and signed
  messaging webhook; Gemini drafting through agentic-core with PHI-free prompts and
  template fallback.
- Escalation queues with de-duplication, claim/resolve and SLA evaluation.
- Identity-aware access: patients see only their own data; role-based minimum-necessary
  projections; agent credentials via agent-identity-fabric or local JWKS.
- MCP tools (official SDK, streamable HTTP), A2A Agent Card and `message/send`, Google
  ADK adapter.
- Helm chart, Kubernetes namespace and secret sync, Terraform for GCP, AWS and Azure.
- CI: lint, types, SAST, contract drift, tests (unit, security, chaos, performance, e2e,
  PostgreSQL), Helm/kubeconform, Terraform validate, Trivy, Syft, Grype, Semgrep,
  container smoke test.
