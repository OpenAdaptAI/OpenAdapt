# OpenAdapt Capture design

## Product role

`openadapt-capture` is the canonical native recorder for OpenAdapt. It records
screen media, native input, timing, window geometry, and optional action-time UI
structure into one local session. `openadapt-flow` consumes the session, applies
the compiler and qualification contracts, and owns governed replay.

An exact Capture release gets its product state from the signed Production
admission record. A missing, expired, revoked, mismatched, or unverifiable
admission means **not actively admitted**. A successful unit test or a runnable
package doesn't admit a recording path. A release must also pass the
exact-commit clean-install and interactive native qualification described
below.

Capture is local-first. It does not upload a recording. A raw session can
contain screen text, typed secrets, accessibility text, and optional narration.
It remains inside the approved local boundary unless a separate explicit
operation exports it.

## Supported recording paths

| Path | Capture contract |
| --- | --- |
| Full virtual desktop | Capture the combined MSS desktop and translate global input into that exact pixel space. |
| One native window | Re-resolve and capture the selected window on each frame; translate input into a fixed encoded viewport. |
| RDP or Citrix client window | Use the native window path. Treat the remote application as pixels; local accessibility APIs do not cross the remote boundary. |
| Browser | `openadapt-flow` owns the supported Playwright recorder. The Chrome extension and bridge in this repository are source-only development prototypes and are excluded from Capture release artifacts. |

Capture supports native input observation on macOS, Windows, and X11 Linux.
It retains action-time structure through Windows UI Automation, macOS
Accessibility, and Linux AT-SPI when the local provider is available. The
public structural protocol also accepts an injected read-only provider.

## Session pipeline

One recording has these stages:

1. Resolve and verify all required local dependencies before input listeners
   start. This includes a real encode-and-decode probe when video is enabled.
2. Resolve the initial native-window or virtual-desktop coordinate scope.
3. Create the per-capture SQLite database and media staging path.
4. Observe native input and screen frames on separate workers.
5. Reserve each observation in one ordered source journal before any optional
   structural lookup. A failed reservation fails the session; later events
   cannot pass it.
6. For a native window, enqueue each frame and its window geometry as one
   source-ordinal pair. Publish that geometry to input observers only after the
   pair enters the journal.
7. Bind each actionable input to the last published frame pair and optional
   structural observation. Retain one ordinal-later frame after input stops.
8. Stream RGB frames to a separately provisioned FFmpeg process.
9. Close, verify, and atomically promote the MP4. Retain an incomplete partial
   file on an encoder failure and never report it as complete media.
10. Post-process raw input into the public action view. A merged action keeps
    its terminal primitive's source binding and refuses mixed geometry epochs.
11. Reconcile committed rows with producer counts, verify the v2 frame/action
    relations, inventory every immutable artifact, and write the completion
    seal.

A worker failure stops the session and propagates through the recording
boundary. A frame whose size violates the fixed stream contract is an error. It
is not silently skipped.

## Coordinate contracts

### Full virtual desktop

MSS monitor zero is the bounding rectangle of all active monitors. Native input
uses global desktop coordinates. Capture stores:

- `coordinate_space = "virtual_desktop_pixels"`
- the combined desktop origin and viewport
- the physical monitor count
- privacy-safe physical monitor rectangles

For each input point, Capture subtracts the combined desktop origin. This maps
negative-origin secondary monitors into the exact captured frame without a
fabricated per-monitor scale. A converter must not apply the legacy
`pixel_ratio` scale to this coordinate space.

The desktop topology is fixed for one recording. Display hot-plug, rotation,
resolution, or scale changes alter the encoded frame contract and fail the
session. Multiple monitors are supported when the topology stays unchanged.

### Native window

The first successful frame fixes the encoded viewport. Capture then re-resolves
the selected window for each frame. It retains the current bounds, source
viewport, source-to-output scale, letterbox content rectangle, and change
timeline.

When the window moves, resizes, or moves between displays with different
scales, Capture scales the complete source frame to fit the fixed encoded
viewport and adds letterboxing as required. It maps input with the same current
bounds and content rectangle. It does not discard a frame because the source
window changed size.

Input outside the selected window remains out of range. Capture does not clamp
it into a valid-looking target coordinate.

Each current window frame carries a process-bound window identity, display
topology digest, and geometry epoch digest. The window and screenshot rows use
the same source ordinal. An action uses a later ordinal and names the exact pair
that supplied its coordinates. Capture refuses process replacement, topology
drift, off-screen state, mixed generations, or a missing pair.

## Completion and consumer boundary

A recorder session becomes complete only after all producers and writers have
stopped and the database has passed its integrity and relationship checks.
Capture then writes two create-only files:

- `capture-artifact-manifest.json` inventories every immutable regular file by
  relative path, size, and SHA-256 digest.
- `capture-terminal.json` binds that manifest, the source session identity,
  event counts, last source ordinal, and completion interval.

Both files use canonical JSON and domain-separated digests. Mutable local
control state is not part of the artifact inventory.

`CaptureSession.load_verified()` checks the complete inventory before it opens
the database. It copies the verified files to a private temporary directory and
opens the copied database with SQLite `mode=ro&immutable=1`. It never migrates
or writes the source capture. A current v2 window session without the terminal
and manifest is incomplete evidence.

## Native input

Capture records these primitive event classes:

- mouse move
- mouse button press and release
- mouse scroll
- key press and release

Platform observers have one ordered callback contract. They identify injected
events when the operating system provides that information. Capture can exclude
its own injected qualification events from a normal session. It refuses an
incomplete observer startup instead of reporting partial coverage as complete.
On macOS, an active pass-through session tap holds downstream event delivery
only while Capture commits a clean frame. This requires both Input Monitoring
and Accessibility permission. The interactive macOS qualification proves that
an annotated downstream event cannot pass that cut before the frame commit.

The post-processing layer merges primitive events into higher-level actions.
The compiler remains responsible for refusing action forms that its selected
backend cannot replay safely.

## Structural observations

Structural observations are optional evidence beside an action. The versioned
schema can retain:

- provider and query type
- element role, name, AutomationId, class, framework, bounds, and patterns
- process and top-level window identity
- bounded ancestry
- exact candidate cardinality and its matching fields

The package creates a Windows UIA, macOS Accessibility, or Linux AT-SPI
observer for the current platform. A missing optional field stays missing.
Capture doesn't infer an accessibility value from a screenshot, coordinate, or
neighboring control. Provider text has strict length and depth bounds. A
transient provider failure omits the optional observation without corrupting
the screen and input evidence.

The Linux provider uses the GObject AT-SPI binding. The `linux` package extra
installs the reviewed `PyGObject>=3.46,<3.50` range, which supports systems with
GLib 2.64 or newer. The host supplies the PyGObject build packages, the AT-SPI
typelib/runtime, and an interactive desktop accessibility bus.

The native provider describes the local accessibility tree. It does not
describe controls inside an RDP or Citrix pixel stream.

## Video and frame timing

Capture does not import, link, download, or bundle FFmpeg. It invokes an
explicitly configured, Desktop-provisioned, or user-provisioned executable
through a process boundary. Preflight verifies the required raw-video input,
selected encoder, MP4 muxing, PNG encode/decode, `image2pipe`, and `select`
filter before recording starts.

The writer emits a deterministic constant-rate stream. It reuses the preceding
RGB frame for a missing integer PTS slot. A compact MP4 metadata box binds
logical capture timestamps to decoded frame indexes. Final verification decodes
a real frame from the staged artifact before the file is promoted.

Capture uses SQLite for events and the filesystem for media. The current media
contract is one MP4 per recording. The package does not claim video chunking,
automatic retention, or crash resume.

## Audio boundary

Microphone narration is off by default. When enabled, Capture requires an
installed on-device transcription backend before it opens the microphone. It
does not use a remote fallback. It discards the waveform after transcription
unless the operator explicitly enables waveform retention.

Transcript text is unsanitized. A retained waveform is biometric data. Neither
is safe for automatic egress.

## Browser boundary

The supported browser recorder remains Playwright-native in `openadapt-flow`.
It needs one owner for the browser context, DOM identity, field geometry,
ordered before/after frames, page state, and source-time secret redaction.

The Chrome extension can supply useful DOM observations, but it does not yet
provide this complete contract. It can become a supported auxiliary observer
after it has:

1. a shared versioned event schema;
2. source-time secret redaction;
3. explicit permission and browser-profile boundaries;
4. fail-closed reconnect and tab-lifecycle behavior;
5. exact frame and event binding; and
6. end-to-end compiler qualification.

It should not replace the Playwright recorder only to consolidate packages.
The stronger ownership and redaction boundary is more important than package
uniformity.

The extension and its unauthenticated development bridge are repository-only.
Wheel and source archives exclude the bridge and its legacy direct replay. The
production package keeps only the passive browser-event schemas needed to read
old local captures.

## Release qualification

The production release workflow is manual and binds evidence to one exact
commit. Every job runs on a GitHub-hosted runner. It must:

- build and validate one wheel and sdist;
- install and uninstall that exact wheel in a clean environment on Linux,
  macOS, and Windows;
- start the real recorder against the real display of a hosted macOS runner,
  prove a real first frame, prove the capture database, prove bounded memory,
  and prove a clean shutdown, in three counted trials with no skip;
- record the exact display topology each trial ran against; and
- retain machine-readable test and topology evidence.

`scripts/check_release_ci.py` also requires a successful `test.yml` run on the
same exact commit, which is where the complete headless suite and the
byte-for-byte synthetic fixture determinism check run.

The release workflow accepts only a successful, complete job set for its exact
commit. Missing, stale, skipped, partial, or failed evidence blocks publication.

## Live qualification

`live-qualification.yml` runs the interactive lanes that need a physical
desktop. It runs weekly and on demand, and it is not a release gate.

It also carries the same live recorder lane on hosted Windows. That lane
reproduces a `video_writer` startup failure that `test.yml` does not hit on
the same commit, so it does not gate a release while that defect is open.
`test.yml` runs the same four tests on `windows-latest` and is required on
the exact commit, so Windows live recorder coverage stays in the required
evidence.

The self-hosted lanes prove, and the release gate therefore does not prove:

- that global input injected through the operating system reaches the native
  listeners and is written into the capture;
- the combined multiple-monitor virtual desktop and its coordinate
  translation, including a secondary monitor with a negative origin;
- live window movement and resize under window-scoped capture; and
- live native structural observation through AX, UIA, and AT-SPI.

No GitHub-hosted runner can prove these. Measured on 2026-08-28, every hosted
runner reports exactly one monitor, and Xvfb with two screens and `+xinerama`,
and a RandR `--setmonitor` split, both still report one monitor to MSS.
Injected input reaches no native hook on any of the three hosted operating
systems. Each lane needs a host with two monitors, a logged-in desktop
session, and real input and screen permissions.

The lanes stay skipped until a runner carrying the labels each lane names is
registered and the repository variable
`CAPTURE_SELF_HOSTED_QUALIFIED_RUNNERS` is set to `1`.

## Known boundaries

- A visible logged-in desktop session and operating-system permissions are
  required.
- Windows window capture uses a screen-region grab and requires an unobstructed
  target window.
- A display-topology change requires a new recording.
- Browser extension capture is a repository-only development prototype. Its
  bridge and direct replay are not in release artifacts. It is not the
  supported browser recorder.
- A raw capture is sensitive and has no automatic safe-for-egress derivative.
- Customer RDP and Citrix environments require their own task-specific
  qualification.
