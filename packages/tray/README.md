# openadapt-tray

[![PyPI version](https://img.shields.io/pypi/v/openadapt-tray.svg)](https://pypi.org/project/openadapt-tray/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/tray/LICENSE)

openadapt-tray puts OpenAdapt in the macOS menu bar or the Windows and Linux
system tray. You can start and stop recordings from its menu or with a hotkey,
see whether OpenAdapt Desktop is recording or compiling a recording into a
workflow, and see how many of your automations in OpenAdapt Cloud stopped and
need a person.

It's for people who use
[OpenAdapt Desktop](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/desktop) and want
those controls without opening the app window. The tray doesn't record or run
anything itself. OpenAdapt Desktop records, and
[openadapt-flow](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/flow) compiles and
replays workflows. The tray sends them commands and shows what they report.

## Quick start

```bash
pip install openadapt-tray
openadapt-tray
```

The OpenAdapt icon appears in the menu bar or system tray. When you select
**Start Recording**, the tray asks for a capture name, starts the installed
OpenAdapt Desktop app if it isn't running, and asks it to record.

Without the desktop app, the tray runs, but recording can't start. OpenAdapt
Desktop has no trusted signed build to download yet
([its README](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/desktop#quick-start) explains),
and the `openadapt-desktop` command that pip installs doesn't open the socket
the tray connects to.

On macOS, the global hotkeys need permission to read the keyboard. Allow the
app that starts the tray, such as Terminal, in **System Settings** >
**Privacy & Security** under **Accessibility** and **Input Monitoring**. The
menu works without them.

Once OpenAdapt Desktop is signed in to OpenAdapt Cloud, the tray checks for
automations that need attention. It reads the same token from the OS keychain,
or from the `OPENADAPT_INGEST_TOKEN` environment variable.

## The menu

```text
Start Recording (⌃⇧R)              Ctrl+Shift+R on Windows and Linux
Recent Captures                    View opens a capture's folder
⚠ 2 automations need attention     shown only when the count is above zero
Open Desktop App
Open Cloud Dashboard
Account: connected · 30 days left  or "Account: sign in" when signed out
Pause Sync                         "Resume Sync" while paused,
                                   "Sync (offline)" when signed out or offline
Settings...                        opens tray.json
Quit
```

While OpenAdapt Desktop works, the recording item changes to **Starting...**,
**Stop Recording**, **Stopping...**, or **Compiling...**, and the icon changes
color:

| Idle | Starting or stopping | Recording, or an error | Compiling |
| :---: | :---: | :---: | :---: |
| <img src="https://raw.githubusercontent.com/OpenAdaptAI/OpenAdapt/main/packages/tray/assets/icons/idle@2x.png" width="32" alt="Blue OpenAdapt icon"> | <img src="https://raw.githubusercontent.com/OpenAdaptAI/OpenAdapt/main/packages/tray/assets/icons/recording_starting@2x.png" width="32" alt="Amber OpenAdapt icon"> | <img src="https://raw.githubusercontent.com/OpenAdaptAI/OpenAdapt/main/packages/tray/assets/icons/recording@2x.png" width="32" alt="Red OpenAdapt icon"> | <img src="https://raw.githubusercontent.com/OpenAdaptAI/OpenAdapt/main/packages/tray/assets/icons/compiling@2x.png" width="32" alt="Purple OpenAdapt icon"> |

A red dot on the icon means automations need attention. Selecting the
needs-attention item opens the cloud dashboard. If `deployment_lane` is `byoc`,
the setting for keeping recordings on this machine, the item opens Teach in
OpenAdapt Desktop instead and never opens the cloud dashboard. Either
`tray.json` or OpenAdapt Desktop can set it.

[PROTOCOL.md](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/tray/docs/PROTOCOL.md)
describes the local socket, the one cloud request, and how the tray handles the
token.

## Configuration

**Settings...** opens `tray.json` in a text editor and creates it with the
defaults if it doesn't exist. Changes take effect the next time the tray starts.
The file is at:

- macOS: `~/Library/Application Support/openadapt/tray.json`
- Windows: `%APPDATA%\openadapt\tray.json`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/openadapt/tray.json`

The settings you're most likely to change:

```json
{
  "hotkeys": {
    "toggle_recording": "<ctrl>+<shift>+r",
    "open_dashboard": "<ctrl>+<shift>+d"
  },
  "stop_on_triple_ctrl": true,
  "hosted_url": "https://app.openadapt.ai",
  "deployment_lane": "cloud",
  "poll_interval_s": 60,
  "show_notifications": true
}
```

- Hotkeys use [pynput](https://pynput.readthedocs.io/) syntax.
  `open_dashboard` opens the workflow library in OpenAdapt Desktop.
- `stop_on_triple_ctrl` stops a recording when you press Ctrl three times,
  each within half a second of the last.
- `deployment_lane` is `cloud` or `byoc`.
- `captures_directory` is the folder that **Recent Captures** lists. It
  defaults to `~/openadapt/captures`.
- `poll_interval_s` sets how often, in seconds, the tray checks OpenAdapt
  Cloud. The minimum is 30.

The token never goes in this file. The file that **Settings...** creates also
has keys the tray doesn't use: `dashboard_port`, `auto_launch_dashboard`,
`auto_start_on_login`, `minimize_to_tray`, `default_record_audio`,
`default_transcribe`, and `hotkeys.stop_recording`.

## Development

```bash
git clone https://github.com/OpenAdaptAI/OpenAdapt.git
cd OpenAdapt/packages/tray
uv sync --extra dev
uv run pytest tests -q
uv run ruff check .
uv run openadapt-tray
```

```text
src/openadapt_tray/
  app.py            tray lifecycle, desktop delegation, and click routing
  ipc.py            client for OpenAdapt Desktop's loopback socket
  hosted.py         needs-attention poller and lane routing
  state.py          recording, sync, and badge state
  menu.py           the menu
  icons.py          per-state icon tinting
  shortcuts.py      global hotkeys
  keychain.py       ingest-token lookup
  config.py         tray.json
  desktop.py        finds and starts the installed OpenAdapt Desktop app
  notifications.py  desktop notifications
  platform/         per-OS dialogs and opening tray.json
```

To try OpenAdapt itself from the command line, start with the
[OpenAdapt launcher](https://github.com/OpenAdaptAI/OpenAdapt).

## Limits

- The tray doesn't record, compile, replay, or check workflow results. It
  shows what OpenAdapt Desktop and OpenAdapt Cloud report.
- OpenAdapt Desktop's CI tests this tray's socket client against the desktop's
  server. The two apps haven't been run together end to end from packaged
  installs.
- The quick start was checked by hand on macOS 15: the tray starts and shows
  this menu. Recording through OpenAdapt Desktop and the global hotkeys weren't
  tried. On Windows and Linux, only the unit tests run in CI.
- On macOS, a tray installed with pip sends notifications through AppleScript,
  so clicking a needs-attention notification doesn't open the dashboard.
  Notification clicks haven't been tried on Windows or Linux.
- The cloud poller is tested against mocked responses, not a live account.
- There's no installer, and the tray doesn't start at login.
- OpenAdapt Desktop saves its captures in `~/.openadapt/captures` and manages
  them itself, so they don't appear under **Recent Captures** by default. Open
  them from the desktop app.
- **Account: sign in** opens the token page of the cloud dashboard. The tray
  has no sign-in flow of its own.

## License

[MIT](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/tray/LICENSE)
