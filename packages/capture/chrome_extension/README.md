# OpenAdapt Capture Chrome extension prototype

This directory is a development prototype. It is not the supported OpenAdapt
browser recorder or a governed replay path.

Use the Playwright recorder in `openadapt-flow` for browser workflows. It can
launch a clean Chromium browser or attach to one existing signed-in local
Chromium tab. Both modes retain the compiler's event, DOM identity, exact
before/after frame, field geometry, and source-time secret-redaction contract.
See the
[`openadapt-flow` browser recording guide](https://github.com/OpenAdaptAI/openadapt-flow/blob/main/docs/BROWSER_RECORDING.md).

## Current prototype behavior

The extension can collect DOM events and visible HTML and send them to the
Capture WebSocket bridge on `localhost:8765`. It also contains legacy direct
DOM replay code.

Do not use it in a sensitive browser profile. The current implementation does
not provide these supported-path controls:

- source-time password and declared-secret exclusion;
- authenticated profile, tab, document, run, and recording-session binding;
- an acknowledged, ordered event sequence with reconnect recovery;
- exact retained frame-to-event binding in one coordinate system;
- compiler integration through the shared Flow recording schema; or
- governed replay with identity, policy, fresh-frame, and effect checks.

## Promotion contract

This extension can become a supported alternate acquisition transport. It
must first:

1. Emit the shared Flow event and evidence schema.
2. Remove direct replay. All execution must use the governed Flow runtime.
3. Exclude secrets before an event leaves the content script.
4. Authenticate and bind the browser profile, tab, document, run, session, and
   monotonic event sequence.
5. Acknowledge or safely resume every event after a reconnect. It must not drop
   an event and report success.
6. Bind each action to exact before/after frames and viewport metadata.
7. Pass at least three trials for record and compile, secret exclusion,
   ambiguity refusal, reconnect behavior, and browser lifecycle preservation.

Until these items pass the same acceptance gate as the Playwright recorder,
this directory stays a prototype component. This status applies only to this
directory. It does not apply to the `openadapt-capture` package.
