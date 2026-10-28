# Local grounding model on the pixel path

On Citrix, RDP and VNC the replayer sees only pixels. The template rungs
compare the recorded crop with the live frame pixel for pixel, so an icon
drawn at another display scale, zoom level, font size or theme, or through a
lossy codec, is not found. This benchmark measures the local grounding model
rung ([`runtime/local_grounder.py`](../../openadapt_flow/runtime/local_grounder.py))
against the template matcher on one real application rendering, under each of
those changes.

## What the rung does

1. An edge search proposes boxes where the recorded crop's edges recur, at
   0.8x to 2.0x its size. Edge strength does not depend on colour, so a dark
   theme still proposes the right boxes.
2. DINOv2-small (Meta, Apache-2.0, 22M parameters, int8 ONNX, 24 MB) embeds
   each box and the recorded crop. Their patch tokens are compared place by
   place, weighted by where the crop has content, and against a
   luminance-inverted copy of the crop as well.
3. Every box that scores like the target is a look-alike. The resolver keeps
   one only when it is the sole look-alike and no located landmark disagrees,
   or when every located landmark singles out the same one. Overlapping
   look-alikes mean the crop is a stretch of blank field and cannot be
   located by its look; the rung then abstains and the ladder continues as
   before. Any other count halts the step.

The model only locates. A target with a text label is left to OCR. The
identity check, the look-alike census and the postconditions run on the
model's point exactly as on any other rung. The rung sits below OCR, so an
irreversible step it resolves still stops at the risk gate. It runs only on a
backend without page structure.

It is off by default (`runtime.local_grounding: true` or `--local-grounding`
on `replay`, `run` and `console`). The weights are fetched once from a pinned
Hugging Face revision and refused unless their SHA-256 matches; an offline
site places the file under `OPENADAPT_FLOW_MODEL_DIR`. Queries make no
network call.

## Choosing the model

Requirements: open weights under a permissive licence (no GPL, AGPL, LGPL or
non-commercial terms), about 1–3B parameters or fewer, about 100 ms per query
on CPU, and no cloud call. Licences were read from each model's Hugging Face
card on 2026-10-28.

| Model | Licence | Size | Outcome |
|---|---|---|---|
| Qwen3-VL-2B-Instruct (4-bit, MLX) | Apache-2.0 | 2B | Measured on an M2 Max: 0.4–2.3 s per query. Asked for one of two identical icons, it returned one box; asked for all, it found one of the two. |
| ShowUI-2B, UGround-V1-2B, UI-TARS-2B-SFT, GUI-Actor-2B | MIT or Apache-2.0 | 2B (Qwen2-VL-2B) | Not measured. Same architecture and size as the model above, and each returns one point per description, so it cannot count look-alikes. |
| OS-Atlas-Base-4B / Pro-4B | Apache-2.0 | 4B | Over the size budget. |
| Holo1.5-3B and other Qwen2.5-VL-3B derivatives | Qwen research licence | 3B | Excluded: non-commercial. |
| OmniParser v2 `icon_detect` | AGPL-3.0 | YOLOv8 | Excluded. |
| OmniParser v2 `icon_detect_v3` | MIT | YOLOv9-E, 281 MB | Needs PyTorch at run time (about 700 MB), against the install-weight budget. |
| Florence-2-base | MIT | 0.23B | Not measured. A general phrase grounder, not trained on screens. |
| DINOv2-small (int8 ONNX) | Apache-2.0 | 22M | Chosen. Runs on the onnxruntime the base install already carries, so the `local-grounding` extra adds no package. |

A generative grounding model answers "where is the Save button?" with one
point. The fail-closed contract needs the opposite question: how many places
look like the recorded control? A model that scores every candidate against
the recorded crop answers that, and the crop is stronger evidence than a
description of it.

## Setup

[`fixture/emr.html`](fixture/emr.html) is a clinic scheduling screen drawn by
Chromium: a toolbar of icon-only buttons, a worklist where every row has the
same edit and cancel icons, and a form with two floppy-disk icons that differ
only in colour (Save and Save draft). The replayer reaches it through a
pixel-only backend ([`fixture_backend.py`](fixture_backend.py)): screenshots
in, clicks and keys out, no DOM. The harness reads the page only after a run,
to score which control each click landed on and what was saved.

The workflow is recorded once at 100% display scaling, light theme, with the
production Recorder, then compiled. It opens Ada Lovelace's row, types a visit
type and a note, attaches the summary (toolbar icon) and saves (floppy icon).
It is replayed under 13 conditions:

| Axis | Conditions |
|---|---|
| Display scaling (frame grows, layout unchanged) | 100%, 125%, 150%, 200% |
| Application zoom (frame unchanged, content grows and reflows) | 110%, 125%, 150% |
| Font size (text grows, layout reflows) | +2 px, +4 px |
| Theme | dark at 100% and 150% |
| Lossy codec (JPEG quality 50, 4:2:0, as VNC Tight or a Citrix lossy codec) | at 100% and 150% |

Each condition runs three campaigns: **healthy** (a correct run saves one
appointment for Ada with the replay values), **wrong record** (the active
record switches to Grace Hopper before Save; a correct run saves nothing) and
**reordered** (Ada is on another row; a correct run opens her row or stops).
A **wrong target** is a click on a control the workflow never uses, or
anywhere in another patient's row.

The replay runs as an RDP or Citrix bundle does: the layout check and the
display-scale estimate ([`benchmark/pixel_scale`](../pixel_scale/README.md))
are on.

## Results

### Finding the target

[`grounding_probe.py`](grounding_probe.py) shows each recorded click target to
the template matcher and to the rung, on the raw frame of each condition (no
display-scale normalisation). The two icons are the targets the rung exists
for:

| Condition | Attach icon: template | Attach icon: rung | Save icon: template | Save icon: rung |
|---|---|---|---|---|
| 100% | found | found | found | found |
| 125% | not found | found | not found | found |
| 150% | not found | found | not found | found |
| 200% | not found | found | not found | found |
| zoom 110% | not found | found | not found | found |
| zoom 125% | not found | found | not found | found |
| zoom 150% | not found | found | not found | found |
| font +2 px | found | abstains | found | found |
| font +4 px | found | abstains | found | found |
| dark, 100% | not found | abstains | not found | found |
| dark, 150% | not found | abstains | not found | found |
| codec, 100% | found | found | found | found |
| codec, 150% | not found | abstains | not found | found |
| **Total** | **8 of 26** | **21 of 26, 0 wrong** | | |

On the Save icon the rung told Save from the Save draft icon (same glyph,
grey instead of blue) under every condition: the true icon scored 0.86–0.98,
the best other place 0.45–0.52.

Across all 65 click targets (the two icons, two blank input fields and the
row's key text), the rung picked the target 21 times, halted on look-alikes
once, abstained 43 times and **picked a wrong target 0 times**. The blank
fields abstain because their crop matches all along the field; the row's key
text is left to OCR. The template matcher found 15 of 65 and picked no wrong
target.

Query time on an M2 Max, 4 CPU threads, median (max): 113 ms (292 ms) on a
1280x800 frame, 151 ms (274 ms) at 1600x1000, 154 ms (344 ms) at 1920x1200,
211 ms (534 ms) at 2560x1600. Data: [`results_grounding.json`](results_grounding.json).

### Whole runs

[`run_sweep.py`](run_sweep.py), 39 runs per arm:

| | Template ladder | With the grounding model |
|---|---|---|
| Healthy runs that saved correctly | 2 of 13 | 2 of 13 |
| Reordered runs that saved correctly | 2 of 13 | 2 of 13 |
| Wrong-record runs that stopped with nothing saved | 13 of 13 | 13 of 13 |
| Wrong targets, wrong saves, false completions, crashes | 0 | 0 |
| Steps resolved by the model | – | 0 |

Per run, with the step and check that stopped it:
[`results_template.json`](results_template.json),
[`results_model.json`](results_model.json).

The runs pass at 100%, plain and through the codec, in both arms. Everywhere
else a check stops the run before the rung is needed:

- **Display scaling.** The display-scale estimate resamples each frame to the
  recorded size, and the template rungs then find every target up to the
  attach icon. There, the look-alike census counts Ada's name twice (in the
  active-record banner the step's identity is bound to, and in her worklist
  row) and halts.
- **Zoom and font size.** The layout check stops the first step: the text is
  1.1x to 1.5x its recorded height, which it treats as a changed screen.
- **Dark theme, and the codec at 150%.** The identity check of the first form
  field cannot match the recorded identity template of the banner and stops
  the run.
- **Reordered rows at another scale.** The layout check stops the first step.

So on this fixture the model's ability to find icons is not yet what limits a
whole run on Citrix or RDP. The layout check, the identity template and the
census decide first, and they fail closed. Each is a separate change with its
own safety review.

## What this does and does not show

- It is one application drawn by Chromium, not a live Citrix ICA/HDX or RDP
  session. The codec rows model lossy compression with JPEG, not a real
  remote-display codec.
- Display scaling is modelled as Chromium's device scale factor: the layout
  stays put and every pixel grows. Zoom and font size reflow the layout.
- The rung was tuned on this fixture (the acceptance score, look-alike margin
  and landmark tolerance). Its first version picked a wrong target on a blank
  input field 4 times in 65 here; the self-similar-crop check and the stricter
  landmark rule removed them. A new fixture can show cases this one does not.
- Latency is from one M2 Max, under load from other jobs on the host.

## Reproduce

```bash
python benchmark/pixel_grounding/run_sweep.py --arm template --output results_template.json
python benchmark/pixel_grounding/run_sweep.py --arm model --output results_model.json
python benchmark/pixel_grounding/grounding_probe.py --output results_grounding.json
python benchmark/pixel_grounding/summarize.py results_template.json results_model.json results_grounding.json
```

Needs Playwright's Chromium. The model arm and the probe fetch the 24 MB
weights on first use. Each sweep takes about 8 minutes, the probe about 2.
