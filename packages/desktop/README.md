# OpenAdapt Desktop

[![Tests](https://github.com/OpenAdaptAI/openadapt-desktop/actions/workflows/test.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-desktop/actions/workflows/test.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Role: installed authoring and operation companion.** OpenAdapt Desktop is
> the local authoring and teaching cockpit for OpenAdapt. The canonical compiler
> and governed runtime live in
> [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow). This
> repository publishes release candidates. An exact release becomes the active
> Production default only while the canonical signed lifecycle ledger has a
> current admission for its exact artifacts. An absent, inactive, expired, or
> revoked admission means that the release is not actively admitted. The latest
> published native prerelease is historical and carries its original
> ad-hoc/unsigned labels. The next native release is blocked until macOS has
> Developer ID plus notarization, Windows has Authenticode, and the exact Linux
> bytes pass GitHub OIDC attestation verification.

## What OpenAdapt is

OpenAdapt is a governed demonstration compiler. You record a workflow once, it
compiles the demonstration into a deterministic program, and it replays that
program with **zero model calls on the healthy path**. When an interface drifts,
OpenAdapt re-resolves from retained evidence or proposes a governed repair, and
it **halts instead of guessing** when verification fails.

Substrates are all first-class in the product design (web, Windows, macOS,
Linux, RDP, and Citrix/VDI).

Substrate roles and qualification evidence:

| Substrate | Role | Evidence boundary |
| --- | --- | --- |
| Browser (web) | Managed browser recording and execution | Qualification is task- and environment-specific; the signed lifecycle ledger selects the active Production default |
| Native desktop (Windows, macOS, Linux) | Customer-controlled native recording and execution | Qualification is task- and environment-specific |
| Remote display (RDP) | Customer-controlled remote-display recording and execution | Qualification is task- and environment-specific |
| Citrix / VDI | Customer-controlled remote-application recording and execution | ICA/HDX qualification is deployment-specific |

The compiler, replayer, certification, and governed repair all live in
`openadapt-flow`. This desktop repository is the cockpit and the local wiring
around that engine, not a second copy of it.

## What this repository is

OpenAdapt Desktop is two cooperating processes:

- A **Tauri/React cockpit** (the shell and UI).
- A **Python engine sidecar** it drives over a JSON-lines stdin/stdout IPC.

The engine owns consent, operating-system permissions, recording and review,
hosted authentication and push, and a `FlowBridge` that runs the exact
`openadapt-flow` version embedded in the signed sidecar for compile, replay,
run, and teach. The shell also runs a
token-authenticated loopback socket server so the separately developed
[`openadapt-tray`](https://github.com/OpenAdaptAI/openadapt-tray) companion can
mirror status and send local commands.

The native CI matrix freezes the canonical Flow runtime into the engine,
performs a real browser record -> compile -> replay lifecycle on every target
operating system, then installs, launches, and uninstalls each package. Apple
Developer ID/notarization and Windows Authenticode remain credential-gated.

### The cockpit

Continue locally without an account, or connect an optional Cloud workspace.
The shell then renders a left-rail cockpit over the live engine:

| Screen | What it does |
| --- | --- |
| Start | Enter the complete local cockpit without an account, or connect Cloud with system-browser PKCE or a one-time token stored in the OS keychain |
| Onboarding | First-run guidance until a workflow exists locally |
| Workflows | The workflow library: recorded and compiled workflows, with their status |
| Record & review | Start/stop a capture and step through the local review gate before anything leaves the machine |
| Runner (watch it run) | Choose Browser, Windows, macOS, Linux, RDP, or Citrix; supply only that target's connection details; trigger a replay or governed run; and watch the live rail, step log, and halt evidence |
| Teach | Resolve a halted step and write a governed repair back toward the workflow |
| Settings | Host, deployment lane, credentials, and local preferences |

The rail carries two orthogonal status channels (recording and sync) plus the
needs-attention break count, mirrored from the engine over events. In a plain
dev checkout the shell renders an engine-offline state, because the frozen
sidecar binary is built only in CI.

### Attended decisions on a phone

When a governed run needs a person, Desktop can pair a phone with a one-use QR
code and a matching code. The phone receives one signed task, the evidence that
the customer-controlled runner is allowed to show, and only the actions that
Flow permits for that pause.

<p align="center">
  <img src="docs/assets/mobile-decision/request.png" width="260" alt="OpenAdapt Desktop phone portal showing an identity check, a retained synthetic OpenEMR frame, and four permitted actions">
</p>

This screen is a request. A tap does not approve a stale screen and does not
prove a business effect by itself. The runner reacquires the live application,
repeats the required state, identity, and target checks, and then returns a
typed result. A refusal leaves the run paused. The detailed portal guide shows
the corresponding result screen.

The local portal can serve an approved retained raster frame through the
customer-operated HTTPS origin, published by a reverse proxy or VPN. The
hosted outbound lane carries a signed,
remote-safe task without pixels. Device pairing authenticates the device
session; it does not authenticate the named operator principal that a
qualification policy can require for decision attribution.

The screenshot above comes from the exact Desktop phone shell with synthetic
data. See its [capture provenance](docs/assets/mobile-decision/provenance.json)
and the complete [mobile decision portal contract](docs/DECISION_PORTAL.md),
including pairing, evidence handling, optional-step decisions, reconciliation,
teaching, escalation, and terminal receipts.

## Status

| Area | Checked-out implementation | Evidence and admission state |
| --- | --- | --- |
| Python capture CLI | Record, list, inspect, scrub, review, approve, local storage, health, and cleanup commands | Covered by tests; native capture comes from the canonical `openadapt-capture` component |
| Local review gate | Persisted states and egress checks for the capture pipeline | Separate from the `openadapt-flow` certification system |
| Tauri/React cockpit | Login, onboarding, workflows, target-aware record/review/replay/governed run, teach, and settings calling the engine through Tauri commands | Browser and customer-controlled native/remote targets are available as scoped above; the shell renders an engine-offline state when the sidecar binary is absent |
| Rust commands | Generic `engine_invoke` bridge plus typed commands, sidecar spawn/watchdog/shutdown, and event re-emission to the WebView | Compiled and bundled in CI |
| Python sidecar IPC | JSON-lines handler backed by a shared `EngineDispatcher` (recording, compile/replay/run/teach, auth, sync/push, review, config) | Unit and end-to-end tests use mocked external boundaries |
| Tray IPC socket server | Token-authenticated loopback TCP server plus a `~/.openadapt/desktop_ipc.json` discovery file for `openadapt-tray` | Desktop and the shipped tray are not yet validated together end to end |
| Desktop-to-flow handoff | `FlowBridge` launches the pinned Flow runtime embedded in the frozen sidecar as an isolated subprocess | Self-contained; no separate Python or Flow installation |
| Hosted auth and governed handoff | Browser-PKCE and paste-token sign-in; host-bound keychain credentials; exact `openadapt.push-result/v1` review, accepted-ingest, and uncertain-delivery state; local handoff retention; and halted-run break reports | Distribution requires a release-qualified Flow build and live Cloud acceptance before Desktop updates its exact runtime pin |
| Attended phone decisions | One-use QR pairing, protected local evidence, typed allowed actions, runner revalidation, receipts, device revocation, and an optional outbound hosted lane | Device pairing does not replace the deployment's authenticated operator principal |
| Build artifacts | Wheel/sdist, a self-contained PyInstaller engine+Flow runtime, and DMG/MSI/NSIS/DEB/AppImage native jobs | Native jobs prove the frozen browser lifecycle, structurally install/uninstall, and label every platform, architecture, and signing state |
| Native installers | Distinct `desktop-v*` prerelease workflow with final-byte checksums and GitHub provenance | Unadmitted release-candidate lane; signing state is encoded in every filename and workflow qualification remains specific |
| Code signing and updater | Apple Developer ID/notarization and Windows Authenticode are credential-gated and fail closed on partial configuration; the updater feed is disabled | Candidate publication requires the complete platform trust set; the updater is outside the current channel |

CI builds the self-contained `openadapt-engine` freeze. Candidate publication
requires the external code-signing and notarization controls. Production
selection also requires an active central admission for the exact artifacts.

## Use OpenAdapt today

The canonical first run uses the
[OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt) launcher:

```bash
pip install 'openadapt[browser]'

openadapt quickstart
```

On Windows `cmd.exe`, use double quotes: `pip install "openadapt[browser]"`.

`openadapt quickstart` and the engine-native `openadapt-flow tutorial` run the
same loop. To drive the stages by hand against the engine directly:

```bash
pip install 'openadapt-flow[browser]'

openadapt-flow demo-record --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow lint bundle                                # expected to find demo gaps
openadapt-flow certify bundle --policy permissive        # smoke-policy pass
openadapt-flow certify bundle --policy clinical-write    # expected strict refusal
openadapt-flow replay bundle
openadapt-flow replay bundle --drift theme --save-healed-to healed
```

The bundled tutorial is runnable but intentionally not certified for clinical
writes. A nonzero strict-certification result is the expected refusal boundary,
not a setup failure.

## Build and run this repository

### Engine and cockpit from source

The Python engine and its CLI run from a plain checkout. Source development
resolves Flow from the locked build extra; native packages embed the exact
runtime and never depend on a system Python or `PATH` executable.

```bash
git clone https://github.com/OpenAdaptAI/openadapt-desktop.git
cd openadapt-desktop
uv sync --extra dev --extra build

uv run openadapt-desktop doctor
uv run openadapt-desktop list
uv run openadapt-desktop storage

uv run pytest tests -q
uv run ruff check engine tests
```

Recording requires the operating-system permissions and runtime support used by
[`openadapt-capture`](https://github.com/OpenAdaptAI/openadapt-capture):

```bash
uv run openadapt-desktop record --task "Inspect capture path"
```

To work on the shell and frontend you also need Rust, Node.js, and the Tauri
CLI. A dev shell runs frontend-only and shows the engine as offline until a
frozen sidecar binary from CI is present.

### Native installer release candidates

Native packages are published under a distinct `desktop-vX.Y.Z` prerelease
channel, separate from the engine's `vX.Y.Z` PyPI/GitHub releases. The native
version is synchronized to each engine release by CI, so a native prerelease
mirrors the engine version it was built from. A native prerelease is packaging
evidence, and it is not a separate supported desktop release. Historical
prereleases retain their original trust labels. New release filenames require
`developer-id-notarized` on macOS, `authenticode` on Windows, and
`github-attested` for the exact Linux bytes. CI installs, launches, and
uninstalls each package on clean runners. Packaging structure is not workflow
qualification.

On macOS, regular ad-hoc CI uses an explicit non-hardened overlay because an
identity-less hardened launcher cannot load PyInstaller's identity-less embedded
libraries. Developer ID builds keep hardened runtime and pass the same Apple
identity into PyInstaller and Tauri. The installed-app smoke executes bundled
Flow after the final signing pass, so a structurally valid but unloadable app
cannot be released.

Third-party licenses and notices for the native runtime are embedded beside
the components they cover and verified against the actual frozen archive. The
pinned sources, hashes, and modification status are recorded in
[`third_party/README.md`](third_party/README.md).

- Which release to download, and the two-lane policy, are in
  [RELEASES.md](RELEASES.md).
- Artifact names, verification scope, and provenance are in
  [Native Release Candidates](docs/RELEASE_CANDIDATE_INSTALLERS.md).
- The signing activation runbook (what to buy, which secrets to add, and what
  each surface may then truthfully claim) is in
  [docs/CODE_SIGNING.md](docs/CODE_SIGNING.md).

The legacy `upload --backend hosted_ingest` command is a compatibility alias
for the supported governed `push` path. It does not call the old direct ingest
adapter. Customer-owned storage upload is paused until it uses Flow's complete
inventory, image-capable sanitization, and exact in-app review contract.

## Architecture

The boundary is deliberately narrow:

```text
Desktop authoring/teaching cockpit (Tauri + React)
        |
        | local IPC (JSON lines over sidecar stdio; token-authenticated
        | loopback socket for the tray)
        v
Frozen Python engine sidecar (capture, review, auth, sync, FlowBridge,
                              pinned openadapt-flow runtime)
        |
        | isolated subprocess mode in the same signed executable
        v
openadapt-flow
  record -> compile -> lint/certify -> replay -> halt/repair/teach
        |
        +-> optional hosted control-plane metadata and break reporting
```

The desktop application owns consent, operating-system permissions, recording
controls, inspection, and human teaching. It does not duplicate the workflow
compiler or runtime. Those remain in `openadapt-flow`.

## Known gaps

- Desktop starts without downloading a browser. The first browser workflow
  downloads the Chromium revision pinned by the
  bundled Playwright runtime into `~/.openadapt/browser-runtime`. The app shows
  setup progress and a retryable failure; no workflow action starts until the
  browser is ready. Air-gapped packages set `PLAYWRIGHT_BROWSERS_PATH` to a
  version-matched prebundle. Native desktop, RDP, and Citrix workflows never
  invoke that setup path.
- CI proves the frozen binary's browser record -> compile -> replay loop on
  Windows, macOS, and Linux. UI event contracts are automated, while broader
  real-application qualification remains workflow-specific.
- The frozen `openadapt-engine` sidecar binary is produced only by CI. A plain
  dev checkout runs the shell in frontend-only mode.
- Native packages remain unadmitted release candidates until the central
  Production lifecycle activates an exact release. The latest published
  prerelease predates the mandatory platform trust gate. Structural
  install/uninstall success is not evidence of a validated workflow.
- Apple Developer ID/notarization and Windows Authenticode credentials must be
  provisioned before the next native release. A missing or partial set stops
  the release. The updater and rollback remain disabled pending an independent
  signing-key lifecycle.
- This repository serves the tray's loopback IPC contract, but the desktop and
  the shipped tray client have not been validated together end to end.

## CLI surface

The Python engine exposes these commands:

| Command | Purpose |
| --- | --- |
| `openadapt-desktop record` | Capture a local session |
| `openadapt-desktop list` / `info` | Inspect capture metadata |
| `openadapt-desktop scrub` | Run configured PII scrubbing |
| `openadapt-desktop review` / `approve` / `dismiss` | Operate the local review state machine; dismissal keeps raw data local |
| `openadapt-desktop compile` / `replay` / `run` | Invoke the bundled, pinned `openadapt-flow` runtime on a capture or bundle |
| `openadapt-desktop login` / `push` / `report-break` | Authenticate to the hosted control plane, push a bundle, report a halted run |
| `openadapt-desktop storage` / `health` / `cleanup` | Inspect and maintain local storage |
| `openadapt-desktop backends` / `upload` | Inspect legacy customer-owned storage adapters; hosted uses governed `push`, and customer-owned upload remains paused behind a fail-closed release gate |
| `openadapt-desktop config` / `doctor` | Inspect local configuration and dependencies |

Raw recordings are local by default. Any egress path still requires careful
review of the selected adapter, configuration, logs, and data-classification
policy. This repository does not by itself establish a HIPAA-compliant or
production-safe deployment.

The governed `push` implementation delegates to Flow's exact-hash sanitized
derivative contract. It consumes the closed `openadapt.push-result/v1` schema
and retains the exact review or ingest handoff locally. A recording acceptance
requires the server-owned `artifact_ingest_id` and a governed next action. A
bundle acceptance additionally requires the server-owned workflow identity,
the runtime-attestation binding, and the exact trusted dashboard path. An
unknown child or delivery outcome requires reconciliation and never becomes an
automatic retry. The command never falls back to a direct Desktop upload when
Flow is missing or returns an error. The former direct hosted-ingest backend
now refuses every upload.

This path does not enter a native release until the exact pinned Flow artifact
and the managed Cloud runtime pass the same live acceptance contract. The legacy
customer-owned adapter queue remains paused for this release; its exit
condition is a Flow-owned complete inventory, image-capable scrub, and exact
in-app review. The dormant queue also selects the reviewed scrubbed path again
immediately before egress; a dismissed raw capture is not uploadable.

## Development

Prerequisites are Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). The main
implementation areas are:

```text
engine/       Python capture, review, auth, sync, and FlowBridge code
src-tauri/    Rust/Tauri shell, sidecar lifecycle, and tray socket wiring
src/          React cockpit (screens, engine client, primitives)
tests/        Python unit and end-to-end tests, largely with mocked boundaries
```

See [DESIGN.md](DESIGN.md) as a historical design reference. Where it conflicts
with this status section or with `openadapt-flow`, this README describes the
current public product boundary.

## Related projects

| Project | Lifecycle and role |
| --- | --- |
| [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow) | Canonical workflow compiler, runtime, certification, and governed repair engine |
| [`OpenAdapt`](https://github.com/OpenAdaptAI/OpenAdapt) | Flagship launcher and meta-repository |
| [`openadapt-tray`](https://github.com/OpenAdaptAI/openadapt-tray) | System-tray status and launcher companion for this cockpit |
| [`openadapt-capture`](https://github.com/OpenAdaptAI/openadapt-capture) | Canonical native screen, mouse, keyboard, timing, window-scoping, and media-capture component |
| [`openadapt-privacy`](https://github.com/OpenAdaptAI/openadapt-privacy) | PII detection and redaction component |

Documentation for the wider stack lives at
[docs.openadapt.ai](https://docs.openadapt.ai).

## License

MIT. See [LICENSE](LICENSE).
