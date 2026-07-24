# OpenAdapt Capture

> [!IMPORTANT]
> **Status: Experimental.** OpenAdapt Capture records native mouse, keyboard,
> and screen activity into a time-aligned local capture session. Its current
> product role is the optional cross-platform desktop recorder used by
> [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow).
>
> The OpenAdapt product is the demonstration compiler,
> [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow), installed
> via the [`OpenAdapt`](https://github.com/OpenAdaptAI/OpenAdapt) launcher
> (`pip install openadapt`): it compiles a demonstrated GUI workflow into a
> deterministic, locally executable program. Healthy runs make no model calls,
> and it halts instead of guessing when verification fails. Lifecycle labels for
> every repository are in the
> [repository lifecycle registry](https://github.com/OpenAdaptAI/.github/blob/main/REPOSITORY_LIFECYCLE.md).

[![Build Status](https://github.com/OpenAdaptAI/openadapt-capture/actions/workflows/test.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-capture/actions/workflows/test.yml)
[![PyPI version](https://img.shields.io/pypi/v/openadapt-capture.svg)](https://pypi.org/project/openadapt-capture/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Cross-platform local desktop recording: native mouse, keyboard, and screen
activity captured into a time-aligned local session that the compiler turns into
deterministic replay input. Local-first by default; a raw capture never leaves
the machine unless you run an explicit opt-in command.

Start with the [OpenAdapt documentation](https://docs.openadapt.ai/) if you want
to record, compile, verify, and replay a workflow.

## The OpenAdapt stack

OpenAdapt is a governed demonstration compiler: record a workflow once, compile
the recording into a deterministic program, and replay that program with zero
model calls on the healthy path. When the live screen does not match what was
demonstrated it halts instead of guessing, using identity gates and independent
effect verification. Every substrate is first-class: web and desktop recording
are validated, RDP and Windows replay are early, and Citrix is exploratory.

| Package | Role |
| --- | --- |
| [`openadapt`](https://github.com/OpenAdaptAI/OpenAdapt) | Launcher and installer (`pip install openadapt`) |
| [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow) | Records, compiles, verifies, and replays workflows |
| **`openadapt-capture`** | Cross-platform local desktop recording (this package) |
| [`openadapt-types`](https://github.com/OpenAdaptAI/openadapt-types) | Canonical action and UI-state schema |
| [`openadapt-grounding`](https://github.com/OpenAdaptAI/openadapt-grounding) | Local OCR text-anchoring plus optional model grounding |
| [`openadapt-privacy`](https://github.com/OpenAdaptAI/openadapt-privacy) | PHI/PII detection and redaction |

Documentation for the whole stack lives at
[docs.openadapt.ai](https://docs.openadapt.ai).

## Where it fits

| Recording path | Current implementation |
| --- | --- |
| Windows and RDP demonstrations | `openadapt-capture` records native input and action-gated screen video; `openadapt-flow` converts the session into compiler input. |
| Browser demonstrations | `openadapt-flow` records its Playwright browser directly. It does not require this package. |
| Chrome extension in this repository | Experimental DOM-capture code for development; it is not the supported web recorder or governed replay path. |

The browser path stays inside `openadapt-flow` because the compiler needs
ordered before/after frames, page state, secret-field redaction, and events in
its own recording format. The extension captures useful DOM context, but it
does not provide that end-to-end contract.

## Use it with OpenAdapt

Install the compiler with the optional native recorder:

```bash
pip install "openadapt-flow[capture]"
```

Record a desktop demonstration, then compile it:

```bash
openadapt-flow record --backend windows --out recording --task "Describe the workflow"
# Perform the workflow, then press Ctrl-C.

openadapt-flow compile recording --out bundle --name my-workflow
```

Use `--backend rdp` when recording inside the RDP client pixel space. Replay
setup and substrate-specific requirements are documented in the
[`openadapt-flow` desktop recording guide](https://github.com/OpenAdaptAI/openadapt-flow/blob/main/docs/desktop/RECORDING.md).

## Use it as a library

Install the capture package directly:

```bash
pip install openadapt-capture
```

Record from the command line:

```bash
capture record ./my-capture --description "Describe the workflow"
# Press Ctrl-C to stop.

capture info ./my-capture
```

Or inspect processed actions in Python:

```python
from openadapt_capture import CaptureSession

with CaptureSession.load("./my-capture") as capture:
    for action in capture.actions():
        print(action.timestamp, action.type, action.x, action.y)
        frame = action.screenshot
```

A capture normally contains:

```text
my-capture/
├── recording.db
├── oa_recording-*.mp4
└── profiling.json
```

Video remains the default evidence format. Capture streams in-memory RGB frames
directly to a separately provisioned FFmpeg executable while recording. Missing
integer PTS slots reuse the preceding frame, so encoding is deterministic and
independent of scheduler or queue latency. A compact MP4 metadata box retains
the logical capture-frame timestamps used by nearest-frame extraction. Capture
then verifies and atomically promotes the MP4; no intermediate screenshot
sequence is written. Capture never downloads, bundles, or links FFmpeg/PyAV. Set
`OPENADAPT_FFMPEG_PATH`, pass `Recorder(ffmpeg_path=...)`, use Desktop's
user-data `ffmpeg.json` provision manifest, or place `ffmpeg` and `ffprobe` on
`PATH`. Recording performs a real encode-and-decode probe and refuses before
input listeners start if the selected executable, codec, or PNG verification
path is unavailable. A minimal managed runtime must provide raw-video input
through a pipe, the selected video encoder, MP4 demuxing/muxing, PNG
decoding/encoding, the `image2pipe` muxer, and the `select` video filter;
Desktop provisions and probes that exact closure.

## Window-scoped recording

**Status: implemented and unit-proven on all CI platforms; live-validated
end to end on macOS (frames, translated coordinates, bounds timeline, and
video verified against a real window on a real display). Windows uses a
Win32 + `mss` region grab and is exercised by the same unit suite; its live
smoke test awaits an interactive Windows desktop. Not yet validated against
a Parallels/Citrix client window specifically.**

By default the recorder captures the full screen. Window-scoped mode records
ONE window in that window's own pixel space. This is the mode built for
remote-display demonstrations (Parallels, Citrix Workspace, Microsoft Remote
Desktop), where `openadapt-flow`'s `rdp_window` replay drives the client
window's pixels directly. Recording scoped to the same window removes the
full-screen-vs-window coordinate mismatch at the source:

```python
from openadapt_capture import Recorder

with Recorder(
    "./my-capture",
    task_description="Demonstrate the workflow",
    window={"owner": "Parallels", "title": None},  # substring match
) as recorder:
    input("Perform the task, then press Enter...")
```

`owner` matches the application (macOS: window owner name; Windows: process
executable name) and `title` optionally disambiguates among its windows; both
are case-insensitive substrings, mirroring how `openadapt-flow`'s
remote-display backend identifies the same window at replay time. The
selectors can also be set via config/environment
(`RECORD_WINDOW_OWNER` / `RECORD_WINDOW_TITLE`).

In this mode:

- **Frames are the target window's pixels.** macOS captures the window's own
  buffer (`CGWindowListCreateImage`, the identical call flow's replay uses);
  Windows grabs the window's screen region, so keep the window unoccluded.
- **Input coordinates are translated at capture time** into the captured
  frame's pixel space (`pixel = (global_point - window_origin) * scale`, the
  exact inverse of the replay mapping). Input outside the window records
  out-of-range coordinates rather than being silently clamped.
- **The window scoping is persisted**: the recording's config JSON carries the
  target, resolved window, initial bounds, scale, and viewport
  (`CaptureSession.window_capture`), and the window is re-resolved every
  frame with bounds changes recorded as window events, a bounds timeline
  converters can use to be exact even when the window moves.
- **Fail-loud guarantees:** recording refuses to start if the window cannot be
  resolved and captured; input arriving before the first frame is discarded
  with a warning instead of being recorded in the wrong coordinate space; a
  mid-recording window *resize* skips unencodable video frames loudly
  (screenshots and the bounds timeline stay exact), so avoid resizing the
  target during a demonstration.

Note for converters: window-mode coordinates are already in captured-frame
pixels (`coordinate_space == "window_pixels"`); do not rescale them by
`pixel_ratio`.

## Data and privacy boundary

A raw capture can contain everything visible on screen and everything typed,
including credentials, personal data, or protected health information. Keep the
entire capture directory inside its approved local boundary and apply an
appropriate retention policy.

Capture does not upload a session by default. The sharing command, remote
transcription, and profiling transfer are explicit opt-in operations. Installing
the `privacy` extra alone does not automatically scrub a recording.

The current desktop-to-Flow conversion has no field geometry for reliable
secret redaction and no live UIA locator. `openadapt-flow` therefore refuses its
desktop `--secret` option, and converted desktop workflows rely on retained
visual evidence unless a separate structural recording path arms them. Review
the desktop guide before recording sensitive workflows.

The experimental Chrome extension can observe pages across its configured host
permissions and can emit DOM text and keyboard events to a local WebSocket.
Treat it as development code; do not deploy it in a sensitive browser profile.

## Current limitations

- Native recording requires a visible user session plus the operating system's
  screen-recording and input-monitoring permissions.
- Desktop capture records pixels and coordinates, not a structural accessibility
  locator for each demonstrated target.
- The Flow adapter rejects unsupported input such as drag, non-left-click, and
  modifier-chord actions instead of silently compiling an incomplete workflow.
- Browser-extension installation, security hardening, and compiler integration
  are not part of the current product path.

See the organization-wide
[repository lifecycle registry](https://github.com/OpenAdaptAI/.github/blob/main/REPOSITORY_LIFECYCLE.md)
and [`openadapt-flow` product status](https://github.com/OpenAdaptAI/openadapt-flow/blob/main/docs/PRODUCT_STATUS.md)
for the evidence behind current maturity labels.

## Optional extras

| Extra | Adds |
| --- | --- |
| `transcribe-fast` | Local faster-whisper transcription |
| `transcribe` | Local openai-whisper transcription |
| `privacy` | `openadapt-privacy` dependency for explicit integrations; no automatic scrubbing |
| `share` | Explicit Magic Wormhole transfer |
| `all` | All optional dependencies |

## Development

```bash
uv sync --dev
uv run pytest -m "not slow"
```

Slow native-capture tests require a visible session and operating-system
permissions:

```bash
uv run pytest -m slow
```

## License

[MIT](LICENSE)
