# ADR-0002: Agents propose; patients and staff decide

- Status: Accepted
- Date: 2026-10-14

## Context

Booking, denying or cancelling care changes what happens to a patient.
Agents are good at the mechanical path (look up, check, search, rank) and
poor at owning consequences. Regulators and patients expect a person to be
accountable for access decisions.

## Decision

- Authorization goes through the agentic-core PolicyEngine with
  `policies/patient-access.yaml`. An agent's `appointment:book` returns
  `require_approval`; the booking waits in `booking_pending_approval` and an
  escalation goes to the scheduling queue.
- A booking may be approved only by a person other than the requester
  (segregation of duties). Prior-authorization holds can be cleared only by
  eligibility staff or supervisors.
- Eligibility overrides, case closure, approvals and queue work are denied
  to agents outright, whatever their scopes.
- Nothing is auto-denied. Coverage problems become work items.
- For agents, the identity fabric decision is also obtained; the most
  restrictive of the two decisions applies.

## Consequences

- Throughput for agent-initiated bookings depends on queue staffing; a
  patient booking through the portal is immediate.
- Every approval leaves an auditable pair of identities (requester and
  approver) on the appointment record.
