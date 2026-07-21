# Citrix and pixel-only remote display

OpenAdapt treats Citrix as a remote-display substrate beneath the same compiler
and governed runtime used for browser, Windows UIA, macOS, and RDP. A workflow
is still recorded, compiled, policy-checked, executed, independently verified,
and audited in the same IR. The Citrix-specific component is the driver that
observes the published-application window and delivers input.

## Driver model

Citrix Workspace paints a remote Windows application into a local client
window. UI Automation and MSAA normally do not cross the ICA boundary, so the
driver uses the capabilities available outside that boundary:

- capture only the intended Workspace or published-application window;
- map captured framebuffer pixels to the client window's screen coordinates;
- bring the exact client window to the foreground and prove that binding before
  physical input;
- deliver mouse, keyboard, and wheel input through the host OS;
- return action-delivery receipts separately from outcome verification; and
- expose no structural capability when none is available.

`openadapt_flow/backends/remote_display.py` implements this contract on macOS
(Quartz), and `openadapt_flow/backends/win32_window_client.py` provides the
Windows-host client for the same backend (PrintWindow/BitBlt client-area
capture, SendInput injection, per-monitor-v2 DPI, UIPI refusal). The win32
client is mock-contract-tested only — no counted batch on a real Windows host
exists yet (`claims.yaml` `win32-window-replay-roadmap`). The backend
deliberately remains small: target uniqueness, identity, policy,
postconditions, independent effects, retries, repair, and audit belong to the
OpenAdapt runtime rather than the display driver.

```text
demonstration
    ↓
OpenAdapt compiler + governed workflow IR
    ↓
policy, identity, target uniqueness, effect contract
    ↓
RemoteDisplayBackend
    ↓
Citrix Workspace / published application
    ↓
screen observation + independent system-of-record verifier
```

## Reusable evidence

The remote-display backend and runtime seam have three complementary evidence
sources:

- CI exercises framebuffer capture, coordinate mapping, foreground refusal,
  input delivery, the absence of a structural rung, pixel-only target
  resolution, same-surface readback, identity refusal, and ambiguity refusal.
- A local Parallels client-window qualification exercised real host-window
  capture and OS-level input against a Windows guest without access to its
  accessibility tree.
- The accepted real-network RDP batch exercised remote frame decode and input
  delivery into Windows 11 for 3/3 fixed trials, with independent guest-tools
  file verification, zero silent incorrect successes, zero over-halts, and zero
  model calls. See [`../backends/RDP.md`](../backends/RDP.md).

These results establish the compiler/runtime seam and remote-display driver
shape. Citrix ICA/HDX acceptance is performed in the customer's exact
environment because the published application, Workspace policy, rendering,
session behavior, and available effect oracle determine the workflow's accepted
scope.

## Design-partner qualification

A Citrix deployment starts with one repeated workflow and one independently
observable business result. The acceptance record names:

- Citrix Workspace and target-application versions;
- published application, account/role, and session policy;
- monitor layout, DPI, scaling, resolution, and window/full-screen mode;
- task parameters, demonstrations, expected exceptions, and risk classes;
- identity evidence required before each entity-sensitive action;
- a system-of-record effect oracle for every consequential write;
- latency, reconnect, lock, and timeout conditions included in the test; and
- run count, failure taxonomy, silent incorrect success, over-halt, operator
  intervention, model calls, and time-to-repair.

Qualification begins in shadow mode, moves to supervised production writes,
and expands only after the fixed workflow meets its acceptance thresholds.
Repeated labels or windows, stale foreground bindings, unreadable identity,
unverifiable effects, session changes, and display calibration drift must halt
before a consequential action.

## Verification boundary

On-screen readback is useful for checking visible state, but it is not an
independent business-effect oracle. A rendered “Saved” state can be optimistic,
stale, or incomplete. Consequential workflows therefore bind the screen action
to an independently queried effect wherever the customer system exposes one:

- API or FHIR record state;
- database or reporting replica;
- exported file and exact hash;
- downstream queue/event state; or
- a customer-approved human authorization when no independent read exists.

When no independent effect can be observed, the workflow remains supervised
and reports the effect as `unverifiable`; action delivery is never promoted to
confirmed business success.

## Deployment configuration

The Citrix driver runs inside the customer-controlled execution boundary. The
deployment pins fail-closed privacy, encrypts bundles and reports, denies
unapproved egress, and scopes screenshot retention to the reviewed workflow.
Screen Recording and Accessibility permissions are granted only to the runtime
identity on the dedicated execution host. The Workspace window must remain
available to that host; a lost or ambiguous foreground binding causes refusal.

Citrix support, assurance, SLA, BAA, or certification applies only to the exact
deployment and written terms that name it. The product architecture is ready
for that qualification; RDP or local-console evidence is not relabeled as ICA
or HDX evidence.
