# openadapt-tray

[![PyPI version](https://img.shields.io/pypi/v/openadapt-tray.svg)](https://pypi.org/project/openadapt-tray/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A menu-bar icon that shows what OpenAdapt is doing and gives you a start/stop
button for it. It records nothing, compiles nothing, and replays nothing. It
mirrors state from the desktop app over an authenticated loopback socket and
reads one number from the hosted control plane.

Which means the honest description is: this is a status surface, not an
integrated desktop product. The workflow logic all lives in
[`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow).

[Documentation](https://docs.openadapt.ai) ·
[openadapt-desktop](https://github.com/OpenAdaptAI/openadapt-desktop) ·
[OpenAdapt launcher](https://github.com/OpenAdaptAI/OpenAdapt)

## Install

```bash
pip install openadapt-tray
openadapt-tray            # openadapt-tray-gui is the same entry point,
                          # registered under gui_scripts so Windows
                          # launches it without a console window
```

Needs a graphical session. With no desktop IPC server and no hosted token it
will sit there offline, which is correct behaviour rather than a failure.

## The menu

Built from whatever local and hosted state currently exists:

```text
Start Recording (<configured hotkey>)
Recent Captures
<N automations need attention>    # only when count > 0
Open Desktop App
Open Cloud Dashboard
Account: connected · <N> days left
Pause Sync / Sync (offline)
Settings...
Quit
```

During a local operation the recording item becomes Starting, Stop Recording,
Stopping, or Compiling. The account row turns into a sign-in action, an expiry
warning, or an unavailable status, and clicking it opens the credential
settings page. These labels follow events; the tray isn't doing the work.

The icon is always the OpenAdapt mark, tinted by state so you can read it at a
glance: brand blue idle, amber starting or stopping, red while recording,
purple while compiling, red on error. One high-resolution transparent master
gets tinted, so the states can't drift into different-looking marks.

Recording and sync are two independent channels. A machine can compile a fresh
recording while a previous push is still syncing, or sit idle and offline.

## How it talks to things

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

Local control goes to `openadapt-desktop`. The desktop app writes a discovery
file at `~/.openadapt/desktop_ipc.json`, the tray reads it and opens an
authenticated loopback connection, then sends start/stop, open-library,
open-teach, and pause/resume-sync, and consumes status events coming back. If
discovery fails, the tray launches `openadapt-desktop` and waits about ten
seconds.

Hosted state is one narrow read:

```text
GET <hosted_url>/api/needs-attention/count
Authorization: Bearer <ingest token>
→ { "count": 0, "credential": {
     "expires_at": "2026-08-05T12:00:00Z",
     "expires_in_days": 8,
     "expiring_soon": true,
     "legacy_non_expiring": false,
     "warning_days": 14
   } }
```

The token comes from `OPENADAPT_INGEST_TOKEN` or the OS keychain, never from
`tray.json`, and it's never stored or logged. Polling defaults to 60 seconds,
clamps to a 30-second floor, and backs off when offline. The control plane
decides when a credential enters its 14-day warning window; the tray shows one
actionable notification per credential and expiry, surviving a tray restart,
and keeps only a non-secret identity digest to deduplicate.

No screenshots, bundles, or capture artifacts go through this poller.

Clicking the needs-attention row routes by lane. On `cloud` it opens
`<hosted_url>/dashboard`. On `byoc` with desktop IPC connected it sends
`open_teach` locally, so workflow data stays inside the customer environment.
On `byoc` with no desktop IPC it currently falls back to the hosted dashboard,
and a regulated deployment should not treat that fallback as a validated
PHI-safe path. Gate it or change it before production.

## What this doesn't do yet

The tray's client behaviour is covered by unit tests: the state machine, the
IPC framing, the menus, the icon tinting, and mocked hosted HTTP. None of that
proves a working installer, a live hosted service, or an authoring loop that
runs end to end.

- `openadapt-desktop`'s `main` serves the exact discovery-socket and command
  contract this tray expects, but the two have not been validated together end
  to end, and no signed generally-available desktop build ships that server.
- No packaged installer proves tray startup, permissions, or auto-start on
  macOS, Windows, and Linux.
- Hosted polling is tested against mocks. Nothing here validates a live service
  contract.
- Recent-capture View still calls a legacy launcher command before falling back
  to a file browser.
- Sign-in opens a settings page. There's no interactive authentication.
- The tray doesn't certify a workflow or verify its effects. Nothing here is a
  safety control.

## Configuration

Non-secret settings live at `~/Library/Application Support/openadapt/tray.json`
on macOS, `%APPDATA%/openadapt/tray.json` on Windows, and
`${XDG_CONFIG_HOME:-~/.config}/openadapt/tray.json` on Linux:

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

`deployment_lane` takes `cloud` or `byoc`. The triple-ctrl stop hotkey is real,
not a typo: the `stop_on_triple_ctrl` setting defaults to true and is what
enables it. The ingest token does
not belong in this file.

## Development

```bash
git clone https://github.com/OpenAdaptAI/openadapt-tray.git
cd openadapt-tray
uv sync --extra dev
uv run pytest tests -q
uv run openadapt-tray
```

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
```

For an OpenAdapt workflow you can actually run, use the launcher instead:

```bash
pip install 'openadapt[browser]'
openadapt quickstart
```

## License

[MIT](LICENSE)
