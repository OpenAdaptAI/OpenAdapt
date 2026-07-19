# OpenAdapt Desktop

[![Tests](https://github.com/OpenAdaptAI/openadapt-desktop/actions/workflows/test.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-desktop/actions/workflows/test.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Lifecycle: Experimental supporting surface.** The canonical OpenAdapt
> workflow engine is
> [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow). This
> repository is not a production desktop application or a supported installer.

OpenAdapt Desktop is the authoring and human-teaching companion for OpenAdapt
workflows. The checked-out code contains a Python engine (capture, review,
hosted auth/push, and a bridge to `openadapt-flow`) wired to a Tauri/React
cockpit over a JSON-lines sidecar IPC, plus a loopback socket server for the
separately developed tray. It is integrated but Experimental: end-to-end tests
mock the capture and flow boundaries, and no supported product release exists.

OpenAdapt compiles demonstrated GUI workflows into deterministic, locally
executable programs. Healthy runs make no model calls. When an interface
drifts, OpenAdapt re-resolves from retained evidence or proposes a governed
repair, and halts when verification fails. Workflow compilation, replay,
certification, and repair live in `openadapt-flow`, not this repository.

## Current Status

| Area | Checked-out implementation | Status |
| --- | --- | --- |
| Python capture CLI | Record, list, inspect, scrub, review, approve, local storage, health, and cleanup commands | Experimental; covered by tests |
| Local review gate | Persisted states and egress checks for the legacy capture pipeline | Experimental; not the `openadapt-flow` certification system |
| Legacy upload adapters | S3-compatible storage, Hugging Face Hub, Magic Wormhole, and federated-learning code paths | Experimental; not the current hosted workflow/break-report contract |
| Tauri/WebView UI | Vite/React cockpit (login, onboarding, record/review, workflow library, watch-run, teach, settings) calling the engine through Tauri commands | Experimental; renders an engine-offline state when the sidecar binary is absent (normal in a plain dev checkout) |
| Rust commands | Implemented: generic `engine_invoke` bridge plus typed commands, sidecar spawn/watchdog/shutdown, event re-emission to the WebView | Experimental; compiled and bundled in CI |
| Python sidecar IPC | JSON-lines stdin/stdout handler backed by a shared `EngineDispatcher` (recording, compile/replay/run/teach, auth, sync/push, review, config commands) | Experimental; unit and e2e tests with mocked boundaries |
| Tray IPC socket server | Token-authenticated loopback TCP server plus discovery file for `openadapt-tray` | Experimental; not yet validated end to end against the shipped tray |
| Desktop-to-flow handoff | `FlowBridge` shells out to the `openadapt-flow` CLI for compile, replay, run, and teach | Experimental; requires a separately installed `openadapt-flow` on `PATH` (not bundled); loop verbs fail with a clear error without it |
| Build artifacts | Wheel/sdist, a PyInstaller sidecar, and Experimental DMG/MSI/NSIS/DEB/AppImage jobs | Native jobs structurally install/uninstall and label every platform, architecture, and signing state |
| Native installers | Distinct `desktop-v*` draft-prerelease workflow with final-byte checksums and GitHub provenance; auto-triggered at each engine release version, with older prereleases marked superseded | Experimental packaging evidence only; not an integrated product release |
| Updater | Plugin code remains compiled, but the unusable empty-key feed configuration is disabled | No updater channel is published |

The Python package releases through semantic-release; the experimental Tauri
shell's native version is synchronized to each engine release by CI so its
`desktop-v*` prerelease mirrors the engine version it was built from. A native
prerelease is packaging evidence, not a separate supported desktop release.
The two-lane release policy, supersession rules, and post-signing convergence
plan are documented in [RELEASES.md](RELEASES.md).

The current release line gates the updater plugin on configuration presence so
packaged installers launch cleanly instead of failing on an empty updater feed.
For the authoritative current version, the matching engine wheel, and its native
installer prerelease, see the
[releases page](https://github.com/OpenAdaptAI/openadapt-desktop/releases): the
newest `vX.Y.Z` engine release is marked "Latest", and its Experimental native
installers ship under the matching `desktop-vX.Y.Z` prerelease tag.

## Use OpenAdapt Today

Use `openadapt-flow` for the runnable product loop:

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

## Inspect This Repository

The current CLI is useful to contributors evaluating the local capture and
review components and the hosted-loop verbs. It is not the canonical OpenAdapt
quickstart; compile/replay/run/teach delegate to a separately installed
`openadapt-flow`.

```bash
git clone https://github.com/OpenAdaptAI/openadapt-desktop.git
cd openadapt-desktop
uv sync --extra dev

uv run openadapt-desktop doctor
uv run openadapt-desktop list
uv run openadapt-desktop storage

uv run pytest tests -q
uv run ruff check engine tests
```

Recording requires the operating-system permissions and runtime support used
by `openadapt-capture`:

```bash
uv run openadapt-desktop record --task "Inspect capture path"
```

Do not treat the `upload` command or optional upload extras as the supported
OpenAdapt hosted path. They belong to an earlier capture-data architecture and
have not been integrated with current workflow bundles, break reports, or
regulated deployment policy.

## Architecture

The boundary is deliberately narrow:

```text
Desktop authoring/teaching UI
        |
        | local IPC (JSON lines over sidecar stdio; token-authenticated
        | loopback socket for the tray)
        v
openadapt-flow
  record -> compile -> lint/certify -> replay -> halt/repair/teach
        |
        +-> optional hosted control-plane metadata and break reporting
```

The desktop application should own consent, operating-system permissions,
recording controls, inspection, and human teaching. It should not duplicate the
workflow compiler or runtime. Those remain in `openadapt-flow`.

## Known Gaps

- `openadapt-flow` is not bundled with the frozen sidecar. Compile, replay,
  run, and teach require a separately installed `openadapt-flow` on `PATH`
  and fail with an explicit error without it.
- End-to-end tests mock the capture and flow boundaries. No CI job drives the
  built UI against a real engine performing a real
  record -> compile -> replay -> teach loop, so integration is code-complete
  but not product-validated.
- The frozen `openadapt-engine` sidecar binary is produced only by CI. A plain
  dev checkout runs the shell in frontend-only mode and shows the engine as
  offline.
- Native packages remain Experimental and unsigned; structural
  install/uninstall success is not evidence of a validated end-to-end
  workflow.
- Apple Developer ID/notarization and Windows Authenticode are credential-gated
  and fail closed on partial configuration. The updater and rollback remain
  disabled pending an independent signing-key lifecycle.
- This repository now serves the tray's loopback IPC contract, but the desktop
  and the separately developed tray client have not been validated together
  end to end.

## CLI Surface

The Python engine currently exposes these experimental commands:

| Command | Purpose |
| --- | --- |
| `openadapt-desktop record` | Capture a local session |
| `openadapt-desktop list` / `info` | Inspect capture metadata |
| `openadapt-desktop scrub` | Run configured PII scrubbing |
| `openadapt-desktop review` / `approve` / `dismiss` | Operate the legacy review state machine |
| `openadapt-desktop compile` / `replay` / `run` | Invoke a separately installed `openadapt-flow` on a capture or bundle |
| `openadapt-desktop login` / `push` / `report-break` | Authenticate to the hosted control plane, push a bundle, report a halted run |
| `openadapt-desktop storage` / `health` / `cleanup` | Inspect and maintain local storage |
| `openadapt-desktop backends` / `upload` | Inspect or invoke legacy upload adapters |
| `openadapt-desktop config` / `doctor` | Inspect local configuration and dependencies |

Raw recordings are local by default. Any egress path still requires careful
review of the selected adapter, configuration, logs, and data-classification
policy. This repository does not establish a HIPAA-compliant or production-safe
deployment by itself.

## Development

Prerequisites are Python 3.11+ and
[`uv`](https://docs.astral.sh/uv/). Rust, Node.js, and the Tauri CLI are needed
only when working on the shell and frontend.

Native packaging details, verification scope, artifact names, and external
signing requirements are documented in
[Experimental Native Installers](docs/EXPERIMENTAL_NATIVE_INSTALLERS.md); the
release-channel policy lives in [RELEASES.md](RELEASES.md).

The main implementation areas are:

```text
engine/       Python capture, review, storage, and legacy adapter code
src-tauri/    Experimental Rust/Tauri shell and sidecar lifecycle wiring
src/          Experimental WebView frontend
tests/        Python unit and end-to-end tests, largely with mocked boundaries
```

See [DESIGN.md](DESIGN.md) as a historical design reference. Where it conflicts
with this status section or with `openadapt-flow`, this README describes the
current public product boundary.

## Related Projects

| Project | Lifecycle and role |
| --- | --- |
| [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow) | Canonical workflow compiler, runtime, certification, and governed repair engine |
| [`OpenAdapt`](https://github.com/OpenAdaptAI/OpenAdapt) | Flagship launcher/meta-repository |
| [`openadapt-capture`](https://github.com/OpenAdaptAI/openadapt-capture) | Experimental capture component used by this Python engine |
| [`openadapt-privacy`](https://github.com/OpenAdaptAI/openadapt-privacy) | Experimental PII detection and redaction component |
| [`openadapt-tray`](https://github.com/OpenAdaptAI/openadapt-tray) | Experimental status and launcher companion; not integrated end to end with this branch |

## License

MIT. See [LICENSE](LICENSE).
