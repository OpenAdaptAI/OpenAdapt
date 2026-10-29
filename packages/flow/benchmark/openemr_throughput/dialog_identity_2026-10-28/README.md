# Identity at OpenEMR's duplicate dialog, before and after #1347

Measured on 2026-10-28 on the pinned local OpenEMR 8.0.0.3 fixture, with
synthetic patients only. One referral was recorded once with the #1347 engine,
so the confirm click carries `dialog_region`. It was compiled twice with
`scripts/openemr_local_demo.py`:
- with `main` (490d0dc40) as the baseline;
- with this branch, which reads the band on the dialog alone.

The OCR band was the full-width strip around the button, so it read a label of
the form behind the dialog. "Before" means `main` and "after" means this branch.

## The identity check at the confirm step, form scrolled behind the dialog

`scroll_identity.py` opens the real duplicate dialog with the New Patient form
scrolled to several positions behind it. It runs the replayer's identity check
for the confirm step of each bundle and saves nothing.

| Path | Positions | `main` false stops | Branch false stops |
|---|---|---|---|
| OCR band (base Playwright backend, as in #1347) | 8 | 8 | 0 |
| OpenEMR adapter's structured identity | 8 | 0 | 0 |

- **Before.** On the OCR path, `main` read "Title: Cancel", "DOB: Cancel" or "License/ID: Cancel", whichever label sat beside the dialog, and refused every time. The recording had read "County:".
- **After.** The branch read the dialog's own text and verified every time.
- **Adapter path.** With the adapter, the structured tier decides, so neither bundle reads the band.

Files: `scroll-ocr-tier.json` and `scroll-openemr-adapter.json`.

## Fail closed on a listed duplicate

`listed-duplicate.json`: the dialog for a patient who matches a seeded chart lists that chart. Both bundles refuse at both positions. The branch refuses because the dialog's listed row is not the empty result it recorded.

## Worklist batches

`batch-before-main.json` and `batch-after-branch.json` are throughput harness runs (`--items 26 --workers 1 --speed standard --backend openemr`).

| Bundle | Correct | Wrong or extra charts | Stops |
|---|---|---|---|
| `main` | 26/26 | 0 | 0 |
| Branch | 26/26 | 0 | 0 |

The `engine_sha` in these files is the harness checkout, not the engine. The baseline ran `main`'s `openadapt_flow`.

In these batches the form behind the dialog sat where it was recorded, so the baseline didn't hit the stop. The batches show that the branch holds no correct save. The scroll table above shows the stop itself.
