# openadapt-tray

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)

> **Lifecycle: Experimental supporting surface.** The canonical workflow
> engine is [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow).
> The behavior described below exists on the checked-out
> `feat/hosted-rewire` branch and is not a generally available integrated
> desktop release.

OpenAdapt Tray is a lightweight status mirror and launcher for the intended
OpenAdapt desktop authoring experience. It does not record, compile, replay,
repair, or train models itself. Local actions are delegated to a companion
desktop process over authenticated loopback IPC; hosted status is read from a
small needs-attention endpoint.

OpenAdapt compiles demonstrated GUI workflows into deterministic, locally
executable programs. Healthy runs make no model calls. When an interface
drifts, OpenAdapt re-resolves from recorded evidence or proposes a governed
repair, and halts when verification fails. That workflow logic belongs to
`openadapt-flow`, not the tray.

## Release Boundary

- Package metadata is currently version `0.0.1` and marks this project
  pre-alpha.
- The hosted rewire is branch code. Installing an existing published package
  must not be assumed to provide the behavior documented here.
- Unit tests cover the client state machine, IPC framing, menus, and mocked
  hosted HTTP behavior. They do not prove a working desktop installer, hosted
  service, or end-to-end authoring loop.
- The current `openadapt-desktop` main branch does not implement the discovery
  socket and command contract this tray expects. The two repositories are not
  integrated end to end in their checked-out state.

## What This Branch Implements

| Surface | Behavior | Maturity |
| --- | --- | --- |
| Recording status | Mirrors start, stop, compiling, and error events received from desktop IPC | Client implemented; companion server unavailable |
| Recording controls | Sends start/stop commands and can launch `openadapt-desktop` when discovery fails | Client implemented; no compatible released desktop |
| Workflow shortcuts | Requests the desktop workflow library or teach view | Client implemented; corresponding desktop views unavailable |
| Sync status | Mirrors synced, syncing, and offline states separately from recording state | Implemented and tested |
| Needs-attention badge | Polls `GET /api/needs-attention/count` using an ingest token from the environment or OS keychain | Mock-tested client contract; hosted availability not established here |
| Break routing | Cloud opens the hosted dashboard; connected BYOC opens local teach | Implemented with an important fallback described below |
| Recent captures | Reads local capture directories; View still tries the legacy `openadapt visualize` command before a file-browser fallback | Transitional behavior |

The retired model-training controls and training states are not part of this
branch.

## Expected Menu

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

## Integration Contract

### Local desktop IPC

The tray discovers a local service from `~/.openadapt/desktop_ipc.json`, then
uses an authenticated loopback connection. It can send commands to start or
stop recording, open the workflow library or teach surface, and pause or resume
sync. It also consumes desktop status events.

If discovery fails, the tray launches `openadapt-desktop` and waits about ten
seconds for the service. The current desktop package exposes a capture/review
CLI under that name but does not start the expected IPC service, so this path
does not currently produce a working integration.

### Hosted needs-attention polling

The poller calls:

```text
GET <hosted_url>/api/needs-attention/count
Authorization: Bearer <ingest token>
```

The token is resolved from `OPENADAPT_INGEST_TOKEN` or the OS keychain and is
not written to `tray.json`. The default poll interval is 60 seconds, clamped to
at least 30 seconds, with a slower offline retry.

This is a narrow status endpoint, not hosted execution. The tray does not
upload screenshots, workflow bundles, or capture artifacts through this
poller.

### Deployment-lane routing

- `cloud`: a needs-attention click opens
  `<hosted_url>/dashboard`, which lists open halts and uncertain dispatches.
- `byoc`: while desktop IPC is connected, the click sends `open_teach` locally
  so workflow data can remain in the customer environment.
- `byoc` without desktop IPC: the current implementation falls back to the
  hosted dashboard. Regulated deployments must not treat this fallback as a
  validated PHI-safe path; it should be changed or policy-gated before
  production use.

## Development Quickstart

Run this branch as contributor software, not as a production install:

```bash
git clone https://github.com/OpenAdaptAI/openadapt-tray.git
cd openadapt-tray
git switch feat/hosted-rewire

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

`deployment_lane` accepts `cloud` or `byoc`. The ingest token does not belong
in this file.

## Known Gaps

- No compatible released desktop IPC server completes the local control path.
- No packaged installer proves tray startup, permissions, or auto-start across
  macOS, Windows, and Linux.
- Hosted polling is tested with mocks; repository tests do not validate a live
  service contract or service-level commitments.
- BYOC fallback can open the hosted dashboard when desktop IPC is absent.
- Recent-capture View still invokes a legacy launcher command.
- Login opens an ingest-token settings page; the tray does not implement
  interactive authentication.
- The tray does not certify workflow safety or verify workflow effects.

## Project Structure

```text
src/openadapt_tray/
  app.py            tray lifecycle, desktop delegation, and routing
  ipc.py            authenticated loopback IPC client
  hosted.py         needs-attention poller and lane routing
  state.py          recording, sync, and badge state
  menu.py           state-dependent tray menu
  keychain.py       ingest-token lookup
  config.py         non-secret local preferences
tests/               mocked/unit coverage for these client boundaries
```

## Related Projects

| Project | Lifecycle and role |
| --- | --- |
| [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow) | Canonical workflow compiler, runtime, certification, and governed repair engine |
| [`openadapt-desktop`](https://github.com/OpenAdaptAI/openadapt-desktop) | Experimental authoring/teaching direction; current main is not IPC-compatible with this branch |
| [`OpenAdapt`](https://github.com/OpenAdaptAI/OpenAdapt) | Flagship launcher/meta-repository |

## License

MIT. See [LICENSE](LICENSE).
