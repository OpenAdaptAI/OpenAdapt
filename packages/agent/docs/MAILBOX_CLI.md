# Pip mailbox client: ChatGPT.com drives this computer

One sentence: **OpenAdapt is installed on this computer, so an agent can drive only through OpenAdapt.**

ChatGPT.com and Claude.ai cannot talk to localhost. They call `https://openadapt.ai/mcp`. A process on the laptop must poll `/j/{id}/runner/poll` with `wait_seconds: 0`, claim `oab_`, Allow per OAuth `sub`, pause so the person signs in **in the real app**, and `record_observed` on Continue. Desktop already does that. `openadapt-agent serve --authoring` is local stdio for Claude Code. That never reaches ChatGPT.com. `pip install` alone is not a listener.

## Command

Canonical in this package:

```bash
openadapt-agent authoring connect '<openadapt://runner?pack=…&bind=oab_…&origin=https://openadapt.ai>'
```

Playwright web (fresh Chromium, empty cookies):

```bash
openadapt-agent authoring connect '<runner-link>' --url https://example.invalid/app
```

Rejected alternative: `openadapt-agent serve --authoring --mailbox`. `serve` is stdio MCP. Mixing it with an outbound poll loop would look like this package grew a hosted transport. The mailbox client is a separate verb.

Job-page / meta-package alias (not implemented here; `openadapt` wraps this package):

```bash
openadapt connect '<runner-link-or-pack-url>'
```

The pack “Connect this computer” control should offer that command next to **Open OpenAdapt**. Do not implement the web page in this repository. GET `/j/{id}` stays presence. It is not actuation.

## Shared engine

The protocol is Desktop’s authoring mailbox (openadapt-desktop PR 154, `engine/authoring_runner.py`):

| Step | Wire |
|---|---|
| Parse | `openadapt://runner` only. Fields: `pack`, `bind`, `origin`. Origin pin `https://openadapt.ai`. |
| Claim | `POST /j/{id}/runner/claim` `{ bind: "oab_…" }` → 201 `{ leaseSecret: "oals_…", lease_s: 900 }` |
| Poll | `POST /j/{id}/runner/poll` `Authorization: Bearer oals_…` `{ wait_seconds: 0, lease_seconds: 900 }`. Empty 204. Sleep 1 s locally. Do not copy hosted-runner `wait=25`. |
| Allow | Terminal `y/n` for **that** pending `bind_pack` `sub`. `POST /j/{id}/runner/allow` `{ command_id }`. |
| Continue | `Recorder.record_observed`. Never `type_text` for secrets or for text the person already typed. |

This package prefers Desktop `engine.authoring_runner.AuthoringMailboxTransport` when that module is importable (Desktop installed in-process). Otherwise it uses the copy in `openadapt_agent.mailbox` (stdlib `urllib`, outbound POST only). There is no new repository and no `control_plane` path.

Flow `AuthoringSession` / `Recorder` is the actuation engine when importable. Overlay chrome is not.

## What this CLI does

- Parse `openadapt://runner` or `https://openadapt.ai/j/{pack}` (pack URL is not enough to claim without `bind`).
- Claim `oab_`. First claim wins. Do not print `leaseSecret`.
- Poll `wait_seconds: 0`.
- Print Allow (`Allow ChatGPT to drive this job?` / replace-account copy). stdin `y/n`.
- Pause: print `Sign in in the app, then press Enter`. Do not ask for a password. Do not `type_text` the secret.
- Continue → `record_observed` on the pause-target node.
- `--url` pins Playwright Chromium with **empty cookies**. No debug-port attach.
- Unique-window fail-closed: macOS / Linux without a unique frontmost title is coach-only. Windows native / Citrix / RDP are coach-only. Never spawn `win_agent`.
- Allow-per-`sub` before observe / click / halt.
- GET handshake is not actuation (the CLI does not GET the pack page to click).
- Uncertain delivery: no blind retry (`RECONCILIATION_REQUIRED`).
- Titles, values, screenshots, backend pixels never go to the mailbox callback.

## What stays Desktop-only

| Capability | Why it is not this package |
|---|---|
| Overlay chrome (ghost ring, pause card, pointer-transparent HUD) | Native overlay contract. Terminal prints instead. |
| `openadapt://` OS URL handler | Tauri / protocol registration. Pip users paste the command. |
| launchd / Login Item / tray always-on listener | Desktop tray is already running. Pip starts a foreground process. |
| Keychain lease persistence across process restarts | Thin CLI keeps `oals_` in memory for this process. |
| Coach HUD chrome on Windows native | Coach-only here too; Desktop owns the HUD. |

Pip is not a worse cousin for the **mailbox protocol**. It is a worse cousin for **chrome**: no overlay, no protocol handler, no tray. The hosted tools still only drive through OpenAdapt.

## Safety invariants (Desktop 154)

- Allow-per-`sub` before observe / click / halt.
- Continue → `record_observed`, never `type_text` for secrets.
- GET handshake is not actuation.
- Uncertain delivery: no blind retry.
- Titles do not go to MCP / mailbox callbacks.
- No public HTTP / Streamable-HTTP listener in this MIT package.
- No localhost tunnel.
- Bind tokens: exact `oab_[A-Za-z0-9_-]{43}`. Lease: exact `oals_[a-f0-9]{64}`. Reject `oar_`, `oap_`, swapped encodings, and anything that merely starts with `oa`.

## Job page (openadapt-web, not this PR)

Do not edit web 462 from this repository. The pack “Connect this computer” should eventually show, next to Open OpenAdapt:

```text
pip: openadapt connect '<runner-link>'
```

Desktop users keep the `openadapt://runner` button. Pip users paste the same bind into this command.

## Remaining gaps

- Overlay chrome.
- launchd / tray autostart.
- OS URL handler so a click on the pack page starts the pip client without paste.
- Extracting the rest of Desktop `AuthoringRunner` (node-table HMAC files, overlay Continue) into a shared import. This PR copies claim / poll / allow / Continue-without-`type_text` only.
