# Repair on every backend

Status: design, with the first part built (see [What is built](#what-is-built)).

Self-repair and "teach the fix" let a run that stopped on an unexpected
screen, such as an update notice or a new dialog, be fixed once and then
handle that screen on later runs. A repair agent or a person shows the fix,
Flow checks it, a person approves it, and the next run replays it.

Until now a fix that adds a click needed a DOM identity, so repair worked only
in a browser ([SELF_REPAIR.md](SELF_REPAIR.md#limits)). Most of the apps
OpenAdapt is meant for are Windows desktop apps, often reached over RDP or
Citrix. This note says how the same fix works on each kind of backend, and
where its safety comes from on each.

## Three kinds of backend

| Kind | Backends | What a click can be identified by |
| --- | --- | --- |
| DOM | Browser (Playwright, CDP) | The DOM element and the record it sits in |
| Accessibility tree | Windows UI Automation, macOS AX, Linux AT-SPI | The element's role, name, ancestry and window |
| Pixels only | RDP, Citrix, other remote displays | Nothing on the screen can identify it |

A backend's kind comes from what it can do, not from its name:

- DOM: the backend has a browser page.
- Accessibility tree: the backend implements `element_identity_at`.
- Pixels only: the backend has neither a page nor any accessibility reads
  (`structural_locator_at`, `structured_text_at`).

A native backend that has accessibility reads but no `element_identity_at` yet
is refused with that reason. It is not treated as pixels only, because a
broken accessibility read must not quietly pick the weaker rule.

## The element identity interface

`openadapt_flow.repair.element_identity` defines one record for any backend
that has an element tree:

```python
@dataclass(frozen=True)
class ElementIdentity:
    source: str            # "dom", "uia", "ax" or "atspi"
    role: str              # "button", "AXButton", ...
    name: str | None       # accessible name or label
    automation_id: str | None
    ancestry: tuple[tuple[str, str | None], ...]  # (role, name), parent first, ending at the window
    window: str            # the top-level window's exact name
    process: str | None    # the owning process, when the backend reads it
```

A backend implements two calls:

- `element_identity_at(x, y)` returns the identity of the element under a
  point, or `None` when it can't read one. It never raises.
- `locate_structural(locator)` (already on every accessibility backend)
  finds the element again on the live screen. It refuses when more than one
  element matches.

The record is evidence and a check, not a selector language. The new step
stores the backend's own `StructuralLocator` for the point, which must name
the same role, name and id as the identity; uniqueness is checked with that
locator, so replay's structural rung finds the same one element. The full identity, ancestry
included, goes into the repair evidence and into what the approving person
sees. Its digest is bound into the candidate.

Each backend fills it from what it reads:

| Backend | `role` and `name` | `ancestry` | `window`, `process` |
| --- | --- | --- | --- |
| macOS AX (built) | `AXRole`, title or description | `AXParent` up to the `AXWindow`, sheets and dialogs included | window title, app pid |
| Windows UIA ([#1707](https://github.com/OpenAdaptAI/OpenAdapt/pull/1707)) | `ControlType`, `Name`, `AutomationId` | `GetParentControl` up to the outermost window; an unmapped role is empty | outermost window name (an owned dialog's owner), process name or none |
| Linux AT-SPI (built) | mapped role, accessible name, accessible id | raw AT-SPI role and name of each parent up to the pinned window, which must match by title, bounds and process | window title, application name |
| Browser | not used for admission | | |

The browser keeps its current rule unchanged: a new click must sit inside the
record the recording verified, read from the DOM.

## When a fix's new click is admitted

A fix is a corrected click for the halted step, or a bridge of up to three new
actions. The rules below are for the bridge's new clicks. The rules that
already apply on every backend stay: no new click may read as a save, an
unlabeled control is refused, a new click can't reuse another step's control,
a typed value must be a parameter or on the screen, and the probe stops
before the first save.

| Kind | A new click is admitted when |
| --- | --- |
| DOM | The DOM shows, under the click, a record the recording verified (today's rule). |
| Accessibility tree | Either the tree shows a verified record under the click (the same rule as the DOM), or all of: the element identity is readable, has a role and a name or id, is in the pinned window, and is the only element in that window with that locator; **and** a record check covers the workflow. |
| Pixels only | A record check covers the workflow. |

**A record check covers the workflow** when all of these hold, checked before
the fix is built:

1. The run has a system-of-record verifier (the write verifier, and the
   whole-database frame where the deployment configures one).
2. The run's profile is `standard` or `regulated`, so the verifier must run
   and a missing one refuses the run.
3. The workflow has at least one save, and every save declares a confirmed
   system-of-record effect contract on each path it can take.
4. Every save has its own identity check armed.

If any of these fails, the fix is not admitted. The run stays stopped and
waits for a person, as it does today.

### Why this is safe without screen identity

On a pixel-only backend, nothing on the screen can prove which control a new
click hits. Screen matching (the template, the label read by OCR, the
dialog's text) stays a pre-check only. The safety comes from the three layers
that don't depend on the screen:

1. **The record check.** The fix's new clicks never save. The save step is
   unchanged, its identity check still runs before it, and after it the
   verifier reads the system of record. A click that opened the wrong record
   is stopped by the save's identity check; a wrong write is caught by the
   verifier as `REFUTED` or `RECONCILIATION_REQUIRED`.
2. **The wire check.** Where the deployment pins the save's request, it still
   applies to the repaired run.
3. **A person, and undo.** A fix admitted this way always needs a person's
   approval. A signed standard-change policy can't apply it on its own.

The fix runs on a hold: the probe replays it up to the last point before the
save and writes nothing. The person approves the exact form the probe
reached. The repaired bundle then enters its canary window. The resumed run
proceeds only if the form is the one the person approved, and its save is
checked against the system of record. A canary run that isn't `VERIFIED`
rolls the active bundle back to the previous one. Where the deployment has a
rollback plan, undo applies as for any run.

### Fail closed

- An element identity that can't be read, isn't unique, or leaves the pinned
  window refuses the click on an accessibility backend.
- A backend whose kind can't be determined is refused.
- A fix that can't be positively checked is never applied without a person.
- The admission basis (`record_identity`, `element_and_record_check` or
  `record_check`) is written into the evidence and the candidate, so a
  reviewer sees which layer carries the click.

## A dialog over the record's identity band

An app's own dialog (a session notice, an update prompt) can open over the
next step's identity band. That step's identity check then reads part of the
record and refuses, which looks like a wrong-record halt, and self-repair
never takes one of those. #1724 tells the two apart, narrowly. The halt is
repaired in `after` mode on the previous click (dismiss the dialog) only
when all of these hold:

- nothing reached the halted step, no save received input, no effect was
  observed;
- the backend proves that a window of the app itself, owned by the window
  the last identity-verified step was in, covers the band
  (`record_verified_window` at that step, then `owned_window_over`). On
  macOS that is the window's one proved sheet; on Windows an owned window of
  the same process, first over the band in the live z-order;
- that window is what cut the band: its rectangle spans the band from where
  the text read stops to the band's right edge, at the band's full height
  (the share of the band left uncovered is within 0.1 of the share of the
  recorded text read). A popup that only touches the band, such as a tooltip
  over a column's ellipsis, does not count;
- the part of the band that was read still matches the recorded record: no
  contradicted, look-alike, glyph-ambiguous or unexplained name token, and at
  least half the band matched. Another patient's header behind the dialog
  fails this and stays an identity halt;
- the previous step is a click that doesn't save, acted on a trusted rung.

The dismiss click is held to that dialog. It must land in the very window
`owned_window_over` reported (asked again at the click point), and the
dialog's text must read as a notice only. Every word in it must be on a
short list of session-notice words ("Your session expires in 5 minutes",
"OK"), and it shows no punctuation beyond `. , : !`, so it asks no question
in any script. It must also say that a session expires, so a dialog whose
message wasn't read (only its "OK" was) is refused, and so is any text that
runs across the dialog's edge. The click's label must be "OK". Anything else refuses,
including an update prompt, a dialog in another language, or one with
nothing readable in it.
Answering "OK" to "Discard unsaved changes?" would lose data. The bridge is
exactly that one click. A covered-band repair gets one attempt: a later
attempt reopens the app, whose dialog is a new window, so its click is
refused.

Nothing after that is relaxed: the probe replays the halted step with its own
identity check in full once the dialog is gone, then the record check and a
person decide as for any repair. That full check is what protects the case
where the dialog hides exactly where two records differ (a same-name patient
with another birth date): the read beside the dialog can match, and the
recheck refuses.

## One lifecycle on every backend

Every backend uses the same four stages and the same store
([REPAIR_LIFECYCLE.md](REPAIR_LIFECYCLE.md)):

1. **Demonstrate.** A repair agent proposes the fix, or a calling agent or
   person demonstrates it on the held run (`--hold-on-halt`). On an
   accessibility backend the hold also returns the element identity under the
   click, so the dialog's box comes from the tree rather than from a pixel
   search.
2. **Propose.** Flow builds the patched bundle and runs the probe, the
   replay campaign and the fault campaign.
3. **Approve.** One person approves, on the phone web app or at a terminal.
   The approval request is the same on every backend: a screenshot of the
   form the probe reached, the new control outlined, and the admission basis.
4. **Replay.** The approved bundle runs in its canary window, as above.

## The new step's identity band

A new click admitted on the record check has no record identity of its own,
and the `clinical-write` policy refuses a click with no identity check at all.
So the new step gets the dialog around it as its identity band, the way agent
teach already marks a dismiss step: the dialog's box comes from the element's
ancestry (the nearest `AXSheet`, `dialog` or similar), or on pixels from the
drawn box around the click; its text is hashed with the bundle's salt. A
click with no dialog around it, or a dialog with no readable text, is refused.
The band is a screen pre-check: it stops the step when a different dialog
shows, but the click's safety still comes from the record check.

## What is built

In this change:

- `openadapt_flow.repair.element_identity`: the identity record, the backend
  kind, the record-check cover and the click admission.
- `MacOSBackend.element_identity_at`, from the AX tree, and
  `LinuxBackend.element_identity_at`, from the AT-SPI tree.
- Self-repair admits bridge clicks by the rules above and gives the new step
  its dialog band. The browser rule is unchanged.
- The repair hold answers `element_identity`, and agent teach takes the
  dialog box from the identity's ancestry when there is one.
- Unit tests, and a real-app qualification script for macOS
  ([benchmark/repair_every_backend](../benchmark/repair_every_backend/README.md)).

## Qualified so far

On a native Cocoa test app with a SQLite database, through `MacOSBackend`
(scripted repair agent and approver; live AX, screen, input and database):
the run stops at the new notice before any save; the "Later" click's AX
identity is read, found unique and admitted on `element_and_record_check`;
the probe, the replay campaign and the fault campaign (another patient's
chart) pass; the scripted approver's answer is signed through the
decision portal's own code; and the
resumed run is `VERIFIED` against SQLite with exactly one new row. That run
holds the app's caret steady. With a blinking caret, the clean-tree run
stopped in the probe at the note field's identity check and wrote nothing; an
earlier run that got further was refused at the save by the frame lease
because of the caret
([#1713](https://github.com/OpenAdaptAI/OpenAdapt/issues/1713)). Under
`demo`, and with no verifier, the click is refused and nothing is written.

On Linux, through `LinuxBackend` on a GTK3 chart app with a SQLite database
(`tests/native_apps/chart_gtk.py`, Xvfb with openbox; scripted repair agent
and approver; live AT-SPI, screen, input and database): the run stops at the
"Update available" notice before any save, the "Later" click is admitted on
`element_and_record_check` with its AT-SPI identity (role, name, ancestry up
to the window, process), the probe and the fault campaign pass, one scripted
approval is signed, and the resumed run is `VERIFIED` with exactly one new
row. Under `demo`, and with no verifier, the click is refused and nothing is
written. The `flow-linux-atspi` CI job runs it
([benchmark/repair_every_backend](../benchmark/repair_every_backend/README.md)).

The pixel-only path doesn't reach repair yet: a remote click whose next
screen differs counts as an uncertain delivery
([#1712](https://github.com/OpenAdaptAI/OpenAdapt/issues/1712)).

## Not built yet

- `WindowsBackend.element_identity_at` and the `win_agent` endpoint behind it.
  The Windows session owns that code.
- Qualification on Windows UI Automation and over RDP, on the Windows lab.
- `--hold-on-halt` (teach the fix over MCP) for desktop and remote backends.
  `run --self-repair` takes them with `repair.session`.
- Input to a Windows app's owned dialog windows. On macOS, the pinned
  window's own AppKit sheet is accepted (#1711); any other window is still
  refused.
