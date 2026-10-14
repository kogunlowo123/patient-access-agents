# Security Policy

## Supported versions

Security fixes are released for the latest minor version.

## Reporting a vulnerability

Do not open a public issue. Use GitHub private vulnerability reporting
(Security tab, "Report a vulnerability"). Include affected version, a
description of the issue, reproduction steps and impact. Never include real
patient data in a report; reproduce with synthetic data.

Expected handling:

| Stage | Target |
|---|---|
| Acknowledgement | 3 business days |
| Triage and severity (CVSS v4) | 10 business days |
| Fix for critical/high | 30 days from triage |
| Coordinated disclosure | After a fixed release is available |

## Scope

In scope: code in this repository, including authentication and
authorization, patient-data projections, the X12 builder and parser, the FHIR
client, the MCP and A2A interfaces, and model output handling.
