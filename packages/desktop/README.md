# OpenAdapt Desktop

[![Tests](https://github.com/OpenAdaptAI/openadapt-desktop/actions/workflows/test.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-desktop/actions/workflows/test.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Lifecycle: Experimental supporting surface.** The canonical OpenAdapt
> workflow engine is
> [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow). This
> repository is not a production desktop application or a supported installer.

OpenAdapt Desktop is the intended authoring and human-teaching companion for
OpenAdapt workflows. The checked-out code does not yet deliver that integrated
product: it contains a tested Python capture/review CLI plus an unintegrated
Tauri shell.

OpenAdapt compiles demonstrated GUI workflows into deterministic, locally
executable programs. Healthy runs make no model calls. When an interface
drifts, OpenAdapt re-resolves from recorded evidence or proposes a governed
repair, and halts when verification fails. Workflow compilation, replay,
certification, and repair live in `openadapt-flow`, not this repository.

## Current Status

| Area | Checked-out implementation | Status |
| --- | --- | --- |
| Python capture CLI | Record, list, inspect, scrub, review, approve, local storage, health, and cleanup commands | Experimental; covered by tests |
| Local review gate | Persisted states and egress checks for the legacy capture pipeline | Experimental; not the `openadapt-flow` certification system |
| Legacy upload adapters | S3-compatible storage, Hugging Face Hub, Magic Wormhole, and federated-learning code paths | Experimental; not the current hosted workflow/break-report contract |
| Tauri/WebView UI | Static prototype assets and a release-mode shell binary built on Linux, macOS, and Windows CI | Compiles unsigned; commands remain scaffold-only |
| Rust commands | Command signatures return `Not implemented` | Not integrated |
| Python sidecar IPC | Protocol skeleton with no registered handlers | Not integrated |
| Desktop-to-flow handoff | No record -> compile -> replay -> teach connection | Not implemented |
| Build artifacts | Wheel/sdist, a smoke-tested PyInstaller sidecar, and an unsigned Tauri shell binary | CI artifacts only; not an integrated or signed release |
| Native installers and updater | Bundle configuration exists; installer signing, updater signing, and release credentials are incomplete | Not release-ready |

The Python package and experimental Tauri shell are versioned independently.
Those implementation versions do not represent separate supported desktop
releases.

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

The current CLI is useful to contributors evaluating the earlier local capture
and review components. It is not the canonical OpenAdapt quickstart and does
not compile or replay workflows.

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

## Intended Architecture

The target boundary is deliberately narrow:

```text
Desktop authoring/teaching UI
        |
        | local authenticated IPC (not implemented here)
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

- The Tauri frontend is not wired to the Python engine.
- The Rust command implementations and sidecar lifecycle are placeholders.
- The Python IPC handler registry is empty.
- No checked-in adapter hands a capture to `openadapt-flow` for compilation.
- No teaching UI writes a governed repair back to a workflow bundle.
- Native DMG, MSI, and Linux packages are not established release artifacts.
- Apple signing/notarization, Windows Authenticode, updater signing, and
  rollback are not configured as a supported release channel.
- The separately developed tray client expects a desktop IPC service that this
  branch does not provide.

## Legacy CLI Surface

The Python engine currently exposes these experimental commands:

| Command | Purpose |
| --- | --- |
| `openadapt-desktop record` | Capture a local session |
| `openadapt-desktop list` / `info` | Inspect capture metadata |
| `openadapt-desktop scrub` | Run configured PII scrubbing |
| `openadapt-desktop review` / `approve` / `dismiss` | Operate the legacy review state machine |
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
only when working on the unfinished shell.

The main implementation areas are:

```text
engine/       Python capture, review, storage, and legacy adapter code
src-tauri/    Experimental Rust/Tauri shell and placeholder sidecar wiring
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
