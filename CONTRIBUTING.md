# Contributing

## Setup

```bash
uv sync --all-extras
make check
```

## Requirements for every change

- `make check` passes: ruff, black, mypy `--strict`, bandit, tests with at
  least 90% branch coverage.
- New behaviour has tests. Security controls need adversarial cases in
  `tests/security`.
- Public API changes are recorded in `CHANGELOG.md`.
- Architecturally significant decisions get an ADR in
  `docs/architecture/adr/`.
- No credentials, customer data or PHI in code, tests or fixtures. Use
  synthetic data only.

## Commit messages

Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
`chore:`). Breaking changes use `!` and a `BREAKING CHANGE:` footer.

## Review

Changes to `src/patient_access_agents/security`, `policies/`, `configs/` or `deployments/` need approval
from a member of the security code owners group (see `CODEOWNERS`).
