# OpenAdapt Desktop

[![Tests](https://github.com/OpenAdaptAI/openadapt-desktop/actions/workflows/test.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-desktop/actions/workflows/test.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Lifecycle: Beta supporting surface.** OpenAdapt Desktop is the
> local authoring and teaching cockpit for OpenAdapt. The canonical compiler and
> governed runtime live in
> [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow). This
> repository builds unsigned or ad-hoc-signed Beta prereleases; it is not
> yet a signed, generally available desktop product.

## What OpenAdapt is

OpenAdapt is a governed demonstration compiler. You record a workflow once, it
compiles the demonstration into a deterministic program, and it replays that
program with **zero model calls on the healthy path**. When an interface drifts,
OpenAdapt re-resolves from retained evidence or proposes a governed repair, and
it **halts instead of guessing** when verification fails.

Substrates are all first-class in the product design (web, Windows, macOS,
Linux, RDP, and Citrix/VDI), reported against an honest maturity ladder:

| Substrate | Maturity |
| --- | --- |
| Browser | Beta - proven end to end today; the only substrate that runs the full loop in CI |
| Windows, macOS, RDP | Early access - each passed a real scoped qualification on named tasks with zero silent-incorrect actions, zero over-halt, and zero model calls; no external customer has run them in production |
| Citrix / VDI | Exploratory - no validated ICA/HDX integration exists yet |

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

Once you sign in, the shell renders a left-rail cockpit over the live engine:

| Screen | What it does |
| --- | --- |
| Login | Sign in with a system-browser PKCE flow or by pasting an ingest token minted in the cloud dashboard; one credential is stored in the OS keychain |
| Onboarding | First-run guidance until a workflow exists locally |
| Workflows | The workflow library: recorded and compiled workflows, with their status |
| Record & review | Start/stop a capture and step through the local review gate before anything leaves the machine |
| Runner (watch it run) | Trigger a replay and watch the compile/replay rail, the live step log, and halt evidence when the run stops for attention |
| Teach | Resolve a halted step and write a governed repair back toward the workflow |
| Settings | Host, deployment lane, credentials, and local preferences |

The rail carries two orthogonal status channels (recording and sync) plus the
needs-attention break count, mirrored from the engine over events. In a plain
dev checkout the shell renders an engine-offline state, because the frozen
sidecar binary is built only in CI.

## Status

| Area | Checked-out implementation | Status |
| --- | --- | --- |
| Python capture CLI | Record, list, inspect, scrub, review, approve, local storage, health, and cleanup commands | Beta; covered by tests |
| Local review gate | Persisted states and egress checks for the capture pipeline | Beta; not the `openadapt-flow` certification system |
| Tauri/React cockpit | Login, onboarding, workflows, record/review, watch-run, teach, and settings calling the engine through Tauri commands | Beta; renders an engine-offline state when the sidecar binary is absent |
| Rust commands | Generic `engine_invoke` bridge plus typed commands, sidecar spawn/watchdog/shutdown, and event re-emission to the WebView | Beta; compiled and bundled in CI |
| Python sidecar IPC | JSON-lines handler backed by a shared `EngineDispatcher` (recording, compile/replay/run/teach, auth, sync/push, review, config) | Beta; unit and e2e tests with mocked boundaries |
| Tray IPC socket server | Token-authenticated loopback TCP server plus a `~/.openadapt/desktop_ipc.json` discovery file for `openadapt-tray` | Beta; not yet validated end to end against the shipped tray |
| Desktop-to-flow handoff | `FlowBridge` launches the pinned Flow runtime embedded in the frozen sidecar as an isolated subprocess | Self-contained; no separate Python or Flow installation |
| Hosted auth and push | Browser-PKCE and paste-token sign-in, keychain-stored credential, bundle push, and halted-run break reports to the hosted control plane | Beta |
| Build artifacts | Wheel/sdist, a self-contained PyInstaller engine+Flow runtime, and DMG/MSI/NSIS/DEB/AppImage native jobs | Native jobs prove the frozen browser lifecycle, structurally install/uninstall, and label every platform, architecture, and signing state |
| Native installers | Distinct `desktop-v*` draft-prerelease workflow with final-byte checksums and GitHub provenance, auto-triggered at each engine release | Beta distribution lane; signing state is encoded in every filename and workflow qualification remains specific |
| Code signing and updater | Apple Developer ID/notarization and Windows Authenticode are credential-gated and fail closed on partial configuration; the updater feed is disabled | In progress; not a supported release channel |

The self-contained `openadapt-engine` freeze is implemented. External code
signing/notarization and the signed updater-key lifecycle remain before a
generally available native release.

## Use OpenAdapt today

For the runnable product loop, use `openadapt-flow` directly:

```bash
pip install openadapt-flow

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

### Native installers (Beta)

Native packages are published under a distinct `desktop-vX.Y.Z` prerelease
channel, separate from the engine's `vX.Y.Z` PyPI/GitHub releases. The native
version is synchronized to each engine release by CI, so a native prerelease
mirrors the engine version it was built from. A native prerelease is packaging
evidence, and it is not a separate supported desktop release. They are unsigned
or ad-hoc-signed Beta artifacts: their filenames carry the platform, architecture, and
signing state, and CI installs, launches, and uninstalls each one on clean
runners. Packaging structure is not workflow qualification.

On macOS, the ad-hoc lane uses an explicit non-hardened overlay because an
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
  [Beta Native Installers](docs/EXPERIMENTAL_NATIVE_INSTALLERS.md).
- The signing activation runbook (what to buy, which secrets to add, and what
  each surface may then truthfully claim) is in
  [docs/CODE_SIGNING.md](docs/CODE_SIGNING.md).

Do not treat the legacy `upload` command or optional upload extras as the
supported hosted path; they predate the current workflow-bundle and
break-report contract.

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

- The first browser workflow downloads the Chromium revision pinned by the
  bundled Playwright runtime into `~/.openadapt/browser-runtime`. The app shows
  setup progress and a retryable failure; no workflow action starts until the
  browser is ready. Air-gapped packages set `PLAYWRIGHT_BROWSERS_PATH` to a
  version-matched prebundle.
- CI proves the frozen binary's browser record -> compile -> replay loop on
  Windows, macOS, and Linux. UI event contracts are automated, while broader
  real-application qualification remains workflow-specific.
- The frozen `openadapt-engine` sidecar binary is produced only by CI. A plain
  dev checkout runs the shell in frontend-only mode.
- Native packages are Beta and unsigned or ad-hoc-signed; structural
  install/uninstall success is not evidence of a validated workflow.
- Apple Developer ID/notarization and Windows Authenticode are credential-gated
  and fail closed on partial configuration; the updater and rollback remain
  disabled pending an independent signing-key lifecycle.
- This repository serves the tray's loopback IPC contract, but the desktop and
  the shipped tray client have not been validated together end to end.

## CLI surface

The Python engine exposes these Beta commands:

| Command | Purpose |
| --- | --- |
| `openadapt-desktop record` | Capture a local session |
| `openadapt-desktop list` / `info` | Inspect capture metadata |
| `openadapt-desktop scrub` | Run configured PII scrubbing |
| `openadapt-desktop review` / `approve` / `dismiss` | Operate the local review state machine |
| `openadapt-desktop compile` / `replay` / `run` | Invoke the bundled, pinned `openadapt-flow` runtime on a capture or bundle |
| `openadapt-desktop login` / `push` / `report-break` | Authenticate to the hosted control plane, push a bundle, report a halted run |
| `openadapt-desktop storage` / `health` / `cleanup` | Inspect and maintain local storage |
| `openadapt-desktop backends` / `upload` | Inspect or invoke legacy upload adapters |
| `openadapt-desktop config` / `doctor` | Inspect local configuration and dependencies |

Raw recordings are local by default. Any egress path still requires careful
review of the selected adapter, configuration, logs, and data-classification
policy. This repository does not by itself establish a HIPAA-compliant or
production-safe deployment.

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
| [`openadapt-tray`](https://github.com/OpenAdaptAI/openadapt-tray) | Experimental system-tray status and launcher companion for this cockpit |
| [`openadapt-capture`](https://github.com/OpenAdaptAI/openadapt-capture) | Experimental capture component used by this Python engine |
| [`openadapt-privacy`](https://github.com/OpenAdaptAI/openadapt-privacy) | Experimental PII detection and redaction component |

Documentation for the wider stack lives at
[docs.openadapt.ai](https://docs.openadapt.ai).

## License

MIT. See [LICENSE](LICENSE).
