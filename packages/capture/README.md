# openadapt-capture

[![Build Status](https://github.com/OpenAdaptAI/openadapt-capture/actions/workflows/test.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-capture/actions/workflows/test.yml)
[![PyPI version](https://img.shields.io/pypi/v/openadapt-capture.svg)](https://pypi.org/project/openadapt-capture/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/OpenAdaptAI/openadapt-capture/blob/main/LICENSE)

Record what a person does on a Windows or macOS desktop, then read it back in
Python. Capture saves mouse clicks, drags, scrolls, keystrokes, and screen
video to one local folder, and pairs every action with the screen frame from
the moment it happened.

Use it when you need real demonstrations of GUI work: to build an automation
or replay tool, to collect training or test data for a computer-use model, or
to study how a task gets done. [OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt)
uses it to record the demonstrations that it compiles into replayable
programs.

**Status:** maintained. OpenAdapt uses it to record the demonstrations it
compiles. Everything stays in a local folder.

| Platform | Records | Notes |
|---|---|---|
| macOS | Mouse, keyboard, screen video, accessibility | Needs Screen Recording, Input Monitoring, and Accessibility permissions |
| Windows | Mouse, keyboard, screen video, accessibility | Needs a signed-in desktop session |
| Linux | Mouse, keyboard, screen video; accessibility with the `linux` extra | X11 only, not Wayland |
| Remote desktop | Pixels only | No accessibility data across the connection |

## Quick start

You need Python 3.10 or later, and before the first recording:

- **FFmpeg.** Capture writes video with the `ffmpeg` executable on your
  `PATH`. If you don't have one, install FFmpeg with your package manager. On
  Capture 1.3.0 or later, `capture install-ffmpeg` can instead install a
  pinned, checksum-verified build for your user account.
- **macOS.** In System Settings > Privacy & Security, allow your terminal app
  under Screen Recording, Input Monitoring, and Accessibility. Then quit and
  reopen the terminal.
- **Windows.** Run from a signed-in desktop session.

```bash
pip install openadapt-capture
capture record ./my-capture --description "Rename a file"
```

If FFmpeg or a permission is missing, `capture record` stops before it records
and says what to fix. Otherwise, do the task. To stop, press Ctrl+C in the
terminal, or press Ctrl three times in any app. Then look at what you
recorded:

```bash
capture info ./my-capture
```

```python
from openadapt_capture import CaptureSession

with CaptureSession.load("./my-capture") as capture:
    for action in capture.actions():
        print(action.timestamp, action.type, action.x, action.y, action.text)
        frame = action.screenshot  # PIL image of the screen at that moment
```

## What a recording contains

```text
my-capture/
├── recording.db                     # actions, raw input events, window metadata (SQLite)
├── oa_recording-*.mp4               # screen video
├── capture-state.json               # whether the session finished and passed its integrity check
├── capture-artifact-manifest.json   # size and SHA-256 of every other file
├── capture-terminal.json            # completion record that covers the manifest
└── *.json                           # profiling data, and sign-in markers if you used them
```

Each action has a type (click, double-click, drag, scroll, typed text, or
keyboard shortcut), screen coordinates, any typed text, and a timestamp.
`action.screenshot` and `action.after_screenshot` give the frames before and
after it.

Where the operating system exposes it, Capture also stores the accessibility
element under each action: its role, name, bounds, available actions, and
window, from Windows UI Automation or macOS Accessibility. Read it with
`action.structural_observation`. Capture never guesses these fields from
pixels. Turn this off with `Recorder(..., capture_structural_observations=False)`.

## Record from Python

```python
from openadapt_capture import Recorder

with Recorder("./my-capture", task_description="Rename a file") as recorder:
    recorder.wait_for_ready()
    input("Do the task, then press Enter...")
```

To record one window instead of the whole desktop, pass
`window={"owner": "Parallels", "title": None}` (substring match), or use
`capture record --window-owner Parallels`. Coordinates are then in that
window's own pixels, which is what remote-desktop clients such as RDP and
Citrix need. Details: [docs/WINDOW_CAPTURE.md](https://github.com/OpenAdaptAI/openadapt-capture/blob/main/docs/WINDOW_CAPTURE.md).

To control a running recording from another process:

```bash
capture status --session-id SESSION_ID
capture stop --session-id SESSION_ID
```

`capture record` prints the session ID when it starts. From Python, use
`status_recording(session_id)` and `stop_recording(session_id, timeout=60)`.
`stop` waits until the video and database are written and checked, and
returns an error if anything is incomplete.

If the task includes signing in, wrap the sign-in in an authentication
handoff. Capture then stops recording the screen, input, and accessibility
data until the sign-in ends, and stores only a marker:

```python
handoff = recorder.begin_authentication(
    methods="password_manager",
    requires_user_presence=True,
)
# Sign in.
recorder.end_authentication(handoff, outcome="completed")
```

See [docs/AUTHENTICATION_HANDOFF.md](https://github.com/OpenAdaptAI/openadapt-capture/blob/main/docs/AUTHENTICATION_HANDOFF.md).

## Use it with OpenAdapt

To compile a recording into a program that replays the task, install
OpenAdapt with this package as an extra. OpenAdapt needs Python 3.10, 3.11,
or 3.12.

```bash
pip install "openadapt[capture]"
openadapt flow record --backend windows --out recording --task "Rename a file"
openadapt flow compile recording --out bundle --name rename-file
```

Other platforms, replay, and remote desktops are covered in the
[desktop recording guide](https://github.com/OpenAdaptAI/openadapt-flow/blob/main/docs/desktop/RECORDING.md).
The compiler accepts left and right clicks, left-button drags, typed text,
named keys, key combinations, and scrolling. It rejects a recording that
contains anything else, such as a middle click.

## FFmpeg

`capture install-ffmpeg` downloads one pinned FFmpeg build for your platform,
checks its SHA-256 against a digest stored in this package, and installs it
under your user data folder. Builds exist for macOS (Apple silicon and Intel),
Linux x86-64, and Windows x86-64. `--dry-run` prints the URL, digest, and
destination without downloading anything, and `capture uninstall-ffmpeg`
removes the build. The package itself contains no FFmpeg, and Capture
downloads nothing unless you run that command.

An FFmpeg you already set up takes priority. Capture looks in this order:
`Recorder(ffmpeg_path=...)` or `OPENADAPT_FFMPEG_PATH`, then
`OPENADAPT_DESKTOP_FFMPEG_PATH`, then OpenAdapt Desktop's runtime, then your
`PATH`, and the installed build last. Before recording, Capture runs a short
encode-and-decode test and refuses to start if it fails.

## Microphone narration

`capture record --audio` also records your voice and transcribes it on this
computer. It's off by default. It needs a local transcription backend and
openadapt-privacy, because the transcript is scrubbed before it is stored:

```bash
pip install "openadapt-capture[audio,privacy]"
python -m spacy download en_core_web_sm
```

Capture refuses `--audio` before it opens the microphone if either is
missing, and it deletes the audio after transcription unless you set
`RECORD_AUDIO_RETAIN_WAVEFORM`.

The scrubbed transcript is saved as `narration.json` in the capture folder:
one entry per sentence, timed on the same clock as your clicks and keys. Word
by word text is not kept. `capture narration ./my_recording` prints it, and
`capture transcribe ./my_recording` builds it from a retained `audio.flac`.

OpenAdapt Flow uses these sentences when it learns a program from several
recordings. If you say why you take a branch ("if the insurance is Medicare,
skip the copay screen"), Flow proposes that condition. It becomes part of the
program only when every recording agrees with it, a test copy confirms it, or
a person accepts it. See Flow's
[NARRATION.md](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/flow/docs/NARRATION.md).

## Privacy

A recording can contain anything that was on screen or typed, including
passwords and personal data. It stays on your computer. Capture uploads
nothing unless you run `capture share` or set `send_profile=True`, and neither
sends the raw recording by default.

`capture share send` scrubs a copy of the recording and sends only the copy.
Scrubbing needs the `privacy` extra:

```bash
pip install "openadapt-capture[privacy,share]"
python -m spacy download en_core_web_sm
capture share send ./my_recording
```

The copy keeps the recording database with text and accessibility values
scrubbed and screenshots redacted. It leaves out typed keys (a name typed one
key at a time can't be recognized), audio, video, and any file type that can't
be scrubbed, and lists what it left out in `SCRUB_MANIFEST.json`. Scrubbing is
automatic and can miss data, so review the copy before you share it further.
Without the `privacy` extra, `capture share send` refuses and sends nothing.

To send the raw recording, for example synthetic demo data, opt out
explicitly:

```bash
capture share send ./my_recording --unscrubbed --reason "synthetic demo data"
```

The opt-out is appended to `share-unscrubbed.jsonl` in Capture's data folder
(`~/Library/Application Support/ai.openadapt.capture` on macOS, or
`OPENADAPT_CAPTURE_DATA_DIR`), and the archive carries an
`UNSCRUBBED_SHARE.json` file so the receiver knows.

`send_profile=True` sends only the numbers and flags from `profiling.json`;
folder names and window titles are withheld.

Shadow-mode pattern mining, which notices repeated tasks and offers a draft,
is off by default, needs the person's acknowledgement to turn on, and keeps
no typed values. Its encrypted buffer lives in the unsynced app-data folder
and is cleared, with its key destroyed, at least every 24 hours and when
shadow mode stops. OpenAdapt never sends shadow mode data off the computer,
but OS backup, sync, and endpoint security tools can still copy local files.
See
[docs/SHADOW_MINING.md](https://github.com/OpenAdaptAI/openadapt-capture/blob/main/docs/SHADOW_MINING.md).

How Capture handles sensitive data, its licensing boundaries, and how releases
are qualified are covered in [docs/COMPLIANCE.md](https://github.com/OpenAdaptAI/openadapt-capture/blob/main/docs/COMPLIANCE.md).

## Limits

- Recording needs a signed-in desktop session and the permissions listed in
  the quick start.
- On Linux, recording needs an X11 session. Capture doesn't record under
  Wayland. It saves the text of a key only when an application receives the
  key. After a click or key that no application receives (for example,
  typing with no window focused), the rest of that recording saves each
  key's code but not its text. `scripts/linux_xvfb_quickstart.sh` runs the
  quick start on Linux in a container.
- Accessibility data appears only where the application exposes it. Across a
  remote-desktop connection, Capture sees pixels only.
- Changing a display (connecting a monitor, rotating, or changing resolution
  or scale) ends the recording. Start a new one afterward.
- These need Capture 1.3.0 or later: `capture install-ffmpeg`,
  `capture status` and `capture stop` (and `status_recording` and
  `stop_recording`), authentication handoffs, `action.after_screenshot`, and
  the `capture-*.json` files. If `pip install --upgrade openadapt-capture`
  gives you an earlier version, install from GitHub:
  `pip install "git+https://github.com/OpenAdaptAI/openadapt-capture"`.
- For web apps, use OpenAdapt's browser recorder. The Chrome extension in
  [`experimental/`](https://github.com/OpenAdaptAI/openadapt-capture/tree/main/experimental) is an unfinished prototype and isn't
  part of the package.

## Extras

| Extra | Adds |
| --- | --- |
| `transcribe-fast` | Local transcription with faster-whisper |
| `transcribe` | Local transcription with openai-whisper |
| `privacy` | `openadapt-privacy`, which `capture share send` needs to scrub a recording |
| `share` | `capture share`, using Magic Wormhole |
| `linux` | Accessibility observation on Linux (AT-SPI) |
| `all` | All of the above |

## Development

```bash
uv sync --extra dev
uv run pytest -m "not slow"
```

The slow tests record for real, so they need a desktop session and the
operating system permissions above:

```bash
uv run pytest -m slow
```

[docs/DESIGN.md](https://github.com/OpenAdaptAI/openadapt-capture/blob/main/docs/DESIGN.md) describes the recording pipeline.

## License

[MIT](https://github.com/OpenAdaptAI/openadapt-capture/blob/main/LICENSE)
