# Screen-only grounding under drift: right, wrong and refused

On Citrix, RDP and VNC the replayer sees only pixels. This benchmark asks how
often the resolution ladder clicks the right control, clicks a wrong one, or
refuses, with no page structure available. It counts wrong clicks and
refusals separately. The thresholds were committed before measuring
([PREREGISTRATION.md](PREREGISTRATION.md), commit `f55800815`).

## Result

The pixel ladder on `main` clicked a wrong control 202 times in these
suites. After this change it clicks none, and it refuses or misses instead.

| Suite | Targets | Arm A: `main` | Arm C: this change | Arm D: this change + local model |
|---|---:|---|---|---|
| Drift-21 (screen only) | 21 | 6 right, **10 wrong**, 5 refused | 6 right, 0 wrong, 15 refused | 6 right, 0 wrong, 15 refused |
| Icon, checkbox and radio controls | 102 | 65 right, **6 wrong**, 17 refused, 14 missed | 70 right, 0 wrong, 17 refused, 15 missed | 70 right, 0 wrong, 31 refused, 1 missed |
| Real AVC444 frames (held out) | 81 | 80 right, 0 wrong, 1 refused | 80 right, 0 wrong, 1 refused | 80 right, 0 wrong, 1 refused |
| Live RDP, three link conditions | 1,428 | 1,006 right, **186 wrong**, 236 missed | 1,145 right, 0 wrong, 283 missed | 1,072 right, 0 wrong, 325 refused, 31 missed |

Arm B (`main` with the local model on) has the same wrong clicks as arm A:
10, 6, 0 and 186. Per-target rows are in [`results/`](results).

### Where `main` clicked the wrong control

- **Drift-21.** 15 of the 21 buttons moved to another column. Rows are 34 px
  apart and the template rung treated anything within 48 px of the recorded
  spot as the target, so for 10 moved buttons it clicked the next row's
  button. The OCR rung did the same when its padded search window held one
  copy of a repeated label.
- **Toggled checkboxes and radio buttons** (6 on the rendered page, 186 over
  live RDP). The recorded crop shows the box checked. After the boxes are
  toggled, the crop matches the neighbouring box that is now checked, 30 px
  away.

### What changed

- A template match off the recorded spot but inside the locality radius is
  kept only when nothing else on the whole frame looks like the widget, and
  when no recorded landmark is missing or puts the target elsewhere
  (`vision/match.py`, `runtime/resolver.py`).
- The OCR rung no longer treats one copy of a repeated label in its padded
  window as unique. If that copy is not where the target was recorded, the
  whole frame's evidence decides.
- The local model rung abstains when its one look-alike is off the recorded
  spot and none of the step's landmarks can be found.

No threshold was loosened. Each change turns a click into a refusal or a miss.

### The 15 drifted buttons are refused, not found

The drifted buttons on the Drift-21 page are identical to one another and are
relabelled from Open to View. Nothing in the pixels tells button 3 from
button 5. A screen-only method that "found" them would be guessing, so 6
right and 15 refused is the best safe score. The structural rung still finds
all 21 where page structure exists, and it still runs first.

The tables above describe the initial guard head `52c52b8e1` and its declared
benchmark conditions. They do not establish that a unique state-bearing crop
identifies the same control: after a toggle, another checkbox or radio button
can be the only exact match, near or far from the recorded spot.

The follow-up guard requires located retained context for an off-spot template
or a single local-model candidate. Missing context is checked before a far
unique match can be admitted. Independently qualified target OCR may still
resolve; otherwise an unsupported moved template causes a typed refusal before
weaker pixel fallbacks, including geometry, can revive it. Geometry's point and
confidence alone are not a control identity or state witness. This deliberately
refuses some legitimate appearance-only moves. The matcher may still propose a
unique appearance; the resolver decides whether it can authorize input. The
local model remains opt-in.

## Pre-registered checks

| Check | Threshold | Outcome |
|---|---|---|
| No wrong click in arms C and D (suites 1 to 3, plus live RDP) | 0 | **Met**: 0 everywhere |
| Drift-21 right answers in C and D, at least arm A's | 6 or more | **Met**: 6 |
| Controls: arm D right answers at least arm A's + 25% of targets | 91 or more | **Not met**: 70 |
| AVC444: arm C right answers at least arm A's - 3 | 77 or more | **Met**: 80 |
| Latency target from issue #1848 | 50 ms per query | **Not met** (see below) |
| Local model on by default | 0 wrong, p95 250 ms or less on both CPUs, no new AVC444 stop | **Not met**: p95 is over 1 s on 1080p icon frames. The model stays opt-in. |

The local model helps on a dark theme (live RDP: 82 to 105 right per link
condition, against 63 to 80 without it), but on toggled boxes it refuses 50 to
55 targets per link condition that the landmark geometry rung would have found.
Overall it lowered right answers over live RDP from 1,145 to 1,072. This is
the main reason it stays off by default.

## Suites

1. **Drift-21.** The 21-button page from
   [`benchmark/structural_action`](../structural_action/STRUCTURAL_ACTION.md),
   resolved with no structural backend.
2. **Controls.** Eight icon-only toolbar buttons (plain SVG shapes), two
   identical pencil icons told apart by their row labels, four checkboxes and
   three radio buttons with labels, rendered in Chromium. Six conditions:
   unchanged, 125% and 150% display scale, dark theme, JPEG quality 50 with
   4:2:0 chroma, every box toggled. Landmark positions come from OCR on the
   recorded frame, as the compiler takes them.
3. **AVC444.** The 81 click-step frames with ground truth from the real
   H.264/AVC444 RDP campaigns behind
   [`benchmark/rdp_avc_real`](../rdp_avc_real/README.md) (Windows Server
   2022 host, stock client).
4. **Live RDP.** The controls page in Edge (kiosk mode), on the Windows lab
   laptop, over RDP with AVC444 requested, captured with the product's
   `XFreeRDPTransport` and `FreeRDPBackend` at 1280x800. The page cycles
   unchanged, toggled and dark every 6 s and keeps a counter running, so the
   encoder keeps sending updates. Three link conditions through
   [`rdp_link_relay.py`](rdp_link_relay.py):
   - clean;
   - shaped: 1.5 Mbit/s, 0 to 80 ms jitter, 2% of chunks stalled 400 ms;
   - bad: 600 kbit/s, 0 to 150 ms jitter, 5% of chunks stalled 800 ms.

   The relay shapes TCP traffic, so packet loss appears as the stalls and
   late bursts that TCP retransmission causes, not as loss on the wire. The
   recording is the first unchanged frame of the clean run. Every fifth frame
   and the two frames after each page change are scored: 54 frames, 17
   controls each. Truth boxes are the page's own boxes, placed by a fiducial
   square found in each frame. Frame hashes, capture timings and three sample
   frames are in [`rdp_frames/`](rdp_frames); the full frames are kept outside
   the repository.

   The link conditions barely changed the outcome. Unchanged frames scored
   136 of 136, 153 of 153, and 169 of 170 right on the clean, shaped and bad
   links. Capture time per frame had a median of 66 to 72 ms on every link;
   the slowest frame took 185 ms (clean), 430 ms (shaped) and 976 ms (bad). A harsher link, packet-level
   loss, or a capture taken in the middle of a large repaint was not tested.

Frames at another display scale are handled as the replayer handles an RDP or
Citrix bundle: the scale is estimated from the anchors' landmark labels and
the frame is resampled into the recorded geometry. When there is no estimate
(at 150% here, in every arm), the layout-drift check stops the step, which is
counted as refused. Without that step, `main` clicked a wrong control 13 times
at 125% and 150% through the landmark geometry rung
([`results/arm_A_no_render_scale.json`](results/arm_A_no_render_scale.json)).
This change does not alter the geometry rung, so the same 13 would occur in a
replay that skips both checks.

## Latency (issue #1848)

[`latency.py`](latency.py) times `LocalGroundingModel.locate_anchor` on fixed
frame and crop pairs. It loads only `runtime/local_grounder.py` and its
pinned weights, which were downloaded on first use and checked against their
SHA-256 on the lab laptop. Each pair was run warm, 5 times.

| CPU | Frames | p50 | p95 |
|---|---|---:|---:|
| Apple M2 Max (this Mac, 4 threads, load 13) | 1920x1080, 17 small controls | 616 ms | 1,085 ms |
| Apple M2 Max | 1280x800 AVC444, 81 steps | 247 ms | 571 ms |
| AMD Ryzen 7 7730U laptop (Windows 11, 4 threads) | 1920x1080, 17 small controls | 537 ms | 1,144 ms |
| AMD Ryzen 7 7730U laptop | 1280x800 AVC444, 81 steps | 318 ms | 363 ms |

The 50 ms target is not met, and these numbers are well above the 133 ms that
#1596's probe measured for larger icons. Small crops (about 30 px) are the
slow case: the edge search runs at the crop's own resolution over nine scales
on the whole frame. Search on a fixed downscaled frame, try only the scales
near the render-scale estimate, and search near the recorded spot first.

## Model choice

No new model was adopted. The candidates checked for a fast, local,
permissively licensed stack:

| Candidate | Licence | Outcome |
|---|---|---|
| DINOv2-small, int8 ONNX | Apache-2.0 | Kept: the existing local rung (#1596). It runs on the onnxruntime Flow already ships. |
| PP-OCR (PaddleOCR) models | Apache-2.0 | Already the default text reader, through RapidOCR. Tesseract is not used on the default path. |
| Florence-2-base | MIT | Not adopted. A general phrase grounder that returns one box per phrase, so it cannot count look-alikes. It needs PyTorch and the model's own remote code. |
| RT-DETR | Apache-2.0 (code and COCO weights) | Not adopted. No permissively licensed weights trained on UI controls were found, and COCO classes are not controls. |
| OmniParser `icon_detect` | AGPL-3.0 | Excluded. |

In these suites the wrong clicks came from the rungs that resolve a target,
not from what a better detector could see. A better model would not have
removed them.

## Reproduce

```bash
python benchmark/pixel_drift/run_arms.py --arm C --output results/arm_C.json \
    --avc444 CAMPAIGN_DIR... --rdp-live CLEAN_DIR SHAPED_DIR BAD_DIR
python benchmark/pixel_drift/run_arms.py --arm D --local-grounding ...
python benchmark/pixel_drift/latency.py export PAIRS_DIR --avc444 CAMPAIGN_DIR...
python benchmark/pixel_drift/latency.py time PAIRS_DIR --output results_latency_HOST.json
```

Arms A and B ran the same harness against a checkout of `f55800815`
(`origin/main` at `c21ba89c4` plus the pre-registration). The live RDP
captures used `rdp_capture.py` in the lab's FreeRDP 3 container, with the
relay listening on the Mac's loopback.
