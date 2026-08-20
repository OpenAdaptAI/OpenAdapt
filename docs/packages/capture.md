# openadapt-capture

**Lifecycle: Experimental**, as recorded in the canonical
[organization lifecycle registry](https://github.com/OpenAdaptAI/.github/blob/main/REPOSITORY_LIFECYCLE.md).
`openadapt-capture` is the component OpenAdapt uses for native screen, mouse,
keyboard, timing, window-scope, and media capture.

Repository:
[`OpenAdaptAI/openadapt-capture`](https://github.com/OpenAdaptAI/openadapt-capture)

## Install

```bash
python -m pip install 'openadapt[capture]'
```

Add the replay surface extra when the workflow needs one:

```bash
python -m pip install 'openadapt[capture,windows]'
python -m pip install 'openadapt[capture,macos]'
python -m pip install 'openadapt[capture,linux]'
python -m pip install 'openadapt[capture,rdp]'
```

## Product role

Flow uses Capture for native and remote-display demonstrations. The Flow
adapter consumes Capture's public `CaptureSession` API and converts each
action, aligned frame, window hint, and optional structural observation into
the recording contract that the compiler accepts.

```bash
openadapt flow record --backend macos --window TextEdit --out rec
openadapt flow compile rec --out bundle --name my-task
```

Use the exact backend and target options from the installed engine:

```bash
openadapt flow record --help
openadapt flow replay --help
```

Capture needs an interactive desktop and the applicable operating system
permissions. RDP and Citrix capture the visible local client window. They do
not claim access to a remote accessibility tree.

## Direct raw session

The compatibility launcher can start a raw Capture session:

```bash
openadapt capture start --name my-task
openadapt capture status --session-id SESSION_ID
openadapt capture stop --session-id SESSION_ID
```

The start command prints the exact session ID after the recorder is ready.
`status` reads the authenticated state of that process. `stop` sends an
authenticated owner-only loopback request, waits for the writers to stop, and
returns success only after Capture verifies the finalized session. You can
omit `--session-id` only when one recorder is active. Ctrl-C in the recorder
terminal remains available. Use `openadapt flow record` when the output must
compile directly.

## Storage and privacy

A session stores structured events plus time-aligned media in its capture
directory. Consumers must use `CaptureSession.load()`, `.actions()`, and
`.get_frame_at()` instead of reading the private database schema.

Raw screenshots and typed values can contain sensitive data. Keep the source
inside its trusted boundary. Compilation does not make it safe to upload. Use
the local sanitize, review, and exact-hash approval path before any artifact
crosses a boundary.

## Evidence boundary

Required CI validates the released Capture API, action conversion, frame
alignment, coordinate scaling, secret exclusion, structural observations, and
all Flow desktop selectors. Native actuation evidence remains bound to a named
task, application, operating system, and verifier. Review the
[current capability matrix](https://docs.openadapt.ai/get-started/what-works-today/)
before a deployment claim.
