# macOS 27.3 permission guidance — 2026-11-20

This is a source-only onboarding/diagnostic correction, not beta runtime
qualification. The new PR is held for review and must remain unmerged.

The defect class is using a legacy global-permission label/check as a claim
about newer app-scoped control. The launcher, engine, recorder and Desktop keep
independent installation boundaries; their small dependency-free guidance
helpers share one tested numeric-version/setup model. No package floor, release
version, native entitlement or workflow is changed.

## Validation

| Targeted group | Result |
| --- | --- |
| Launcher guidance and doctor | 53 passed |
| Flow macOS wrappers and refusal/prompt cases | 63 passed |
| Capture input permission cases | 8 passed, 17 deselected |
| Desktop capability and API-result cases | 55 passed |
| Desktop permission dispatch cases | 10 passed, 92 deselected |
| Onboarding UI | 6 passed |
| Desktop TypeScript | Passed, no emit |

Python checks used CPython 3.12.13 with source paths bound to this checkout.
Desktop test additions used task-local dependencies, including Pydantic2.14.4;
they are not a new locked-runtime qualification. Capture used its existing
complete 3.12.13 test interpreter. UI used Node22.22.2 and the exact npm lock.
All tests ran headlessly through the shared CPU-slot wrapper, sequential package
processes for Python. No native permission API, desktop input or GUI was driven.

Initial collection failures were environmental: the first Capture interpreter
lacked Flow cryptography; combining the packages collided on their separate
`tests` namespaces; the Flow interpreter lacked Capture's fire dependency.
Each package was then tested separately in its appropriate existing interpreter.
A local path typo was corrected before final formatting/type validation. These
setup failures are not test passes or source defects. The source-growth check
first found dispatch one line over its existing budget; moving the pure guidance
import to module scope removed the extra line without raising the budget. The final Capture group
retains its existing SQLAlchemy deprecation warning; Vite retains its config
loader warning.

## Remaining beta checks

No macOS27.3 machine was identified. Test the exact new-pane deep link, whether
AX process trust reflects each override mode, UI reads versus posted input,
Choose Apps with allowed/disallowed targets, Ask First cancellation, and old
migrated grants on a beta machine before merging. The code does not invent a
replacement API or bypass a denied preflight. The UI/doctor never treat process
trust as proof of all target-app grants. General-availability timing is unknown.

Primary references and beta2 known-issue identifiers are in
[the permission guide](../getting-started/permissions.md).
