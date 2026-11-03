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

## Results, 2026-11-02 (macOS 15, Apple silicon)

**AX passes end to end, with the app's caret held steady** (`--steady-caret`,
finding 5). With a blinking caret it stops safely at the save. Pixels only
doesn't pass yet. No run wrote a wrong row.

| Check | AX (`MacOSBackend`) | Pixels only (`RemoteDisplayBackend`) |
| --- | --- | --- |
| Demonstration recorded and compiled, certified `clinical-write` | Yes | Yes |
| New version stops at the notice before any save | Yes (`HALTED_BEFORE_EFFECT`, `after`) | No: see finding 3 |
| The new "Later" click is read and admitted | Yes: `element_and_record_check (button 'Later')`, ancestry `AXSheet "Update available"`, then `AXGroup`, then `AXWindow "Chart"` | Not reached |
| Probe up to the save: nothing written, target found, same record, same fields | Yes | Not reached |
| Replay campaign and fault campaign (another patient's chart stops before the save) | Yes, 1/1 each | Not reached |
| One person's approval, through the decision portal | Yes | Not reached |
| Resumed run, checked against SQLite | `VERIFIED`, exactly one new row for the patient (steady caret); refused at the save with a blinking caret, nothing written | Not reached |
| With a `demo` profile the click is refused, nobody is asked, nothing is written | Yes | Not reached |
| With no verifier the run gate refuses, nothing is written | Yes | Yes |

The AX run needs [#1710](https://github.com/OpenAdaptAI/OpenAdapt/issues/1710)'s
fix (`MacOSBackend.act_structural`) and the glued-words `same_fields` check.

Evidence: [`results/macos-ax-20261102.json`](results/macos-ax-20261102.json)
(steady caret), [`results/macos-pixel-20261102.json`](results/macos-pixel-20261102.json).

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
2. **macOS sheets and separate dialog windows can't take input.** A real
   `NSAlert` sheet is its own CoreGraphics window, so `MacOSBackend`'s window
   binding refuses input to it, and raises after the click that opened it.
   The test app therefore draws its notice inside the window, as many desktop
   apps do. Windows update prompts are often separate top-level windows too;
   a backend that pins one window needs a rule for the app's own owned
   dialogs before repair can reach them.
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
   out. `--steady-caret` launches the app with AppKit's blink period set so
   the caret stays on; that run is the `VERIFIED` one above.
6. OCR glues a field's words together (`REPAIREDRUNFOLLOWUPINTHREEWEEKS`), so
   the probe's `same_fields` check on a screen with no form to read now also
   accepts a value of two or more words found on one line with its words
   joined.

Not covered here: Windows UI Automation and RDP to the Windows lab. They wait
on `WindowsBackend.element_identity_at` and the lab.
