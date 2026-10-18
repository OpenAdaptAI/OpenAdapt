# Code signing policy

Free code signing for the OpenAdapt Desktop Windows installers is provided by
[SignPath.io](https://about.signpath.io/), with a certificate from the
[SignPath Foundation](https://signpath.org/). Signing starts with the first
Windows release built after the project is accepted; earlier installers are
unsigned.

## What is signed

Only OpenAdapt's own release builds: the OpenAdapt Desktop installers (MSI and
NSIS) and the executables inside them, built from this repository's source by
GitHub Actions. Each signing request is approved by hand for that release.
Nothing built on a developer's machine is signed. Third-party components keep
their upstream signatures and licenses (see
[third-party notices](https://github.com/OpenAdaptAI/openadapt-desktop/tree/main/third_party)).

macOS builds are not covered by this policy. They are ad-hoc signed and not
notarized.

## Team roles

| Role | Who |
| --- | --- |
| Committers and reviewers | [Richard Abrich](https://github.com/abrichr) |
| Approvers | [Richard Abrich](https://github.com/abrichr) |

Every change reaches `main` through a reviewed pull request, including changes
written with AI coding tools. All team members use multi-factor authentication
on GitHub and SignPath.

## Privacy

This program will not transfer any information to other networked systems
unless specifically requested by the user or the person installing or
operating it. Healthy runs make no model calls and send nothing out. Data
leaves the computer only for features a person turns on, such as model
grounding or the OpenAdapt Cloud dashboard. See the
[privacy policy](https://openadapt.ai/privacy-policy) and
[Security & procurement](https://docs.openadapt.ai/security/).

## Reporting a problem

Report a signed binary that you believe is malicious or mis-signed to
GitHub private vulnerability reporting ([SECURITY.md](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/SECURITY.md)) or to [hello@openadapt.ai](mailto:hello@openadapt.ai).
