# Compiled replay vs. computer-use agent — comparison artifact

A self-contained, theme-aware HTML page that packages the core wedge in one
place: for a task you have **already demonstrated**, a compiled replay is
**model-free**, **~$0 per run**, and **faster** than a computer-use agent — at
**parity** success on these tasks.

It is **generated from the repo's real benchmark results**, not hand-typed. The
generator reads the two existing `results.json` files and lays their figures out
as charts and tables. It runs nothing costly: **zero Anthropic calls, zero
network, deterministic.**

It shares the design vocabulary of the
[wrong-patient safety gallery](../safety_gallery/README.md) — the same CSS
custom-property palette, the same light/dark theming, the same card and
honest-limits patterns — so the two artifacts read as a matched set.

## Sources (every figure comes from these)

| figure | source |
|---|---|
| OpenEMR — 20 compiled vs 10 agent, p50/p95, cost/run, cost total | [`benchmark/openemr/results.json`](../openemr/results.json) |
| MockMed — 100 compiled vs 20 agent, p50/p95, cost/run, drift row | [`benchmark/results.json`](../results.json) |
| one-time record + compile cost ("about a minute of human demonstration") | prose in [`benchmark/openemr/BENCHMARK.md`](../openemr/BENCHMARK.md) + [`benchmark/BENCHMARK.md`](../BENCHMARK.md) — the **only** prose-sourced figure, labelled as such |

Nothing on the page is invented. The `derived` figures (p50 speed-up and the
illustrative repeat-run model-cost table) are plain arithmetic on the measured
numbers: `agent p50 / compiled p50` and `measured model $/run × N`. They are
labelled as projections, not new measurements or total-cost claims.

## What it shows

Leads with the **real third-party result** (OpenEMR public demo, an 18-step
add-patient-note workflow on a live EMR), then the **CI-reproducible anchor**
(MockMed, the bundled demo clinic). For each benchmark:

- **Success parity** — both arms pass the same arm-independent OCR check.
- **Measured model API cost** — `$0` for compiled vs `$/run` for the agent, as
  an inline-SVG bar chart plus an explicitly limited arithmetic projection.
- **Latency** — p50/p95 wall-clock for both arms as grouped inline-SVG bars.

Charts are inline SVG (axis, gridlines, emphasized endpoints, tabular-nums
labels) — no screenshots, no external assets, no base64 needed.

It ends with an **honest "Read before quoting these numbers"** panel (mirroring
the gallery's "What still slips" tone): small N and wide error bars; the lead is
a field result on a shared, daily-resetting public demo (not CI-reproducible);
list-price costs with hard caps; the single OCR success check that errs
conservative on both arms; and the fact that this measures cost/latency at
**parity** success — **not** a general capability claim.

## Regenerate

```bash
python -m benchmark.comparison_artifact.generate
```

Rewrites both artifacts in this directory:

- `comparison.html` — the self-contained page (inline CSS + inline SVG, no
  external assets; theme-aware). Open it directly or lift it onto the website.
- `comparison.json` — the exact figures extracted from the source files, with
  their provenance, so the page is verifiable without eyeballing it.

## Test

[`tests/test_comparison_artifact.py`](../../tests/test_comparison_artifact.py)
asserts the loaded figures equal the source `results.json` files exactly and
that the emitted HTML carries the real headline numbers — a guard against the
template drifting away from the data. The tests need no browser, OCR, model, or
network.
