# How openadapt-tray talks to OpenAdapt

The tray has two connections: a local socket to OpenAdapt Desktop, and one
authenticated read from OpenAdapt Cloud. It doesn't record, compile, or replay
anything itself.

```text
openadapt-tray
    |                                   \
    | loopback socket, per-session token \ HTTPS: GET needs-attention count
    v                                     v
OpenAdapt Desktop                     OpenAdapt Cloud (app.openadapt.ai)
    |
    v
openadapt-flow (compile, replay, repair)
```

## OpenAdapt Desktop

When its socket server starts, OpenAdapt Desktop writes a discovery file to
`~/.openadapt/desktop_ipc.json`:

```json
{"protocol_version": 1, "host": "127.0.0.1", "port": 50123, "token": "<per-session token>"}
```

The tray reads that file and opens a TCP connection to the loopback address.
Messages are newline-delimited JSON, and every command carries the token so the
desktop app can reject other local processes. The tray refuses a discovery file
whose `protocol_version` isn't `1`. When it connects, the tray asks the
desktop app for a status snapshot.

| Direction | Messages |
| --- | --- |
| Tray to desktop | `start_recording`, `stop_recording`, `get_status`, `open_workflow_library`, `open_teach`, `pause_sync`, `resume_sync` |
| Desktop to tray | `recording_started`, `recording_stopped`, `recording_error`, `status_update`, `compile_progress`, `sync_state`, `break_count` |

If there is no discovery file when you select an action that needs the desktop
app, the tray starts the installed app and waits up to about 10 seconds for the
file to appear:

- macOS: `open -b ai.openadapt.desktop`
- Windows: `OpenAdapt Desktop\openadapt-desktop.exe` under `%LOCALAPPDATA%`,
  `%ProgramFiles%`, or `%ProgramFiles(x86)%`
- Linux: `gtk-launch "OpenAdapt Desktop"`, then `/usr/bin/openadapt-desktop`,
  then the AppImage named by `OPENADAPT_DESKTOP_APPIMAGE`

The tray never runs a Python `openadapt-desktop` console script.

OpenAdapt Desktop's CI runs this package's IPC client, from the same
monorepo commit, against the desktop's socket server
([`packages/desktop/tests/test_tray_integration.py`](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/desktop/tests/test_tray_integration.py)).

## OpenAdapt Cloud

The tray makes one request:

```text
GET <hosted_url>/api/needs-attention/count
Authorization: Bearer <ingest token>
```

```json
{
  "count": 0,
  "credential": {
    "expires_at": "2026-08-05T12:00:00Z",
    "expires_in_days": 8,
    "expiring_soon": true,
    "legacy_non_expiring": false,
    "warning_days": 14
  }
}
```

- The token comes from `OPENADAPT_INGEST_TOKEN` or from the OS keychain entry
  that OpenAdapt Desktop writes (service `ai.openadapt.desktop`, account
  `<hosted_url>`). It is never read from or written to `tray.json`, and never
  logged.
- The tray polls every 60 seconds by default, never more often than every 30
  seconds, and every 5 minutes while offline.
- A response without a readable integer `count` is treated as a failed poll.
  The badge keeps its last value instead of dropping to zero.
- OpenAdapt Cloud decides when a credential is expiring soon. The tray shows
  one notification per credential and expiry date, and remembers which ones it
  showed in `credential-warning-state.json` next to `tray.json`. That file holds
  only hashes, never the token.
- No screenshots, recordings, or workflow data go through this request.

## Needs-attention clicks

| Lane | What a click opens |
| --- | --- |
| `cloud` | `<hosted_url>/dashboard` in the default browser |
| `byoc` | Teach in OpenAdapt Desktop, through `open_teach`. If the desktop app isn't running, the tray starts it first. If that fails, the tray says so and opens nothing else. |

The lane is `byoc` when either `tray.json` or OpenAdapt Desktop's status report
says so. On `byoc`, the tray never opens the cloud dashboard.
