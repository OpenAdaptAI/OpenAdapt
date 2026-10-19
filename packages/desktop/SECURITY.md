# Security Policy

OpenAdapt Desktop stores hosted credentials in the operating-system keychain,
starts a local compiler/runtime sidecar, and packages native installers. Please
report suspected vulnerabilities privately so they can be investigated before
public disclosure.

## Report privately

Use GitHub's private vulnerability reporting from this repository's
**Security → Advisories → Report a vulnerability** page. If that channel is
unavailable, email **hello@openadapt.ai** with “Security” in the subject. Do not
open a public issue, discussion, or pull request containing vulnerability
details.

Please include the affected version and platform, impact, reproduction steps,
and any suggested remediation when available.

## What to expect

- We aim to acknowledge a report within 5 business days.
- We will determine affected versions and keep the reporter informed of the
  remediation plan.
- Fixes ship forward on the latest Beta release; there is no long-term-support
  branch today.
- We will credit reporters who want recognition after a fix is released.

Credential exposure, keychain-boundary failures, unsafe protocol handling,
sidecar command or path injection, installer provenance/signing mismatches, and
release supply-chain findings are explicitly in scope.
