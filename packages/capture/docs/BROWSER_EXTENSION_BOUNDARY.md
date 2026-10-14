# Why the browser recorder lives in Flow, not here

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

## The prototype's exposure

The repository-only Chrome extension prototype can observe pages across its
configured host permissions. Its development bridge can emit DOM text and
keyboard events to an unauthenticated local WebSocket, and it contains legacy
direct DOM replay. Those files are excluded from the published wheel and source
archive. The production Capture API doesn't export the bridge, and the former
`browser_events=True` opt-in fails before it binds a listener.

The prototype provides no source-time secret exclusion, no authenticated
profile/tab/document/session binding, no acknowledged ordered delivery, and no
exact frame-to-event evidence. Treat it as development code. Don't run it in a
sensitive browser profile.

Use Flow's attach recorder when you need an existing SSO or 2FA browser
session: the
[browser recording guide](https://github.com/OpenAdaptAI/openadapt-flow/blob/main/docs/BROWSER_RECORDING.md).
