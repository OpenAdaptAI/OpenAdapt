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

```bash
# Browser (Playwright / Chromium)
openadapt-flow record --backend web --url https://your.app --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow replay bundle --url https://your.app

# Windows (native UI Automation via the in-guest WAA agent)
openadapt-flow record --backend windows --agent-url http://localhost:5001 --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow replay bundle --agent-url http://localhost:5001

# macOS (accessibility, one app window)
openadapt-flow record --backend macos --macos-app TextEdit --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow replay bundle --macos-app TextEdit

# Linux (AT-SPI, one exact app window)
openadapt-flow record --backend linux --linux-app gedit \
  --linux-window-title "Untitled Document 1" --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow replay bundle --linux-app gedit \
  --linux-window-title "Untitled Document 1"

# RDP (network session, or a local remote-desktop client window)
openadapt-flow record --backend rdp --rdp-host 10.0.0.5 --out rec
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
`replay`/`run` for a bound bundle; the target flags (`--agent-url`,
`--macos-app`, ...) still name the concrete window/host. `run ... --config
deploy.yaml --profile standard|regulated` wires the same selection for a real
deployment.

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
