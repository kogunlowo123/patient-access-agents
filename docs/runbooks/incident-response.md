# Runbook: incident response

## Elevated 5xx

1. `kubectl -n patient-access get pods`; check `/readyz` per pod.
2. Readiness failing: database. Check Cloud SQL status and failover events.
3. 503 with `temporarily_unavailable`: an upstream (FHIR, clearinghouse,
   identity fabric) is down. The service is failing closed as designed;
   follow the upstream's incident process.
4. Pods crash-looping at start: configuration. The service refuses to start
   on incomplete settings or a bad `providers.yaml`; `kubectl logs` names
   the setting.
5. If a release caused it, follow the rollback runbook.

## Clearinghouse outage

Alert `PatientAccessEligibilityUnavailable`. Cases move to
`eligibility_pending` with retries (15 min doubling to 4 h, 5 attempts),
then escalate to the eligibility queue. Nothing is denied.

1. Confirm with the clearinghouse; note the incident reference.
2. If the outage will outlast the retry budget, tell the eligibility team to
   verify urgent and stat referrals by payer portal or phone and record them
   with `POST /v1/cases/{id}/eligibility/manual`.
3. After recovery, retried cases continue automatically; escalated ones are
   re-verified by staff from the queue.

## Messaging outage

Alert `PatientAccessReminderFailures`. Deliveries retry with back-off outside
quiet hours; exhausted reminders escalate to care navigation, who call the
patient. Portal messages are unaffected.

## Worker failures

Alert `PatientAccessWorkerFailing`. Logs show `work_cycle_failed` with the
exception. Typical causes: database connectivity or an unexpected upstream
response. Individual reminders that fail are released back to the queue;
check for a reminder that fails repeatedly and inspect its case.

## Deny spike

A jump in denials right after a policy change is expected to be the policy
change. Compare `policy_digest` in recent audit events with the previous
release and revert the policy commit if unintended. Otherwise look for
probing: many 404s for one patient-context subject indicate attempted access
to other patients' cases; suspend the account at the IdP and raise a privacy
incident.

## Model output rejections

Alert `PatientAccessModelFallbackSpike`. Patients still receive reviewed
templates. Check `reason` labels: `output_pii` or `output_guard` suggests the
model is inventing content; `invalid_placeholders` usually follows a model
version change. Disable drafting (`models.enabled=false`) if needed; no
other behaviour changes.

## Suspected compromise of an agent

Suspend the agent in agent-identity-fabric (all its credentials stop
validating immediately). Agent bookings were held for approval, so review
`booking_pending_approval` cases requested by that agent and decline any
not confirmed with the patient. Pull its audit trail by `actor`.

## Replicas down

Fewer than two replicas scraped: check node pressure and PDB status. One
replica keeps serving; background work continues with leases.

## PHI exposure

Treat as a potential breach under HIPAA 45 CFR 164.400-414: preserve audit
chains and logs, engage the privacy officer, and determine scope with the
audit trail (`actor`, `resource`, `correlation_id`).
