# Native macOS qualification

## A third-party app: calibre

`qualify_calibre.py` records, compiles and runs a write workflow on
[calibre](https://calibre-ebook.com) 6.27 (GPL-3.0, Qt), an e-book manager
whose library is a SQLite database (`metadata.db`). calibre is a test target
only: the script drives the copy in `/Applications` on a throwaway library and
config folder, and nothing of it is copied or shipped.

The workflow sets one book's tag: click the book's title, click its Tags cell
(calibre opens its in-place editor with the tag selected), type the new tag,
and click the title again, which commits the edit. Every click lands on an AX
element with a name, so the bundle certifies under `clinical-write` with no
step needing an operator risk review.

```bash
python benchmark/macos_native/qualify_calibre.py --steady-caret
```

### Results, 2026-11-03 (macOS 15, Apple silicon, `MacOSBackend`)

Both runs are from clean trees, with the caret held steady and Apple Vision
OCR (`OPENADAPT_FLOW_OCR=native`).

| Check | [run 1](results/calibre-20261103-steady-caret-run1.json) (`d26f3e16f`) | [run 2](results/calibre-20261103-steady-caret-run2.json) (`954e2e400`) |
| --- | --- | --- |
| Demonstration sets the tag; bundle certified under `clinical-write` | Yes | Yes |
| Clean run (`standard`, run gate, durable, SQL record check) | Stopped at the Tags-cell click: its region was still changing at the postcondition; nothing written | `VERIFIED`; only the target book's tags changed |
| Planted fault: the book with that title is by another author | Stopped at the first click's identity check; nothing written | The same |
| Look-alike: two identical books | Refused as ambiguous before any input; nothing written | The same |

### Findings

1. **Qt cells had no window.** Qt's table cells and inline editors report no
   `AXTopLevelUIElement`, so `MacOSBackend` refused them. Their window is now
   found up the `AXParent` chain.
2. **Qt line edits take only `AXValue`.** Typed text is now spliced over the
   selection and checked by an exact read-back.
3. **A table cell's own text verified the wrong record.** With Qt cells
   reachable, the identity check read only the clicked title, so the
   wrong-author book verified (the run stopped later, at the caret). A table
   cell's identity is now its row; a Qt cell, whose row AX doesn't expose,
   abstains and the recorded band decides.
4. **Commit by key needs certification.** Pressing F2 and Return to edit and
   commit compiled to steps that need an operator's certified risk review, so
   the run gate refused the bundle. The workflow commits by clicking instead.
5. **A blinking caret** in the open editor makes the commit click's frame
   unstable ([#1713](https://github.com/OpenAdaptAI/OpenAdapt/issues/1713));
   `--steady-caret` sets AppKit's blink period in calibre's own defaults domain
   for the run (Qt follows it) and restores it afterwards.
6. **Timing.** RapidOCR takes over ten seconds on calibre's full window, past
   the frame lease, so the script uses Apple Vision; and one capture takes up
   to a few seconds on a loaded host, so the pointer-settle window is six
   seconds (two identical frames are still required).
