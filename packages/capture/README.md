# openadapt-capture

[![Build Status](https://github.com/OpenAdaptAI/openadapt-capture/actions/workflows/test.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-capture/actions/workflows/test.yml)
[![PyPI version](https://img.shields.io/pypi/v/openadapt-capture.svg)](https://pypi.org/project/openadapt-capture/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Records what you do on a Windows, macOS, or Linux desktop: mouse, keyboard, and
screen, written into one local session where the events and the video frames
share a clock. [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow)
reads that session and compiles it into a replayable program.

If you want to record, compile, and replay a workflow, you want Flow, and this
package comes along as an extra. Install it on its own when you need the
recorder as a library.

[Documentation](https://docs.openadapt.ai) ·
[Flow](https://github.com/OpenAdaptAI/openadapt-flow) ·
[Window capture](https://github.com/OpenAdaptAI/openadapt-capture/blob/main/docs/WINDOW_CAPTURE.md)

## With OpenAdapt

```bash
pip install "openadapt-flow[capture]"

openadapt-flow record --backend windows --out recording --task "Describe the workflow"
# Do the task, then press Ctrl-C.

openadapt-flow compile recording --out bundle --name my-workflow
```

Use `--backend rdp` to record inside an RDP client's pixel space. Replay setup
and per-substrate requirements are in the
[desktop recording guide](https://github.com/OpenAdaptAI/openadapt-flow/blob/main/docs/desktop/RECORDING.md).

## As a library

```bash
pip install openadapt-capture
```

```bash
capture record ./my-capture --description "Describe the workflow"
# The ready message prints the session ID.

capture info ./my-capture
```

`capture status`, `capture stop`, `capture install-ffmpeg`, and the
`status_recording` / `stop_recording` Python contract below are on `main` and
ship in 1.3.0. They are not in the published 1.2.2 wheel, so install from
source until 1.3.0 reaches PyPI:

```bash
pip install "openadapt-capture @ git+https://github.com/OpenAdaptAI/openadapt-capture"
```

```bash
# From another terminal, against a running capture:
capture status --session-id SESSION_ID
capture stop --session-id SESSION_ID
```

Or drive it from Python:

```python
from openadapt_capture import CaptureSession

with CaptureSession.load("./my-capture") as capture:
    for action in capture.actions():
        print(action.timestamp, action.type, action.x, action.y)
        print(action.structural_observation)
        frame = action.screenshot
```

A launcher or an embedded client should use the public contract rather than
reading recorder internals:

```python
from openadapt_capture import status_recording, stop_recording

current = status_recording(session_id)
completed = stop_recording(session_id, timeout=60)
assert completed.complete and completed.integrity_verified
```

A finished capture looks like this:

```text
my-capture/
├── capture-state.json
├── recording.db
├── oa_recording-*.mp4
└── profiling.json
```

`capture stop` is bound to the exact session ID, process ID, and process start
identity. It waits for the producer and the writer to shut down, reconciles
producer counts against committed rows, checks database integrity, validates
the replay-relevant events, and writes atomic terminal metadata before it
returns success. A timeout, a worker failure, an invalid capability, a stale
process, or an ambiguous set of active sessions all return non-success. Repeat
calls share one finalization result.

The control endpoint listens on IPv4 loopback only, and every request carries
an authenticated session capability that lives in an owner-only runtime file
(`0700`/`0600` on macOS and Linux, a protected current-user DACL on Windows).
On macOS it also removes extended ACL entries and verifies they are absent. The
capability never reaches command arguments, logs, or the capture directory.

## FFmpeg

Recording video needs an FFmpeg executable, and `capture install-ffmpeg` gets
you one. Video is the default evidence format, so most people need it.

```bash
capture install-ffmpeg
```

That downloads a single pinned build for your platform, checks its SHA-256
against a digest compiled into this package, and installs it under your user
data directory. Nothing is made executable until every digest matches. Run it
with `--dry-run` to print the exact URL, digest, and destination without
fetching anything, and `capture uninstall-ffmpeg` to remove it again. Builds
are pinned for macOS on Apple silicon and Intel, Linux x86-64, and Windows
x86-64. On anything else, supply your own executable.

The wheel and the source distribution carry no FFmpeg bytes, and Capture
downloads nothing unless you run that command. Licensing is the reason. This
package is MIT and FFmpeg is not, so shipping FFmpeg inside it would relicense
the package. The pinned build is LGPL-2.1-or-later, configured with
`--disable-gpl`, `--disable-nonfree`, and `--disable-version3`. FFmpeg's own
`LICENSE.md` covers the GPL components it leaves out: "None of these parts are
used by default." The install puts that licence text beside the binaries, and
writes a receipt naming the archive, its digest, and the matching upstream
source tarball.

An FFmpeg you already configured keeps priority. Capture resolves, in order,
`Recorder(ffmpeg_path=...)` or `OPENADAPT_FFMPEG_PATH`, then
`OPENADAPT_DESKTOP_FFMPEG_PATH`, then Desktop's `ffmpeg.json` provision
manifest, then `PATH`, and the installed runtime last. Point
`OPENADAPT_FFMPEG_PATH` at the installed one to move it to the front.

Recording runs a real encode-and-decode probe first, and refuses before the
input listeners start if the executable, the codec, or the PNG verification
path is missing. A minimal runtime has to supply raw-video input through a
pipe, the chosen video encoder, MP4 demuxing and muxing, PNG decoding and
encoding, the `image2pipe` muxer, and the `select` video filter. The pinned
build and Desktop's runtime both cover that closure.

Frames stream from memory straight to FFmpeg. A missing integer PTS slot reuses
the preceding frame, so the encode is deterministic no matter what the
scheduler does, and a compact MP4 metadata box keeps the logical capture-frame
timestamps that nearest-frame extraction needs. No intermediate screenshot
sequence gets written.

## Structural observations

Beside each action the recorder can retain a versioned native accessibility
observation, from Windows UI Automation, macOS Accessibility, or Linux AT-SPI.
Where the provider exposes them it records the target identifier, the role,
the name, the bounds, the supported actions, the process and window identity,
and the ancestry. Windows UIA also records how many candidates matched inside
the top-level window. A value the provider doesn't expose stays absent: Capture
never infers a structural field from coordinates or pixels. The provider name
is `windows_uia`, `macos_ax`, or `linux_atspi`, and the field is optional, so
an older recording still loads unchanged.

It's on by default wherever a provider exists. Turn it off with
`Recorder(..., capture_structural_observations=False)`, or inject your own
read-only observer through the `StructuralObserver` protocol.

Accessibility text is capped at 512 characters per field, and a longer value is
omitted rather than truncated, so the compiler never sees half a string and
treats it as exact identity evidence.

macOS needs Accessibility permission. Linux needs a live AT-SPI bus and the
system typelib, so install your distribution's PyGObject packages and
`gir1.2-atspi-2.0` first (the
[PyGObject install guide](https://pygobject.gnome.org/getting_started.html) has
the current names), then:

```bash
pip install "openadapt-capture[linux]"
```

The provider describes the local accessibility tree. It cannot see controls on
the far side of an RDP or Citrix pixel boundary, and those demonstrations fall
back to window-scoped pixels and coordinates for Flow's visual compiler.

## Recording one window

By default the recorder takes the whole virtual desktop. Window-scoped mode
records a single window in that window's own pixel space, which is what
remote-display demonstrations need, because Flow's `rdp_window` replay drives
the client window's pixels directly:

```python
from openadapt_capture import Recorder

with Recorder(
    "./my-capture",
    task_description="Demonstrate the workflow",
    window={"owner": "Parallels", "title": None},  # substring match
) as recorder:
    input("Perform the task, then press Enter...")
```

Coordinates are translated at capture time into the frame's pixel space, the
window is re-resolved every frame, movement and resize are handled by scaling
into a fixed viewport, and a lost window fails the session rather than
producing media that looks complete but has an evidence gap. The full contract,
including the multi-monitor rules and the coordinate-space flags converters
must respect, is in [docs/WINDOW_CAPTURE.md](https://github.com/OpenAdaptAI/openadapt-capture/blob/main/docs/WINDOW_CAPTURE.md).

Linux window mode needs X11 with EWMH and XComposite. It refuses to start under
native Wayland or XWayland-only.

## What a capture contains, and where it must stay

A raw capture can hold everything visible on screen and everything typed:
credentials, personal data, protected health information. Keep the whole
directory inside its approved local boundary and give it a retention policy.

Capture doesn't upload a session. The sharing command and the profiling
transfer are the only two transfers and both are explicit. Installing the
`privacy` extra does not scrub anything by itself.

**Microphone narration** (`capture record --audio`, or `RECORD_AUDIO`) is off
by default and it's the riskiest thing this package can observe. Speech has no
structure to scrub against the way a screen field does, and a voice is itself
biometric identifying data, Safe Harbor identifier #16. `openadapt-privacy` has
no audio modality and `openadapt-flow` refuses audio artifacts, so a waveform
has no sanitized derivative and can never be cleared for egress. These
boundaries are in code, not convention:

- Transcription is on-device. There is no remote backend, and with no local
  backend installed, `--audio` and `capture transcribe` refuse with an install
  hint instead of reaching for a network recognizer.
- `--audio` refuses before the microphone opens if it couldn't transcribe
  locally, so you never capture a session you then have to throw away.
- The waveform is discarded after transcription unless
  `RECORD_AUDIO_RETAIN_WAVEFORM` is set.
- The transcript is never logged. Narration carries names, dates of birth, and
  diagnoses.
- The HTML viewer doesn't embed audio by default, because that file is
  self-contained and easy to forward.
- `--audio` prints a microphone notice before recording starts.

Transcript text is unscrubbed free text. Treat it as at least as sensitive as
the rest of the capture. Nothing downstream reads it today.

Structural observations carry control names, window titles, and accessibility
ancestry, and stay in the same local boundary. Flow still refuses desktop
`--secret` authoring until its source-time field-redaction contract can prove
the sensitive value was never retained.

The Chrome extension in this repository is a prototype. It's excluded from the
published wheel and source archive, it has no source-time secret exclusion, and
its development bridge speaks to an unauthenticated local WebSocket. Don't run
it in a sensitive browser profile. Why the supported browser recorder lives in
Flow instead: [docs/BROWSER_EXTENSION_BOUNDARY.md](https://github.com/OpenAdaptAI/openadapt-capture/blob/main/docs/BROWSER_EXTENSION_BOUNDARY.md).

## Limits

- Native recording needs a visible user session plus the operating system's
  screen-recording and input-monitoring permissions.
- Accessibility evidence appears only where the application and the local
  provider expose it. An opaque remote application still needs Flow's visual
  and OCR bindings.
- The Flow adapter compiles left and right clicks, left-button drags, typed
  text, named keys, modifier chords, and scrolling. Anything else, including
  middle clicks, non-left-button drags, malformed shortcuts, and unmapped keys,
  is rejected rather than compiled into an incomplete workflow.
- Display hot-plug, rotation, resolution change, and scale change all end a
  recording, because one media stream has one fixed virtual-desktop viewport.
  Start a new recording afterwards.
- The browser extension is not a supported recorder and its artifacts are not
  published.

## Product state

An exact Capture release reaches Production only through an active signed,
expiring, revocable release admission. Missing, expired, revoked, mismatched,
or unverifiable, and the release is **not actively admitted**. The validator
won't fall back to an older admission or hand out a lifecycle label instead.
The current state is machine-readable in the
[signed production record](https://docs.openadapt.ai/production-lifecycle.json).

Evidence behind what each substrate can claim: the
[repository lifecycle registry](https://github.com/OpenAdaptAI/.github/blob/main/REPOSITORY_LIFECYCLE.md)
and [Flow's product status](https://github.com/OpenAdaptAI/openadapt-flow/blob/main/docs/PRODUCT_STATUS.md).

## Extras

| Extra | Adds |
| --- | --- |
| `transcribe-fast` | Local faster-whisper transcription |
| `transcribe` | Local openai-whisper transcription |
| `privacy` | The `openadapt-privacy` dependency, for explicit integrations. No automatic scrubbing. |
| `share` | Explicit Magic Wormhole transfer |
| `linux` | Native Linux AT-SPI structural observation |
| `all` | Everything in this table |

Neither transcription extra is vendored into this MIT wheel; you install them
into your own environment. Watch `transcribe-fast`: it pulls PyAV, whose
published wheels bundle GPL-licensed `libx264` and `libx265` binaries. Fine for
a user-side `pip install`, not fine frozen into a first-party installer,
sidecar, or image. Packaging work that bundles a transcription backend should
prefer an MIT backend with a clean dependency tree, and should inspect the
built archive.

## Development

```bash
uv sync --dev
uv run pytest -m "not slow"
```

The slow native-capture tests need a visible session and OS permissions:

```bash
uv run pytest -m slow
```

The Linux X11 window check needs an explicit target:

```bash
OPENADAPT_CAPTURE_LINUX_WINDOW_QUALIFICATION=1 \
OPENADAPT_WINDOW_SMOKE_OWNER=citrix \
uv run pytest tests/test_window_capture_linux.py -m slow
```

## License

[MIT](LICENSE)
