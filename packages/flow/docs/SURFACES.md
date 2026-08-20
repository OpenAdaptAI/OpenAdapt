# Execution surfaces

OpenAdapt drives six execution surfaces through one governed runtime:
`web`, `windows`, `macos`, `linux`, `rdp`, and `citrix`. The browser is one
surface among six, not a privileged default.

## Surface selection is explicit in production

- Under `--profile standard` or `--profile regulated`, `record` and `run`
  REFUSE to proceed without an explicit target: pass `--backend` (one of
  `web`, `windows`, `macos`, `linux`, `rdp`, `citrix`) or set `backend.kind`
  in the deployment `--config`. There is no implicit browser default in
  production.
- Under `--profile demo` (or with no profile, the permissive pre-profile
  posture), an omitted `--backend` defaults to the browser and prints a
  visible notice. With `--profile demo`, the CLI also remembers your last
  explicitly selected target in a per-user state file
  (`~/.openadapt/flow_cli.json`, override with `OPENADAPT_FLOW_CLI_STATE`)
  and offers it as the default next time, again with a visible notice. This
  convenience is CLI state only; it is never written into a workflow bundle.

## Workflows are bound to their surface

The recorder stamps the surface into the recording (`meta.json` `surface`),
and `compile` seals it into the bundle (`workflow.json` `surface` plus the
implied `execution_mode`). A workflow recorded and qualified on one surface
refuses to `replay`/`run` on another:

```text
run REFUSED: this workflow is bound to surface 'windows', but the resolved
backend targets 'web'. ...
```

Pass `--allow-surface-override` to proceed anyway; the override is recorded in
the run report (`surface_override: true`, alongside `recorded_surface` and
`execution_target_kind`) as compatibility evidence, so a cross-surface run is
never silent. A surface-bound bundle also supplies its own default target: an
unqualified `replay bundle` selects the bound surface rather than the browser.
Bundles compiled before surface binding carry no `surface` and behave exactly
as before.

## First workflow, per surface

Each surface has an equivalent record -> compile -> replay path:

| Workflow surface | Exact install |
|---|---|
| Browser | `pip install 'openadapt-flow[browser]'` |
| Native Windows | `pip install 'openadapt-flow[capture,windows]'` |
| Native macOS | `pip install 'openadapt-flow[capture,macos]'` |
| Native Linux | `pip install 'openadapt-flow[capture,linux]'` plus the AT-SPI system packages in [`desktop/LINUX_NATIVE.md`](desktop/LINUX_NATIVE.md) |
| Network RDP | Recorder: `pip install 'openadapt-flow[capture]'` inside the demonstrated session; runner: `pip install 'openadapt-flow[rdp]'` |
| Local RDP/Citrix client window | macOS host: `pip install 'openadapt-flow[capture,macos]'`; Windows host: `pip install 'openadapt-flow[capture,windows]'` |

```bash
# Browser (Playwright / Chromium)
openadapt-flow record --backend web --url https://your.app --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow replay bundle --url https://your.app

# Attach the same recorder to one existing signed-in local Chromium tab.
openadapt-flow record --backend web --url https://your.app \
  --browser-cdp-endpoint http://127.0.0.1:9222 --out rec

# Windows: Capture records the local window; the in-guest WAA agent replays it.
openadapt-flow record --backend windows --window "Target App" --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow replay bundle --agent-url http://localhost:5001

# macOS: the app and title scope the local Capture window.
openadapt-flow record --backend macos --macos-app TextEdit \
  --macos-window-title notes.txt --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow replay bundle --macos-app TextEdit \
  --macos-window-title notes.txt

# Linux: Capture records the local desktop; AT-SPI selects the replay target.
openadapt-flow record --backend linux --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow replay bundle --linux-app gedit \
  --linux-window-title "Untitled Document 1"

# Network RDP: run record inside the demonstrated remote session.
openadapt-flow record --backend rdp --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow replay bundle --rdp-host 10.0.0.5

# Citrix / VDI (one exact local Citrix Workspace window)
openadapt-flow record --backend citrix --window "Citrix Viewer" \
  --rdp-window "Citrix Viewer" --rdp-window-title "Ward A" \
  --rdp-readiness-text "Appointments" --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow replay bundle --rdp-window "Citrix Viewer" \
  --rdp-window-title "Ward A" --rdp-readiness-text "Appointments"
```

The bound surface is the replay default, so `--backend` may be omitted on
`replay`/`run` for a bound bundle. During record, `--macos-app` /
`--macos-window-title` scope the macOS Capture window, and the local
RDP/Citrix flags `--rdp-window` / `--rdp-window-title` scope Capture and enter
the bundle's existing replay-binding metadata. `--agent-url`, `--linux-app`,
`--linux-window-title`, and `--rdp-host` are replay targets. The local Capture
session cannot control them, so `record` refuses them instead of accepting an
unused flag. Pass them to `replay`/`run`; `run ... --config deploy.yaml
--profile standard|regulated` wires the same selection for a real deployment.

The browser attach mode keeps the Playwright-native recording contract. It
binds one same-origin tab and reuses the same event schema, DOM evidence,
before/after frames, secret redaction, compiler, and governed replay path as a
browser that Flow launches. The endpoint is local-loopback only. Flow refuses
ambiguous tabs and does not navigate or close the attached browser. It
rebaselines exact event/frame coordinates after an idle resize or
monitor-scale change. It refuses an action that overlaps that transition. See
[`BROWSER_RECORDING.md`](BROWSER_RECORDING.md).

## The two remote execution modes

Remote systems (a Windows guest, a virtual desktop, a published app) can be
driven in exactly two modes, and the difference is a policy and capability
decision, not an implementation detail:

- **In-session** (`execution_mode: in_session`): the driver runs INSIDE the
  session it automates and uses the platform's accessibility / structured
  layer (Windows UIA via the in-guest WAA agent, macOS AX, Linux AT-SPI, the
  browser DOM). Choose this when policy permits installing the agent in the
  remote session; it enables structural resolution and structured-text
  identity.
- **External** (`execution_mode: external`): the driver runs OUTSIDE the
  remote session and drives the LOCAL client window (RDP client, Citrix
  Workspace) via pixels, keyboard, and mouse. Zero install inside the remote
  session; resolution and identity use the pixel/OCR ladder, with the
  documented pixel-substrate limits (`docs/LIMITS.md`).

The mode is fixed by explicit capability negotiation at qualification time,
recorded in the bundle (`execution_mode`), and never silently switched at run
time: `rdp` and `citrix` recordings are `external`; `web`, `windows`,
`macos`, and `linux` recordings are `in_session`. Moving a workflow between
modes means re-recording or re-qualifying it on the other surface (or an
explicit, report-recorded `--allow-surface-override`).
