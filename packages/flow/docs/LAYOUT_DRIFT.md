# Layout drift

A recorded workflow assumes the screen looks the way it did at recording
time. A zoom change, a display-scaling change, a new default font, or a moved
panel breaks that assumption. Without a check, the run fails later with an
unexplained "target not found", or it acts on the wrong element.

`openadapt_flow.runtime.layout_drift` checks for this before acting and halts
with a named reason.

## How it works

`fingerprint(lines, viewport, landmarks=..., profile_sha256=...)` summarizes a
frame from its OCR lines:

- frame size
- median text height
- median character width
- the centre of each stable label (landmark), such as "Account number" or
  "Status"
- the remote session profile digest, when the frame comes from a pinned
  session

The fingerprint holds label positions and font metrics only. It does not
store field values.

`compare(recorded, live)` returns a `DriftVerdict` with every difference
outside tolerance. `check_layout` raises `LayoutDriftHalt` on any drift.

| Reason | Meaning |
| --- | --- |
| `resolution_changed` | Frame size differs. |
| `session_profile_changed` | The remote session profile digest differs. |
| `font_size_changed` | Median text height changed by more than 8%. |
| `font_changed` | Character width changed relative to text height by more than 10%. A pure zoom scales both together, so it is reported as a size change instead. |
| `layout_changed` | A label moved more than 12 px relative to the others. A whole-window move or scroll is not drift. |
| `landmark_missing` | A recorded label was not found. |
| `too_little_text` | Fewer than three readable lines, so font metrics can't be judged. |

## Recovery advice

`check_layout` accepts an optional `advisor`, such as a small local layout
model. It is called only after drift is found. Its text is attached to the
verdict as `advice` for the person reviewing the halt. The run halts either
way: the advisor can't change the verdict or authorize a step, and a healthy
frame makes no advisor call. If the advisor fails, the halt still happens and
the advice records the failure.

## Tests

`tests/test_layout_drift.py` covers each reason on synthetic OCR lines and
on a synthetic form rendered in a browser and read with the repository OCR.
The rendered cases are: unchanged, window moved (passes), font size 16 to 20
and 16 to 13 px, Arial to Courier, one row shifted 240 px, and viewport
1280x800 to 1440x900.

## Not yet done

The replayer does not call `check_layout` yet. A deployment that wants the
check must call it on the first frame of each step against the fingerprint
stored with the recording.
