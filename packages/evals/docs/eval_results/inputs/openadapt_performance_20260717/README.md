# Committed inputs for the 2026-07-17 performance report

These files are the compact, non-sensitive inputs used to generate
`docs/eval_results/openadapt_performance_20260717.{json,md}` from a clean
checkout.

- `frappe_results.json` and `openemr_results.json` are the exact retained
  synthetic model-free summary artifacts. They contain no customer data,
  credentials, model content, screenshots, or raw oracle records.
- `legacy_compact.json` is a sanitized derivative of 48 ignored historical
  WAA summary/execution files. It retains task instructions, normalized
  outcomes, timings, step counts, and the SHA-256 of each raw input. It omits
  screenshots, action payloads, and model content. The retained historical
  artifacts do not identify the model or runner revision, so those fields stay
  explicitly unknown.

The current summaries retain hashes and relative locators for raw oracle and
run artifacts, but those raw files are not committed. The summaries also do
not embed the Flow Git revision. Candidate `84c7a94` is therefore recorded only
as a post-hoc worktree association, not an exact runtime binding.

Regenerate into temporary files with:

```bash
python3 scripts/report_openadapt_performance.py \
  --frappe-results docs/eval_results/inputs/openadapt_performance_20260717/frappe_results.json \
  --openemr-results docs/eval_results/inputs/openadapt_performance_20260717/openemr_results.json \
  --legacy-input docs/eval_results/inputs/openadapt_performance_20260717/legacy_compact.json \
  --flow-candidate-association 84c7a94f2d2ca9e183799394d1952ae32fa6bf92 \
  --flow-current-commit db87e3ffe802a94046f0f131da6094dac9a0fbd7 \
  --evals-report-base-commit 24a3108dc4a2c301895881d06172a2d280518dfc \
  --evidence-date 2026-07-17 \
  --out-json /tmp/openadapt-performance.json \
  --out-md /tmp/openadapt-performance.md
```

The focused regression test regenerates both outputs from these inputs and
requires byte-for-byte equality with the committed reports.
