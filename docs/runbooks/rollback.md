# Runbook: rollback

The schema is additive within a minor version (`schema_version` table), so
application rollback within 0.x.y does not require database changes.

```bash
helm -n patient-access history paa
helm -n patient-access rollback paa <revision> --wait
kubectl -n patient-access rollout status statefulset/paa-patient-access-agents
curl -sf "$PAA/readyz"
```

Policy or rules regressions: revert the commit touching
`policies/patient-access.yaml` or `configs/access-rules.yaml`, release, and
confirm that `policy_digest` and `rules_version` in new audit events match
the reverted files.

Model drafting regressions: set `models.enabled=false` and upgrade; patients
receive the reviewed templates.

Rollback is exercised in CI on every change: the chart renders and the
container starts from a clean state; every released image tag remains in
GHCR.
