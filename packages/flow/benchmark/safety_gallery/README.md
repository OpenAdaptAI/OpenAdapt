# Wrong-patient safety gallery

A self-contained, case-by-case visual proof of openadapt-flow's wrong-patient
identity defense — and an honest account of what it still misses.

It is **generated from real evidence**, not hand-drawn:

- **Real renders.** Each patient row is painted with the same fixture the
  identity studies use
  (`openadapt_flow.validation.dense_surface.render_table_html`) via headless
  Chromium.
- **Real OCR.** Each row is read with the repo's own RapidOCR
  (`openadapt_flow.vision.ocr.ocr`) — no synthetic strings.
- **Real production gate.** Each recorded/live pair is judged by the shipping
  identity check, `openadapt_flow.runtime.identity.verify_target_identity`.
- **No model.** Zero Anthropic calls, zero VLM, zero network. This is the
  deterministic OCR/identity path.

## What it shows

For every case: the two rows as they paint on screen, a magnified crop of the
identifier cell, the byte-level OCR output of each row side by side (a true
glyph collapse reads **byte-identically** — the visceral "the computer cannot
tell them apart from pixels" moment), the gate's verdict, a plain-English
one-liner, and a SAFE/UNSAFE marker. It ends with a **"What still slips"**
section drawn from [`docs/LIMITS.md`](../../docs/LIMITS.md) and the
[fault-model study](../fault_model/FAULT_MODEL.md).

Cases (the real glyph classes plus the separator class from the 10th
wrong-patient reopening, plus two controls so the gate is provably not
trivially abstaining or verifying):

| case | recorded → live | kind | correct verdict |
|---|---|---|---|
| `O0_alphanumeric` | `MG4408` → `MG44O8` | wrong-patient trap | must NOT verify |
| `l1_alphanumeric` | `MG4118` → `MG41l8` | wrong-patient trap | must NOT verify |
| `numeric` | `100512` → `1OO512` | wrong-patient trap (9th reopening) | must NOT verify |
| `separator` | `MG-4408` → `MG-44O8` | wrong-patient trap (10th reopening) | must NOT verify |
| `sibling` | `MG5439` → `MG7263` (same name+DOB) | wrong-patient trap | must NOT verify |
| `clean_control` | `RC79284` → `RC79284` | control | MUST verify |
| `different_patient` | `RC44823` → `RC77235` | control | MUST mismatch |

A dangerous case is **safe** iff the gate does not VERIFY (mismatch, abstain,
and unreadable all HALT the run on a pure-pixel substrate).

## Regenerate

```bash
python -m benchmark.safety_gallery.generate
```

This rewrites both artifacts in this directory:

- `gallery.html` — the self-contained page (inline CSS, base64 images, no
  external assets; theme-aware). Open it directly or lift it onto the website.
- `results.json` — the machine-checkable record (each case's OCR strings,
  verdict, and safe/unsafe flag) so correctness is verifiable without eyeballing
  the page.

The generator exits non-zero and names the offending case if any dangerous
case ever VERIFIES a wrong patient.

## Identity path used

This gallery is generated on top of the identity code that includes the **10th
wrong-patient reopening** fix (separator-formatted collapsible MRNs). On that
path all five dangerous cases are correctly refused. On an identity build
WITHOUT that fix, the `separator` case verifies a wrong patient — the generator
would then flag it UNSAFE (a P0), which is exactly the regression the bundled
test guards against.

## Test

`tests/test_safety_gallery.py` has fast unit tests for the case fixture and the
safety-classification logic, plus one Playwright + OCR guarded end-to-end test
that runs the real generator and asserts every dangerous case is SAFE and both
controls are correct — so a future change to the identity path cannot silently
ship a gallery that verifies a wrong patient.
