# Repair on every backend: real-app qualification

`qualify_macos.py` repairs an "Update available" notice in a native macOS app
on two backend kinds, with a SQLite record check. Design:
[docs/REPAIR_ON_EVERY_BACKEND.md](../../docs/REPAIR_ON_EVERY_BACKEND.md).

The app is [`tests/native_apps/Chart.swift`](../../tests/native_apps/Chart.swift),
a Cocoa window built with `swiftc` on the machine that runs the script. The
repair agent and the approver are scripted. The app, its accessibility tree,
the screen capture, the input and the database are real.

```bash
python benchmark/repair_every_backend/qualify_macos.py --kind ax --kind pixel
```

## Results, 2026-11-03 (macOS 15, Apple silicon)

**AX passes end to end, with the app's caret held steady** (`--steady-caret`,
finding 5). With a blinking caret it stops safely before the save. Pixels
only doesn't pass yet. No run wrote a wrong row.

| Check | AX (`MacOSBackend`), steady caret | Pixels only (`RemoteDisplayBackend`) |
| --- | --- | --- |
| Demonstration recorded and compiled, certified `clinical-write` | Yes | Yes |
| New version stops at the notice before any save | Yes (`HALTED_BEFORE_EFFECT`, `after`) | No: see finding 3 |
| The new "Later" click is read and admitted | Yes: `element_and_record_check (button 'Later')`, ancestry `AXSheet "Update available"`, then `AXGroup`, then `AXWindow "Chart"` | Not reached |
| Probe up to the save: nothing written, target found, same record, same fields | Yes | Not reached |
| Replay campaign and fault campaign (another patient's chart stops before the save) | Yes, 1/1 each | Not reached |
| One approval, by the scripted approver, signed through the decision portal's own code with a throwaway roster key | Yes | Not reached |
| Resumed run, checked against SQLite | `VERIFIED`, exactly one new row for the patient (blinking caret: not reached; see below) | Not reached |
| With a `demo` profile the click is refused, nobody is asked, nothing is written | Yes | Not reached |
| With no verifier the run gate refuses, nothing is written | Yes | Yes |

The AX run needs [#1710](https://github.com/OpenAdaptAI/OpenAdapt/issues/1710)'s
fix (`MacOSBackend.act_structural`) and the glued-words `same_fields` check.

Evidence, each from a clean tree (the JSON records `git_sha` and
`git_dirty`): [`results/macos-ax-steady-caret-20261103.json`](results/macos-ax-steady-caret-20261103.json),
[`results/macos-ax-20261103.json`](results/macos-ax-20261103.json) (blinking
caret), [`results/macos-pixel-20261103.json`](results/macos-pixel-20261103.json).

With a blinking caret, the clean-tree run stopped in the probe at the note
field's identity check: OCR read only part of the patient banner (coverage
0.54), so the click was refused and nothing was written. An earlier run from
an uncommitted tree got past that check and was refused at the save by the
frame lease (finding 5).

## Findings

Each is a fail-closed refusal, not a wrong action.

1. **Fixed by #1710's PR: a durable run on `MacOSBackend` refused its first
   structural click.**
   Under `standard` the run is durable, and the checkpoint refuses the click:
   `remote delivery receipt does not bind the resolved target`. The receipt
   from `click_guarded` binds the frame and point
   (`visual_resolution_point_fingerprint`), while `action_evidence` expects
   the structural fingerprint when the rung is `structural`. The unrepaired
   bundle on the old app version fails the same way, so no production-profile
   run on native macOS completes today. Code: `openadapt_flow/action_evidence.py`
   (`_delivery_receipt_error`) and the replayer's `remote_guarded` branch.
2. **Fixed for macOS sheets
   ([#1711](https://github.com/OpenAdaptAI/OpenAdapt/issues/1711)): a real
   `NSAlert` sheet couldn't take input.** A sheet is its own CoreGraphics
   window, so `MacOSBackend`'s window binding refused input to it. It now
   accepts the pinned window's one attached sheet, proved by AX and the
   window server, and nothing else (see "A real sheet" below). Windows owned
   dialogs are the Windows session's half of #1711.
3. **On pixels only, a click whose next screen differs is "delivery
   uncertain".** Under `demo` the run ends `RECONCILIATION_REQUIRED`
   (`effect not settled at step_000`), so the stop is not repairable, by
   design. Under `standard` the first run also lost the window focus right
   after launch in the full script (it passed when run alone). A remote
   `after` repair needs the remote backend to prove delivery of a click that
   doesn't save.
4. Smaller: `RemoteDisplayBackend.type_text` on a macOS host delivered
   shifted text (`follow-up` arrived as `FOLLOW_UP`), so the script types
   upper case only. Pixel identity correctly abstains on MRNs with `0/O/1/l/I`,
   so the script's MRNs avoid them.

5. **A save click after typing is refused while the caret blinks**
   ([#1713](https://github.com/OpenAdaptAI/OpenAdapt/issues/1713)). The
   field keeps focus, and the caret's 5 by 32 pixel strip changes between
   the runtime's frame lease and the click, so the fresh-frame retries run
   out (`changed_bbox` 5 by 32 pixels at the end of the typed text, not
   touching the button). `--steady-caret` launches the app with AppKit's blink
   period set so the caret stays on; that run is the `VERIFIED` one above.
6. OCR glues a field's words together (`REPAIREDRUNFOLLOWUPINTHREEWEEKS`), so
   the probe's `same_fields` check on a screen with no form to read now also
   accepts a value of two or more words that equals a run of whole OCR words
   on one line, glued or not. It never matches part of a word (`5 mg` is
   not in `25mg`). The check is word presence on screen, not per field.

7. **The identity band moves with a wide control's structural offset**
   ([#1720](https://github.com/OpenAdaptAI/OpenAdapt/issues/1720)). A click
   recorded on a text field's left-aligned placeholder resolves at replay to
   the field's center, and the band is translated by that gap, so it reads
   only part of the patient banner and the click is refused. The test app
   centres its field's placeholder so the two coincide.
8. **Right after a sheet closes, OCR often reads the banner's `O` as `0`**
   (`0kafor`), and the identity check refuses the next click. Four of the five
   clean sheet runs stopped this way, before the save, with nothing written.

## A real sheet

`--sheet` shows the notice as an `NSAlert` sheet: its own window, attached to
the chart window. All runs are from a clean tree with a steady caret:

| Run | Code | Result |
| --- | --- | --- |
| [run 1](results/macos-ax-sheet-steady-caret-20261103-run1.json) | `3e44044d5` | Stopped in the probe at the note field's identity check (finding 8); nothing written |
| [run 2](results/macos-ax-sheet-steady-caret-20261103-run2.json) | `3e44044d5` | The same |
| [run 3](results/macos-ax-sheet-steady-caret-20261103-run3.json) | `3e44044d5` | `VERIFIED`: halt, "Later" in the sheet admitted on `element_and_record_check`, probe, replay and fault campaigns, the scripted approval, and exactly one new row |
| [run 4](results/macos-ax-sheet-steady-caret-20261103-run4.json) | `3f50a7065` (review fixes) | Stopped in the probe at the note field's identity check (finding 8); nothing written |
| [run 5](results/macos-ax-sheet-steady-caret-20261103-run5.json) | `3f50a7065` | The same |

In every run the stop came before any save, the sheet's "Later" button was
read and admitted through AX, and the bridge click on the sheet passed the
window checks. Finding 8 is what keeps the rest from completing.

## Windows UI Automation (lab VM)

`qualify_windows.py` runs the same check on the lab's Patient Notes WinForms
app through `WindowsBackend` and `win_agent`. The record check reads a fresh
copy of the guest's SQLite file over WinRM. With `interrupt=after_search`,
the app shows an owned "Session notice" message box after the search. The box
covers the right part of the patient header, the next step's identity band
([screenshot](results/windows-uia-20261103-session-notice.png)). Run it with
`OAFLOW_WINLAB_SCRIPTS` set to a folder holding the lab's `patient_notes.ps1`
and `pn_db.py` (the copies with the `interrupt` knob), plus the lab helpers
`qualify_lab_native.py` and `winrm_lab.py` on `PYTHONPATH`. The JSON records
each app file's SHA-256.

**It doesn't pass yet. In each run, the stop came before any save and
nothing was written.**

| Run | Result |
| --- | --- |
| [before #1724](results/windows-uia-20261103-before-1724.json) | Recorded and certified. The governed run stopped at the note field: identity mismatch, `REJECTED_POLICY`, never repaired ([#1724](https://github.com/OpenAdaptAI/OpenAdapt/issues/1724)) |
| [after #1724 and #1727](results/windows-uia-20261103-center-rule.json) | The same stop: the read `Patient: Neil Sorenson (1984-` is the recorded band word for word up to the dialog's edge, and the rule accepts it offline. The live `owned_window_over` answered None (finding 9) |
| [after #1735, #1739 and #1740](results/windows-uia-20261103-no-dialog-box.json) | The stop is reclassified live (`repaired after step_002 (after)`), and the scripted "OK" click passes the dismiss guard (the same window, a session notice). The patch then refuses it: no dialog box was found around the click (finding 10) |

9. **`owned_window_over` on Windows answers only when the owned window covers
   the band's center.** The session notice starts at x 388, and the band runs
   from x 227 to x 430, so the dialog meets the band but not its center at
   x 328. The agent's `/window/owned-over` returned `window: None`, although
   the recorded window and the z-order were right. The macOS hook answers on
   any overlap, and the #1724 rule needs only that: the text left uncovered
   must still be the recorded band, cut only at its end.
10. **The new dismiss step looked for the dialog by pixels, not by the
    proved window.** A click admitted on the record check carries the dialog
    around it as its identity band. That box came from the accessibility
    ancestry (a `dialog` role) or a drawn box around the click, and neither
    was found for the Windows message box. A covered-band repair now uses the
    window `owned_window_over` proved as that box.
