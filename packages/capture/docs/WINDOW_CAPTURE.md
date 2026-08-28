# Window-scoped and multi-monitor capture

How the recorder scopes a capture to one window, how coordinates are
translated, and what happens when a window moves or a display changes.

## Window-scoped recording

**Status: implemented, with display-free unit coverage on every
supported operating system.** The production release gate also requires live
window capture, input injection, movement, resize, video verification, and no
skipped tests on interactive macOS and Windows runners. Linux X11 has a separate
opt-in live window check. A customer RDP or Citrix deployment still requires
task- and environment-specific qualification.

By default the recorder captures the full screen. Window-scoped mode records
ONE window in that window's own pixel space. This is the mode built for
remote-display demonstrations (Parallels, Citrix Workspace, Microsoft Remote
Desktop), where `openadapt-flow`'s `rdp_window` replay drives the client
window's pixels directly. Recording scoped to the same window removes the
full-screen-vs-window coordinate mismatch at the source:

```python
from openadapt_capture import Recorder

with Recorder(
    "./my-capture",
    task_description="Demonstrate the workflow",
    window={"owner": "Parallels", "title": None},  # substring match
) as recorder:
    input("Perform the task, then press Enter...")
```

`owner` matches the application. macOS uses the window owner name. Windows and
Linux use the process executable name. `title` optionally disambiguates among
the application's windows. Both selectors are case-insensitive substrings,
matching how `openadapt-flow` identifies the same window at replay time. The
selectors can also be set through config or environment
(`RECORD_WINDOW_OWNER` / `RECORD_WINDOW_TITLE`).

In this mode:

- **Frames are the target window's pixels.** macOS captures the window's own
  buffer (`CGWindowListCreateImage`, the identical call flow's replay uses);
  Linux X11 reads an XComposite named-window pixmap. It doesn't use a root
  screenshot, so another window cannot replace the target pixels. Windows
  grabs the window's screen region, so keep the window unoccluded.
- **Input coordinates are translated at capture time** into the captured
  frame's pixel space (`pixel = (global_point - window_origin) * scale`, the
  exact inverse of the replay mapping). Input outside the window records
  out-of-range coordinates rather than being silently clamped.
- **The window scoping is persisted**: the recording's config JSON carries the
  target, resolved window, initial bounds, fixed output viewport, current
  source viewport, scale-to-fit mapping, and content rectangle
  (`CaptureSession.window_capture`), and the window is re-resolved every
  frame with bounds changes recorded as window events, a bounds timeline
  converters can use to be exact even when the window moves.
- **Each frame has one ordered geometry identity.** Current window captures
  store the frame and its window event under the same source ordinal. An action
  stores a later ordinal and names the exact earlier pair it used. The pair
  binds the process start identity, display topology, bounds, scale, fixed
  viewport, and geometry generation. Recorder shutdown retains one final frame
  after input has stopped, so a consumer can select an exact after-action frame
  by ordinal instead of by nearest timestamp.
- **Window movement and resize are supported.** The first frame fixes the
  encoded video size. Later source frames scale to fit and letterbox into that
  viewport. Input uses the exact current bounds and content rectangle. No frame
  is silently skipped because the window changed size or moved to a display
  with a different scale.
- **Fail-loud guarantees:** recording refuses to start if the window cannot be
  resolved and captured; input arriving before the first frame is discarded
  with a warning instead of being recorded in the wrong coordinate space; a
  lost window, capture failure, or unexpected output-frame size fails the
  session instead of producing complete-looking media with an evidence gap.

Linux window mode requires an X11 session with EWMH and XComposite. Capture
won't start window mode in a native Wayland or XWayland-only session. A future
Wayland producer must bind the portal-selected window, its pixel stream, and
event-time coordinates before it can replace this refusal.

Note for converters: window-mode coordinates are already in captured-frame
pixels (`coordinate_space == "window_pixels"`); do not rescale them by
`pixel_ratio`.

After every producer and writer has stopped, `Recorder` verifies the database
and writes `capture-artifact-manifest.json` plus `capture-terminal.json`. The
terminal binds the complete artifact inventory, event counts, final source
ordinal, capture session identity, and completion time. A current native
consumer should use `CaptureSession.load_verified()`. It checks the seal, copies
the exact artifacts into a private snapshot, and opens the copied database in
SQLite immutable read-only mode. A current window capture without this seal is
incomplete and must not be compiled as native geometry evidence.

## Multiple monitors

Full-screen mode records the complete virtual desktop reported by MSS, not
only the primary monitor. Capture stores its origin, fixed viewport, monitor
count, and privacy-safe monitor rectangles as
`CaptureSession.desktop_capture`. Global input is translated into this exact
combined-frame coordinate space. This includes a secondary monitor whose
native coordinates have a negative origin.

The multiple-monitor path is qualified by `live-qualification.yml`, which needs
a host with two real monitors. A GitHub-hosted runner reports one monitor, so
the release gate records the single-monitor topology its trials ran against and
does not prove the multiple-monitor contract. Downstream converters must not
apply the legacy display-ratio scale when
`coordinate_space == "virtual_desktop_pixels"`.

The monitor topology is fixed for one recording. Connecting, disconnecting,
rotating, or changing the resolution or scale of a display changes the encoded
frame contract. Capture fails the session if that occurs. Start a new recording
after a display-topology change. This boundary does not restrict movement or
resize of a recorded window across an unchanged monitor layout.
