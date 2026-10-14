# Runbook: first deployment and startup

## Prerequisites

- GKE (reference) or EKS/AKS cluster ≥ 1.27 with Workload Identity, External
  Secrets Operator and a `ClusterSecretStore` named `platform-secrets` for
  the cloud secret manager.
- A FHIR R4 endpoint for the EHR (or Google Cloud Healthcare API FHIR store)
  supporting search on `Slot` (`status`, `specialty`, `start`, `_include`),
  versioned updates and conditional create.
- An OIDC client for the patient portal and the access-centre console that
  issues the `patient` (or `fhirUser`) claim for patients and a roles claim
  with `access-scheduler`, `eligibility-specialist`, `care-navigator`,
  `access-supervisor`, `access-auditor` for staff.
- Business associate agreements with the clearinghouse, messaging provider
  and cloud provider.
- Optional: agent-identity-fabric, with this service registered as an agent
  (`spiffe://<domain>/agent/access/patient-access-agents`) holding the FHIR
  scopes it needs, and each calling agent holding scopes such as
  `case:read case:create eligibility:verify slots:search appointment:book`.

## 1. Provision cloud resources

```bash
cd deployments/terraform/gcp        # or aws / azure
terraform init                      # configure an encrypted remote backend first
terraform apply -var-file=<environment>.tfvars
```

Record the outputs: service account for Workload Identity, database URL
secret, audit bucket, integration secret names.

## 2. Create the audit signing key and integration secrets

Keys are created outside Terraform so they never enter state:

```bash
uv run python -c "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K; \
from cryptography.hazmat.primitives import serialization as s; \
print(K.generate().private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()).decode())" \
  | gcloud secrets create patient-access-agents-audit-key-2026-10 --data-file=-

printf '%s' "<clearinghouse API key>" | gcloud secrets versions add patient-access-agents-clearinghouse-api-key --data-file=-
openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add patient-access-agents-notification-signing-key --data-file=-
```

Share the notification signing key with the messaging service owner; it
verifies `X-Signature-256` on every request.

## 3. Namespace and secret sync

```bash
kubectl apply -k deployments/kubernetes
kubectl -n patient-access get externalsecret   # all READY=True
```

## 4. Install the chart

Create `<environment>.yaml` with every value marked REQUIRED in
`values.yaml` plus the integrations you use, and the Workload Identity
annotation from Terraform:

```bash
helm upgrade --install paa oci://ghcr.io/kogunlowo123/charts/patient-access-agents \
  --version 0.1.0 -n patient-access -f <environment>.yaml --wait
```

## 5. Verify

```bash
kubectl -n patient-access rollout status statefulset/paa-patient-access-agents
kubectl -n patient-access port-forward svc/paa-patient-access-agents 8080:80
curl -sf localhost:8080/readyz
curl -s localhost:8080/.well-known/agent-card.json | jq '.skills[].id'
```

Then, with a synthetic test patient in a non-production FHIR store, run one
referral end to end as a scheduler (`POST /v1/referrals/process`) and confirm
the case reaches `slots_proposed`, the audit chain grows, and the Grafana
dashboard shows the transitions.
