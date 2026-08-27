# OpenAdapt Capture

OpenAdapt Capture records native mouse, keyboard, and screen activity into a
time-aligned local session. It is the cross-platform desktop recorder used by
[`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow).

> **Product state:** An exact Capture release enters Production only through an
> active signed, expiring, and revocable release admission. A missing, expired,
> revoked, mismatched, or unverifiable admission produces **not actively
> admitted**. The validator doesn't restore an older admission or assign a
> fallback lifecycle label. Check the
> [current signed Production record](https://docs.openadapt.ai/production-lifecycle.json).

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
effect verification. Every substrate is first-class.

Each execution surface keeps its own evidence and deployment boundary. Browser
recording stays in Flow's Playwright path. Native desktop recording uses this
package. RDP and Citrix/VDI recording remains externally pixel-based and needs
qualification for the exact workflow, application, environment, identity, and
effect contract.

The packages in the stack:

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
| Windows, macOS, and Linux demonstrations | `openadapt-capture` records native input and action-gated screen video; Windows can also retain action-time UI Automation evidence. `openadapt-flow` converts the session into compiler input. |
| RDP and Citrix/VDI demonstrations | `openadapt-capture` records the selected client window in its own pixel space. The remote application remains externally black-box, and `openadapt-flow` converts the session into compiler input. |
| Browser demonstrations | `openadapt-flow` uses its Playwright recorder. It can launch Chromium or attach to one existing signed-in local Chromium tab. It does not require this package. |
| Chrome extension in this repository | Repository-only prototype. Its bridge and legacy direct replay are excluded from the wheel and source archive. It is not the supported recorder. |

The supported browser path stays inside `openadapt-flow`. Playwright owns the
browser context and can bind DOM identity, field geometry, ordered before/after
frames, and source-time secret redaction to one recording contract. A Chrome
extension cannot guarantee that contract across browser profiles, extension
permissions, browser-internal pages, and process or tab disconnects.

The extension remains useful as a research observer and as a possible future
source of optional DOM evidence. It should become a supported auxiliary
observer only after it emits the shared event schema, has a fail-closed
connection and permission contract, redacts secret fields before persistence,
and passes the same compiler qualification as the Playwright path. It should
not replace the Playwright recorder merely to make the package layout uniform,
create a second compiler format, or bypass governed replay. Flow supports an
existing authenticated browser session through its local-loopback CDP attach
mode.

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
# The ready message prints the exact session ID.

# From another terminal:
capture status --session-id SESSION_ID
capture stop --session-id SESSION_ID

capture info ./my-capture
```

The recorder creates its control endpoint before it reports ready. The endpoint
listens on IPv4 loopback only. Each request and response uses an authenticated
session capability. Capture stores that capability only in an owner-only
runtime file (`0700` directory and `0600` file on macOS/Linux; a protected
current-user owner and DACL on Windows). Capture removes and verifies the
absence of macOS extended ACL entries. The capability does not enter command
arguments, logs, or the capture directory.

`capture stop` binds the request to the exact session ID, process ID, and
process start identity. It waits for producer and writer shutdown, reconciles
producer counts with committed rows, checks database and relationship integrity,
validates replay-relevant events, and writes atomic terminal metadata before it
returns success. Repeated stop requests share one finalization result. A timeout,
worker failure, invalid capability, stale process, or ambiguous set of active
sessions returns non-success. A crash leaves `capture-state.json` incomplete;
the next authenticated discovery removes the stale runtime descriptor only after
it proves that the bound process instance is no longer live.

Launchers and embedded clients use the public Python contract instead of
reading recorder internals:

```python
from openadapt_capture import status_recording, stop_recording

current = status_recording(session_id)
completed = stop_recording(session_id, timeout=60)
assert completed.complete and completed.integrity_verified
```

Or inspect processed actions in Python:

```python
from openadapt_capture import CaptureSession

with CaptureSession.load("./my-capture") as capture:
    for action in capture.actions():
        print(action.timestamp, action.type, action.x, action.y)
        print(action.structural_observation)
        frame = action.screenshot
```

A capture normally contains:

```text
my-capture/
├── capture-state.json
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

## Native structural observations

On Windows, the recorder can retain a versioned UI Automation observation
beside the native action that produced it. When UIA exposes the information,
the observation includes the target's AutomationId, role/control type, name,
bounds, supported patterns, process/window identity, ancestry, and exact
candidate cardinality within the top-level window. Unavailable values remain
absent; capture never infers structural fields from coordinates or pixels.

This evidence is stored in `recording.db`, exposed on raw events and processed
`Action` objects, and remains optional so existing recordings and non-Windows
hosts continue to load unchanged. It is enabled by default on Windows and can
be disabled with
`Recorder(..., capture_structural_observations=False)`. Applications can inject
another read-only observer through `Recorder(..., structural_observer=...)`
using the public `StructuralObserver` protocol.

Accessibility text remains inside the local raw-capture boundary and is bounded
to 512 characters per field. Longer provider values are omitted rather than
truncated, so partial text is never presented to the compiler as exact identity
evidence. The versioned observation contract accepts namespaced provider IDs for
future macOS Accessibility and Linux AT-SPI observers, but this package currently
emits only `windows_uia` observations.

UIA describes the local Windows accessibility tree. It does not cross an
RDP/Citrix pixel boundary into the remote application; those demonstrations
retain window-scoped pixels and coordinates for Flow's remote visual compiler.

## Window-scoped recording

**Status: implemented, with display-free unit coverage on every
supported operating system.** The production release gate also requires live
window capture, input injection, movement, resize, video verification, and no
skipped tests on interactive macOS and Windows runners. Linux X11 has a separate
opt-in live window check. A customer RDP or Citrix deployment still requires
task- and environment-specific qualification.

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

`owner` matches the application. macOS uses the window owner name. Windows and
Linux use the process executable name. `title` optionally disambiguates among
the application's windows. Both selectors are case-insensitive substrings,
matching how `openadapt-flow` identifies the same window at replay time. The
selectors can also be set through config or environment
(`RECORD_WINDOW_OWNER` / `RECORD_WINDOW_TITLE`).

In this mode:

- **Frames are the target window's pixels.** macOS captures the window's own
  buffer (`CGWindowListCreateImage`, the identical call flow's replay uses);
  Linux X11 reads an XComposite named-window pixmap. It doesn't use a root
  screenshot, so another window cannot replace the target pixels. Windows
  grabs the window's screen region, so keep the window unoccluded.
- **Input coordinates are translated at capture time** into the captured
  frame's pixel space (`pixel = (global_point - window_origin) * scale`, the
  exact inverse of the replay mapping). Input outside the window records
  out-of-range coordinates rather than being silently clamped.
- **The window scoping is persisted**: the recording's config JSON carries the
  target, resolved window, initial bounds, fixed output viewport, current
  source viewport, scale-to-fit mapping, and content rectangle
  (`CaptureSession.window_capture`), and the window is re-resolved every
  frame with bounds changes recorded as window events, a bounds timeline
  converters can use to be exact even when the window moves.
- **Each frame has one ordered geometry identity.** Current window captures
  store the frame and its window event under the same source ordinal. An action
  stores a later ordinal and names the exact earlier pair it used. The pair
  binds the process start identity, display topology, bounds, scale, fixed
  viewport, and geometry generation. Recorder shutdown retains one final frame
  after input has stopped, so a consumer can select an exact after-action frame
  by ordinal instead of by nearest timestamp.
- **Window movement and resize are supported.** The first frame fixes the
  encoded video size. Later source frames scale to fit and letterbox into that
  viewport. Input uses the exact current bounds and content rectangle. No frame
  is silently skipped because the window changed size or moved to a display
  with a different scale.
- **Fail-loud guarantees:** recording refuses to start if the window cannot be
  resolved and captured; input arriving before the first frame is discarded
  with a warning instead of being recorded in the wrong coordinate space; a
  lost window, capture failure, or unexpected output-frame size fails the
  session instead of producing complete-looking media with an evidence gap.

Linux window mode requires an X11 session with EWMH and XComposite. Capture
won't start window mode in a native Wayland or XWayland-only session. A future
Wayland producer must bind the portal-selected window, its pixel stream, and
event-time coordinates before it can replace this refusal.

Note for converters: window-mode coordinates are already in captured-frame
pixels (`coordinate_space == "window_pixels"`); do not rescale them by
`pixel_ratio`.

After every producer and writer has stopped, `Recorder` verifies the database
and writes `capture-artifact-manifest.json` plus `capture-terminal.json`. The
terminal binds the complete artifact inventory, event counts, final source
ordinal, capture session identity, and completion time. A current native
consumer should use `CaptureSession.load_verified()`. It checks the seal, copies
the exact artifacts into a private snapshot, and opens the copied database in
SQLite immutable read-only mode. A current window capture without this seal is
incomplete and must not be compiled as native geometry evidence.

## Multiple monitors

Full-screen mode records the complete virtual desktop reported by MSS, not
only the primary monitor. Capture stores its origin, fixed viewport, monitor
count, and privacy-safe monitor rectangles as
`CaptureSession.desktop_capture`. Global input is translated into this exact
combined-frame coordinate space. This includes a secondary monitor whose
native coordinates have a negative origin.

The release qualification requires at least two real monitors on each
interactive operating-system runner. It checks the topology and then runs the
native screen and input tests. Downstream converters must not apply the legacy
display-ratio scale when
`coordinate_space == "virtual_desktop_pixels"`.

The monitor topology is fixed for one recording. Connecting, disconnecting,
rotating, or changing the resolution or scale of a display changes the encoded
frame contract. Capture fails the session if that occurs. Start a new recording
after a display-topology change. This boundary does not restrict movement or
resize of a recorded window across an unchanged monitor layout.

## Data and privacy boundary

A raw capture can contain everything visible on screen and everything typed,
including credentials, personal data, or protected health information. Keep the
entire capture directory inside its approved local boundary and apply an
appropriate retention policy.

Capture does not upload a session. The sharing command and profiling transfer
are the only transfers, and both are explicit opt-in operations. Installing the
`privacy` extra alone does not automatically scrub a recording.

### Audio narration

Microphone narration (`capture record --audio`, or `RECORD_AUDIO`) is **off by
default** and is the highest-risk observation this package takes. Speech has no
structure to scrub against the way a screen field does, and a voice is itself
biometric identifying data — Safe Harbor identifier #16. `openadapt-privacy`
has no audio modality, and `openadapt-flow` refuses audio artifacts outright,
so a waveform has **no sanitized derivative and can never be cleared for
egress**.

The following boundaries are enforced in code, not by convention:

- **Transcription is on-device only.** There is no remote transcription
  backend. If no local backend is installed, `--audio` and `capture transcribe`
  refuse with an install hint rather than falling back to a network recognizer.
- **`--audio` refuses before the microphone opens** when it could not
  transcribe locally, so a session is never captured that must then be thrown
  away.
- **The waveform is discarded after transcription.** Only transcript text is
  retained unless `RECORD_AUDIO_RETAIN_WAVEFORM` is explicitly enabled.
- **The transcript is never logged**, because narration can contain names,
  dates of birth, and diagnoses.
- **The HTML viewer does not embed audio by default**, since that file is
  self-contained and easy to forward.
- **`--audio` prints an explicit microphone notice** before recording starts.

Transcript text is unscrubbed free text. Treat it as at least as sensitive as
the rest of the capture, and keep it inside the same approved local boundary.
Nothing downstream consumes it today.

Structural observations can also contain sensitive control names, window
titles, and accessibility ancestry. They stay in the same local raw-capture
boundary. `openadapt-flow` still refuses desktop `--secret` authoring until its
source-time field-redaction contract can prove that sensitive values were not
retained. Review the desktop guide before recording sensitive workflows.

The repository-only Chrome extension prototype can observe pages across its
configured host permissions. Its development bridge can emit DOM text and
keyboard events to an unauthenticated local WebSocket and contains legacy
direct DOM replay. These files are excluded from the package wheel and source
archive. The production Capture API does not export the bridge, and the former
`browser_events=True` opt-in fails before it binds a listener. The prototype
does not provide source-time secret exclusion, authenticated
profile/tab/document/session binding, acknowledged ordered delivery, or exact
frame-to-event evidence. Treat it as development code. Do not deploy it in a
sensitive browser profile.

Use Flow's supported attach recorder when an existing SSO or 2FA browser
session is required. See the
[`openadapt-flow` browser recording guide](https://github.com/OpenAdaptAI/openadapt-flow/blob/main/docs/BROWSER_RECORDING.md).

## Current limitations

- Native recording requires a visible user session plus the operating system's
  screen-recording and input-monitoring permissions.
- Native Windows capture retains UIA evidence when the application exposes it;
  opaque remote applications still require Flow's visual and OCR bindings.
- The Flow adapter compiles left and right clicks, left-button drags, typed
  text, named keys, modifier chords, and scrolling. It rejects unsupported
  input such as middle clicks, non-left-button drags, malformed shortcuts, and
  unmapped keys instead of silently compiling an incomplete workflow.
- Display hot-plug, rotation, resolution changes, and scale changes require a
  new recording because one media stream has one fixed virtual-desktop viewport.
- Browser-extension installation, bridge code, and direct replay are not part
  of the published Capture artifacts or supported browser path. Promotion
  requires the shared Flow schema, source-time secret exclusion, authenticated
  and sequenced delivery, exact frame binding, compiler integration, and
  removal of direct replay.

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

Both transcription extras are installed by the user into their own
environment; neither is vendored into this MIT wheel. Note that
`transcribe-fast` pulls `av` (PyAV), whose published wheels bundle GPL-licensed
`libx264`/`libx265` binaries. That is acceptable for a user-side `pip install`,
but such an artifact must not be frozen into a first-party installer, sidecar,
or image. Packaging work that bundles a transcription backend should prefer an
MIT backend with a clean dependency tree and must inspect the built archive.

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

The Linux X11 window check also requires an explicit target:

```bash
OPENADAPT_CAPTURE_LINUX_WINDOW_QUALIFICATION=1 \
OPENADAPT_WINDOW_SMOKE_OWNER=citrix \
uv run pytest tests/test_window_capture_linux.py -m slow
```

## License

[MIT](LICENSE)
