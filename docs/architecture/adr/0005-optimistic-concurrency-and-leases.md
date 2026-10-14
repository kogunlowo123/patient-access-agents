# ADR-0005: Optimistic concurrency for bookings and cases; leases for background work

- Status: Accepted
- Date: 2026-10-14

## Context

Two people, or a person and an agent, can act on the same slot or case at
the same time; several replicas run the same background work.

## Decision

- Booking re-reads each Slot, then updates it with `If-Match: W/"<version>"`
  and creates the Appointment with `If-None-Exist: identifier=<case>`. On any
  failure, slots already claimed are released; a retry after a partial
  failure finds the existing Appointment and completes.
- Cases carry a version column; saves are `UPDATE ... WHERE version = n`, and
  a stale write returns 409 instead of overwriting.
- Reminders are claimed with a time-limited lease (`lease_owner`,
  `lease_until`) so exactly one replica sends each one; escalations are
  de-duplicated by a unique key per case and reason while open.

## Consequences

- No distributed lock service is required; PostgreSQL provides the
  guarantees and SQLite behaves the same in tests.
- FHIR servers must support versioned updates (HAPI and the Google Cloud
  Healthcare API do); otherwise double-booking protection falls back to the
  scheduling system's own slot checks.
