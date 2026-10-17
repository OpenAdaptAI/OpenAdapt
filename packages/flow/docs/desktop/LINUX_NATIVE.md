# Native Linux backend

`backend.kind: linux` drives one exact local Linux application window through
AT-SPI. It uses the same compiler, policy, identity, effect-verification, halt,
and audit runtime as the browser and Windows backends.

## Install and configure

On Debian/Ubuntu, install the AT-SPI runtime/typelib and the Flow extra:

```bash
sudo apt-get install \
  gcc pkg-config python3-dev libcairo2-dev libgirepository-2.0-dev \
  gir1.2-atspi-2.0 libatspi2.0-0
python -m pip install "openadapt-flow[linux]"
```

Use exact, case-insensitive application and top-level window names:

```yaml
backend:
  kind: linux
  linux_app: gedit
  linux_window_title: oa-trial.txt
  linux_allow_physical_input: false
```

```bash
openadapt-flow replay bundle/ \
  --backend linux \
  --linux-app gedit \
  --linux-window-title oa-trial.txt
```

Zero or multiple matching windows refuse before capture or input. The backend
enumerates all AT-SPI candidates inside that window and likewise refuses an
ambiguous locator instead of selecting the first match.

## Action model

The normal path is structural:

1. Record the target's AT-SPI accessible ID (stored in the backend-neutral
   `StructuralLocator.automation_id` field), role, name, and exact window.
2. Enumerate live candidates inside the exact configured window.
3. Require one candidate and bind its live object identity and bounds into a
   SHA-256 fingerprint.
4. Re-enumerate immediately before action and refuse a stale fingerprint.
5. Use the strongest exposed AT-SPI action (`invoke`, `toggle`, `select`,
   `focus`, or editable-text replacement).
6. Return an `ActionDeliveryReceipt` whose `outcome_verified` is always false.

Postconditions and independently configured system-of-record effects decide
whether the business action succeeded. AT-SPI accepting an action is never
treated as proof of that outcome.

Global X11 pointer and keyboard synthesis is disabled by default. A deployment
may enable `linux_allow_physical_input` only after qualifying the exact
interactive session and workflow. Even then, the backend first binds and
focuses the exact target window; delivery failure raises and halts.

## Display boundary

X11 is the initial live transport. Window capture is cropped to the exact
AT-SPI window bounds and requires that window to be active so another
application cannot silently occlude the pixels used by verification.

Wayland does not allow ordinary clients to inspect and inject into other
applications. The correct boundary is an operator-approved XDG Desktop Portal
RemoteDesktop/ScreenCast session (with its live D-Bus/PipeWire/libei
capability). The built-in client does not fabricate such a grant from an
environment variable: it refuses on Wayland until a portal-backed client owns
the live session.

## Qualification

The required `linux-atspi-x11` CI job owns an isolated Xvfb display, session
D-Bus, AT-SPI registry, and minimal GTK3 application. It runs exactly three
fresh-process text-and-button trials, verifies each effect from exact file bytes
outside the target UI, then runs three ambiguous-control and three stale-handle
trials that must refuse with the effect file absent. Its artifact explicitly
reports silent incorrect success, over-halt, refusal failures, native delivery,
latency, cleanup, and model calls.

The default unit suite separately exercises exact-window selection, bounded
candidate enumeration, traversal refusal, target-window capture, Wayland portal
gating, and disabled-by-default physical input through an injected client.

This is scoped acceptance evidence for the in-tree GTK workflow and CI image,
not arbitrary-application acceptance. Before promoting a customer Linux
workflow, repeat the same evidence contract against the exact deployment and
retain:

- Exact distribution, desktop environment, X11/portal transport, application
  version, display scale, and workflow.
- Task and independent effect oracles.
- Silent incorrect success, over-halt, operator intervention, and latency.
- Duplicate-window/control ambiguity, app restart, window movement, and display
  scale conditions relevant to that deployment.
- Zero falsely confirmed outcomes.
