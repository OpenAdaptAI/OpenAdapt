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

## Known limits

- **The approver root on macOS.** macOS builds are ad-hoc signed without the
  hardened runtime. A process running as the same user can run code inside
  Desktop's engine, so the login Keychain doesn't keep the approver root
  safe from it: such a process could replace the root and approve its own
  saves. On a computer where agents run as the person's account, have an
  administrator pin the root with `sudo openadapt-desktop approver-pin ROOT`.
  Desktop then requires that root, and refuses a Keychain root that differs.
  Windows and Linux use only the administrator's pin. See
  [APPROVAL_MODES.md](../flow/docs/APPROVAL_MODES.md#the-approver-key).
