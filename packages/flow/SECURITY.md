# Security Policy

openadapt-flow is often deployed next to sensitive systems (it can drive
clinical and other regulated desktop workflows, and its `privacy` extra scrubs
PHI/PII on the persist/log paths). We take security reports seriously.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub's built-in channel:

1. Go to the repository's **Security** tab → **Advisories** →
   **Report a vulnerability** ([direct link](https://github.com/OpenAdaptAI/openadapt-flow/security/advisories/new)).
2. Describe the issue, the impact, and a minimal reproduction if you have one.

This opens a private advisory visible only to you and the maintainers. If you
are unable to use that channel, open a public issue that contains **no
details** and asks a maintainer to open a private channel with you.

## What to expect

- We aim to acknowledge a report within **5 business days**.
- We will confirm the issue, determine affected versions, and prepare a fix.
- We will credit reporters who wish to be credited once a fix is released.

## Scope notes specific to this project

- **PHI/PII handling.** The compiled bundle and `report.json` intentionally
  retain literal identifiers (for the identity check and audit trail) and are
  protected by a documented boundary — see [docs/PRIVACY.md](docs/PRIVACY.md).
  A report that these are exposed *outside* that boundary is in scope.
- **Identity crops to the on-prem VLM appliance** are deliberately not scrubbed;
  the control there is on-prem-only + no-retention. Reports of retention or
  off-prem transmission are in scope.
- **Supply chain.** GitHub Actions are pinned by commit SHA and dependency
  updates flow through Dependabot; reports of a pinning gap are welcome.

## Supported versions

We support the latest released version on PyPI. Fixes are shipped forward; there
is no long-term-support branch at this stage.
