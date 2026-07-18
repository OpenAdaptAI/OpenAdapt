# OpenAdapt Capture

[![Build Status](https://github.com/OpenAdaptAI/openadapt-capture/actions/workflows/test.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-capture/actions/workflows/test.yml)
[![PyPI version](https://img.shields.io/pypi/v/openadapt-capture.svg)](https://pypi.org/project/openadapt-capture/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Lifecycle: Experimental.** OpenAdapt Capture records native mouse, keyboard,
and screen activity into a time-aligned local capture session. Its current
product role is the optional desktop recorder used by
[`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow), OpenAdapt's
workflow compiler and governed runtime.

Start with the [OpenAdapt documentation](https://docs.openadapt.ai/) if you want
to record, compile, verify, and replay a workflow.

## Where it fits

| Recording path | Current implementation |
| --- | --- |
| Windows and RDP demonstrations | `openadapt-capture` records native input and screen video; `openadapt-flow` converts the session into compiler input. |
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

Audio and individual images are optional.

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
