# Interactive run player

`player.html` is a single, self-contained web page that lets you **scrub
through a real compiled run** of openadapt-flow — the actual per-step
screenshots the replayer saved, with a decision overlay on every step. Open
it in any browser (no server, no network — every asset is inlined).

It shows the **same compiled workflow** replayed three ways so a cold viewer
can immediately see the two things that matter: it **heals** when the UI
changes, and it **stops** when something is wrong.

| Run | What it shows | Outcome |
| --- | --- | --- |
| **Baseline replay** | The UI it was recorded on; every target matches on the `template` rung. | 11/11, 0 heals, succeeded |
| **Theme drift — self-heals** | A theme it never saw; each moved target re-resolves via `geometry`/`ocr` and the fix is written as a reviewable diff. | 11/11, 8 heals, succeeded |
| **Surprise modal — halts** | A blocking survey pop-up appears instead of the save; the final postcondition never holds. | 10/11, **HALTS** at `step_010` |

For each step the overlay reports which **resolution rung** fired
(template / geometry / ocr / grounder), the identity verdict, whether it
**healed** (with the before→after anchor diff), and whether the expected
screen actually appeared (the postcondition). The halt is a first-class
moment: the banner turns red and the run stops loudly rather than reporting
a success that did not happen.

## Honesty

- **Real frames.** Every image is a genuine screenshot saved by the
  replayer during the run — nothing is staged or mocked up.
- **Model-free.** All three runs report `model_calls = 0` ($0.00/run). The
  resolution ladder and identity checks run with no VLM.
- **Synthetic app, real pipeline.** The app is **MockMed**, a fake EMR
  stand-in (the patient data is invented). The record → compile → replay
  pipeline and the captured pixels are real. This is labeled in the player.

## Regenerate

```bash
# Rebuild player.html + player_data.json from the committed run artifacts:
python -m benchmark.run_player.generate

# Also re-run the surprise-modal HALT replay from scratch (needs Playwright,
# still model-free) before rebuilding:
python -m benchmark.run_player.generate --regen-halt
```

The baseline and theme-drift runs are reused from `docs/showcase/`
(canonical committed artifacts). The HALT run is a real, model-free replay
of `docs/showcase/bundle` against MockMed with `?drift=modal`, saved under
`runs/modal-halt-run/`.

## Files

- `generate.py` — the generator (extracts real run data, builds the player).
- `player.html` — the self-contained interactive player (open in a browser).
- `player_data.json` — the extracted per-step decision data (metadata only,
  no image bytes) for inspection without a browser.
- `runs/modal-halt-run/` — the real HALT run's report + per-step frames.
