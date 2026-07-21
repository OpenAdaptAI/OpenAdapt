# openadapt-tray

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![PyPI version](https://img.shields.io/pypi/v/openadapt-tray.svg)](https://pypi.org/project/openadapt-tray/)

> **Lifecycle: Experimental supporting surface.** The canonical compiler and
> governed runtime live in
> [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow). This tray
> is published on PyPI as `openadapt-tray`, but it is a companion status
> surface, not a generally available integrated desktop product.

## What OpenAdapt is

OpenAdapt is a governed demonstration compiler. You record a workflow once, it
compiles the demonstration into a deterministic program, and it replays that
program with **zero model calls on the healthy path**. When an interface drifts,
OpenAdapt re-resolves from retained evidence or proposes a governed repair, and
it **halts instead of guessing** when verification fails. Substrates are all
first-class in the product design (web, Windows, macOS, Linux, RDP, and
Citrix/VDI), with browser proven end to end today and the other substrates at
earlier maturity. That workflow logic belongs to `openadapt-flow`.

## What the tray is

OpenAdapt Tray is a lightweight system-tray companion for the OpenAdapt desktop
authoring experience. It does not record, compile, replay, repair, or train
anything itself. It mirrors state and hands local actions to a companion desktop
process over an authenticated loopback connection, and it reads a small
needs-attention count from the hosted control plane.

From the tray you can:

- **Start or stop recording** on the desktop app, and see the live recording
  and compile lifecycle.
- **Open recent captures** from the local captures directory.
- **See how many automations need attention** and jump straight to them.
- **Open the desktop app** or the **cloud dashboard**.
- **Pause or resume sync**, shown as its own channel.
- **Sign in** (open the ingest-token settings page) and open **settings**.

### The per-state OpenAdapt-mark icon

The tray icon is always the OpenAdapt mark, tinted per lifecycle state so the
state stays readable at a glance:

| State | Tint |
| --- | --- |
| Idle | Brand blue |
| Recording starting | Amber |
| Recording | Red |
| Recording stopping | Amber |
| Compiling | Purple |
| Error | Red |

The mark is stored once as a high-resolution transparent master and tinted from
it, so every state renders as the same recognizable mark and the states never
drift apart. Recording lifecycle and sync are modelled as two independent
channels: a machine can be compiling a fresh recording while a previous push is
still syncing, or sit idle while offline.

## How it fits with desktop and cloud

```text
openadapt-tray (status mirror + launcher)
    |                                   \
    | authenticated loopback IPC         \ HTTPS: GET needs-attention count
    v                                     v
openadapt-desktop (local cockpit)     hosted control plane (app.openadapt.ai)
    |
    v
openadapt-flow (compile / replay / halt / repair / teach)
```

- **Local control** goes to `openadapt-desktop` over an authenticated loopback
  socket. The desktop app writes a discovery file the tray reads, then the tray
  sends start/stop, open-library, open-teach, and pause/resume-sync commands and
  consumes desktop status events.
- **Hosted status** is a narrow read: the tray polls a needs-attention count and
  routes a click to the right place. It never uploads screenshots, bundles, or
  capture artifacts.

## Release boundary

- The hosted-lifecycle behavior described here is merged on `main` and published
  to PyPI; the package classifier is Pre-Alpha.
- Unit tests cover the client state machine, IPC framing, menus, the per-state
  icon tinting, and mocked hosted HTTP behavior. They do not prove a working
  desktop installer, a live hosted service, or an end-to-end authoring loop.
- The `openadapt-desktop` `main` branch now serves the exact discovery-socket
  and command contract this tray expects, but the two surfaces have
  not been validated together end to end, and no signed, generally available
  desktop build ships that server yet. Treat the tray as a status surface until
  that integration is qualified.

The retired model-training controls and training states are not part of this
release.

## Expected menu

The menu is built from current local and hosted state:

```text
Start Recording (<configured hotkey>)
Recent Captures
<N automations need attention>    # only when count > 0
Open Desktop App
Open Cloud Dashboard
Pause Sync / Sync (offline)
Login...
Settings...
Quit
```

During a local operation, the recording item changes to Starting, Stop
Recording, Stopping, or Compiling. These labels reflect events; the tray does
not perform the work.

## Integration contract

### Local desktop IPC

The tray discovers a local service from `~/.openadapt/desktop_ipc.json`, then
uses an authenticated loopback connection. It can send commands to start or stop
recording, open the workflow library or teach surface, and pause or resume sync.
It also consumes desktop status events.

If discovery fails, the tray launches `openadapt-desktop` and waits about ten
seconds for the service. The desktop `main` branch now implements a matching
token-authenticated loopback server and writes this discovery file, but that
path has not yet been validated end to end from a shipped desktop build.

### Hosted needs-attention polling

The poller calls:

```text
GET <hosted_url>/api/needs-attention/count
Authorization: Bearer <ingest token>
```

The token is resolved from `OPENADAPT_INGEST_TOKEN` or the OS keychain and is
not written to `tray.json`. The default poll interval is 60 seconds, clamped to
at least 30 seconds, with a slower offline retry.

This is a narrow status endpoint, not hosted execution. The tray does not upload
screenshots, workflow bundles, or capture artifacts through this poller.

### Deployment-lane routing

- `cloud`: a needs-attention click opens `<hosted_url>/dashboard`, which lists
  open halts and uncertain dispatches.
- `byoc`: while desktop IPC is connected, the click sends `open_teach` locally
  so workflow data can remain in the customer environment.
- `byoc` without desktop IPC: the current implementation falls back to the
  hosted dashboard. Regulated deployments must not treat this fallback as a
  validated PHI-safe path; it should be changed or policy-gated before
  production use.

## Installation

```bash
pip install openadapt-tray
openadapt-tray            # run the tray application
```

Treat the install as an Experimental status surface, not a production desktop
product (see the release boundary above).

## Development quickstart

```bash
git clone https://github.com/OpenAdaptAI/openadapt-tray.git
cd openadapt-tray

uv sync --extra dev
uv run pytest tests -q
uv run openadapt-tray
```

Running the process requires a graphical desktop/session. Without a compatible
desktop IPC server or hosted token it may correctly remain offline, fail local
actions, or open only browser routes.

For a runnable OpenAdapt workflow, use the canonical engine separately:

```bash
pip install openadapt-flow
openadapt-flow demo-record --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow replay bundle
```

## Configuration

Non-secret settings are stored at:

- macOS: `~/Library/Application Support/openadapt/tray.json`
- Windows: `%APPDATA%/openadapt/tray.json`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/openadapt/tray.json`

Representative settings:

```json
{
  "hotkeys": {
    "toggle_recording": "<ctrl>+<shift>+r",
    "open_dashboard": "<ctrl>+<shift>+d",
    "stop_recording": "<ctrl>+<ctrl>+<ctrl>"
  },
  "captures_directory": "~/openadapt/captures",
  "desktop_ipc_port": null,
  "hosted_url": "https://app.openadapt.ai",
  "deployment_lane": "cloud",
  "poll_interval_s": 60,
  "show_notifications": true,
  "auto_start_on_login": false
}
```

`deployment_lane` accepts `cloud` or `byoc`. The ingest token does not belong in
this file.

## Known gaps

- The desktop socket contract exists on the desktop `main` branch, but no
  shipped desktop build has been validated end to end with this tray.
- No packaged installer proves tray startup, permissions, or auto-start across
  macOS, Windows, and Linux.
- Hosted polling is tested with mocks; repository tests do not validate a live
  service contract or service-level commitments.
- BYOC fallback can open the hosted dashboard when desktop IPC is absent.
- Recent-capture View still invokes a legacy launcher command before its
  file-browser fallback.
- Login opens an ingest-token settings page; the tray does not implement
  interactive authentication.
- The tray does not certify workflow safety or verify workflow effects.

## Project structure

```text
src/openadapt_tray/
  app.py            tray lifecycle, desktop delegation, and routing
  ipc.py            authenticated loopback IPC client
  hosted.py         needs-attention poller and lane routing
  state.py          recording, sync, and badge state
  menu.py           state-dependent tray menu
  icons.py          per-state OpenAdapt-mark tinting
  keychain.py       ingest-token lookup
  config.py         non-secret local preferences
tests/              mocked/unit coverage for these client boundaries
```

## Related projects

| Project | Lifecycle and role |
| --- | --- |
| [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow) | Canonical workflow compiler, runtime, certification, and governed repair engine |
| [`openadapt-desktop`](https://github.com/OpenAdaptAI/openadapt-desktop) | Experimental authoring/teaching cockpit; its `main` now serves the IPC contract this tray expects, pending end-to-end validation |
| [`OpenAdapt`](https://github.com/OpenAdaptAI/OpenAdapt) | Flagship launcher and meta-repository |

Documentation for the wider stack lives at
[docs.openadapt.ai](https://docs.openadapt.ai).

## License

MIT. See [LICENSE](LICENSE).
