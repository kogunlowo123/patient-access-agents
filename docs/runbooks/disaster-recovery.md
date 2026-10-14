# Runbook: disaster recovery

| Objective | Target | Mechanism |
|---|---|---|
| RPO | ≤ 5 minutes | Cloud SQL point-in-time recovery |
| RTO | ≤ 1 hour | Terraform + Helm redeploy in the recovery region |

## State that matters

- **PostgreSQL**: cases, reminders, escalations, portal inbox. Restore with
  PITR.
- **EHR (FHIR)**: the system of record for referrals, slots and
  appointments. Not owned by this service.
- **Audit chains**: in the retention-locked bucket. Not needed to serve
  traffic.

Work lost to the RPO window is recoverable: cases are re-created idempotently
from their ServiceRequest (`POST /v1/referrals` returns the existing case or
creates it again), and bookings made in the window exist in the EHR as
Appointments with this service's identifier, which the next booking attempt
detects.

## Procedure

1. Restore Cloud SQL to the latest point before the incident; note the new
   endpoint.
2. Update the database URL secret if the endpoint changed.
3. `terraform apply` in the recovery region if the cluster region is lost.
4. `helm upgrade --install` with the same values.
5. Verify `/readyz`, list open escalations, and process one synthetic
   referral in a non-production FHIR store.
6. Re-run intake for ServiceRequests created during the RPO window (EHR
   query by `authored` date) to recreate any missing cases.
7. Record the audit-chain cutover (new chains start at sequence 0 on new
   volumes) in the incident ticket.

Recovery is exercised quarterly by restoring a PITR copy into a staging
namespace and running `tests/e2e` against it.
