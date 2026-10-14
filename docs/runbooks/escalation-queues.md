# Runbook: working the escalation queues

Escalations are the agents' hand-off to people. Each has a reason, a queue,
a priority derived from the referral priority (SLA breaches are raised one
level), and stays open until a person resolves it. One open escalation per
case and reason; repeats are de-duplicated.

| Queue | Owning role | Reasons |
|---|---|---|
| `eligibility` | `eligibility-specialist` | `eligibility_denied`, `eligibility_unavailable`, `manual_verification`, `prior_authorization` |
| `scheduling` | `access-scheduler` | `no_capacity`, `sla_at_risk`, `booking_approval` |
| `care-navigation` | `care-navigator` | `availability_conflict`, `unroutable_referral`, `reminder_failed` |
| `supervisor` | `access-supervisor` | `sla_breach` |

Supervisors may claim and resolve items in every queue.

## Working an item

```bash
GET  /v1/escalations?queue=<queue>&status=open
POST /v1/escalations/{id}/claim
POST /v1/escalations/{id}/resolve   {"resolution": "<what was done>"}
```

Many escalations resolve themselves when the underlying problem is fixed
(coverage verified, options found, booking approved, appointment booked);
the resolution names the action that fixed it.

## By reason

- **eligibility_denied**: read `rejection_code` and `follow_up_action` on the
  case. Correct the Coverage in the EHR and re-run eligibility, or verify
  with the payer and record `POST /v1/cases/{id}/eligibility/manual`. Never
  close a referral for coverage alone without the clinical team's agreement.
- **manual_verification / eligibility_unavailable**: verify with the payer
  and record the result with its reference.
- **prior_authorization**: obtain authorization; approve the pending booking
  (`POST /v1/cases/{id}/booking/decision`) as eligibility staff.
- **booking_approval**: confirm the option with the patient, then approve or
  decline. You cannot approve a booking you requested.
- **no_capacity**: the solver found nothing before the deadline. Ask the
  clinic to open capacity or widen preferences with the patient, then run
  slot search again.
- **availability_conflict**: the patient's windows exclude every option.
  Call the patient, update preferences, search again.
- **unroutable_referral**: route the case to a configured service
  (`POST /v1/cases/{id}/route`) or ask access operations to add the code to
  `configs/access-rules.yaml`.
- **reminder_failed**: call the patient and update their contact details in
  the EHR.

## SLA breach

`sla_breach` goes to supervisors with priority one level above the
referral's. Decide whether to add capacity, offer another location or
modality, or escalate clinically. Breaches are reported in the
`PatientAccessSlaBreaches` alert.

## Backlog

`PatientAccessQueueBacklog` fires above 50 open items in a queue for 30
minutes. Reassign staff, or check for a systemic cause (clearinghouse
outage, a clinic template not loaded) before working items one by one.
