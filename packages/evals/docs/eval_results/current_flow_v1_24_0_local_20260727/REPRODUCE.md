# Reproduce this evidence from a clean checkout

Everything below runs on one machine with no cloud VM, no hosted runner, no
model API, and no paid provider. The only network access is the initial download
of the pinned wheel from PyPI.

## 1. Pin the exact engine artifact

```bash
python - <<'PY'
import hashlib, json, pathlib, urllib.request
VERSION = "1.24.0"
EXPECTED = "170fdac154794292c99dc6eea6486e7a2c3fdf321bcd87976d924bccd3db4aef"
meta = json.load(urllib.request.urlopen(
    f"https://pypi.org/pypi/openadapt-flow/{VERSION}/json"))
wheel = next(f for f in meta["urls"] if f["packagetype"] == "bdist_wheel")
path = pathlib.Path(wheel["filename"])
path.write_bytes(urllib.request.urlopen(wheel["url"]).read())
digest = hashlib.sha256(path.read_bytes()).hexdigest()
assert digest == EXPECTED, f"wheel digest mismatch: {digest}"
print(path, digest)
PY
```

Expected artifacts for `openadapt-flow` 1.24.0:

| Artifact | SHA-256 |
|---|---|
| `openadapt_flow-1.24.0-py3-none-any.whl` | `170fdac154794292c99dc6eea6486e7a2c3fdf321bcd87976d924bccd3db4aef` |
| `openadapt_flow-1.24.0.tar.gz` | `2d4702e5ccbdfed0f78063ca510dc82894a2833edbed71d77edffbd0ffebd67d` |

## 2. Check out the release-tagged, tracked-clean engine source

Both runners refuse to proceed unless the source checkout is tracked-clean and
its `HEAD` carries the tag matching the wheel's `__version__`, so the report can
never bind a wheel to unrelated source.

```bash
git clone https://github.com/OpenAdaptAI/openadapt-flow.git flow-1.24.0
git -C flow-1.24.0 checkout v1.24.0   # commit 4ca7566b73154769398d3135507060fa020aad0a
```

## 3. Create the measurement environment

```bash
uv venv --python 3.12 .venv-evidence
uv pip install --python .venv-evidence/bin/python \
    "openadapt-flow==1.24.0" "playwright==1.61.0" requests
.venv-evidence/bin/python -m playwright install chromium
```

Flow is imported from the locally extracted wheel, not from this install; the
install only provides the dependency set. Both runners assert that the imported
`openadapt_flow` resolves inside the extracted wheel directory and abort
otherwise.

## 4. Run the comparison and the transaction probe

```bash
.venv-evidence/bin/python scripts/run_current_flow_local_benchmark.py \
  --flow-source flow-1.24.0 \
  --flow-wheel openadapt_flow-1.24.0-py3-none-any.whl \
  --out out/comparison

.venv-evidence/bin/python scripts/run_flow_transaction_probe.py \
  --flow-source flow-1.24.0 \
  --flow-wheel openadapt_flow-1.24.0-py3-none-any.whl \
  --out out/transaction_probe --no-fail-on-violation

.venv-evidence/bin/python scripts/extract_over_halt_regression.py \
  --results out/comparison/results.json --condition clean \
  --out out/comparison/clean_postcondition_over_halt.json
```

Drop `--no-fail-on-violation` to make a violated invariant exit non-zero. It is
passed here so the artifacts are still written when an invariant fails; the
violations recorded in this directory reproduce with it either way.

## 5. Environment this evidence was measured on

| | |
|---|---|
| Platform | `macOS-15.7.3-arm64-arm-64bit` (Apple silicon) |
| Python | 3.12.7 |
| Playwright | 1.61.0 |
| Chromium | 149.0.7827.55 (Playwright-managed, headless) |
| Flow wheel | `openadapt_flow-1.24.0-py3-none-any.whl`, SHA-256 `170fdac1…4aef` |
| Flow source | `4ca7566b73154769398d3135507060fa020aad0a`, tag `v1.24.0` |
| Evals commit | `1fec68532e70…` |
| Comparison runner | SHA-256 `ac58c0b9a02cfc991144a1f5c7a2814c6b6b748bcba27b87f8e1027ee575970f` |
| Model calls / cost | 0 / $0.00 |

The comparison runner digest is identical to the one used for the published
1.16.1 report, so the *harness* — task script, arms, oracle, trial count, and
retry policy — is byte-identical across the two measurements.

**The two measurements are not otherwise apples-to-apples.** The MockMed target
application ships *inside* the Flow wheel (`openadapt_flow/mockmed/`), so
changing the pinned wheel changes the engine **and** the application it is
measured against. Between `v1.16.1` and `v1.24.0` the fixture changed in
`openadapt_flow/mockmed/static/app.js` and `static/styles.css`; the load-bearing
change is [`c416b7d`][c416b7d] ("export real public demo evidence pack"), which
added a patient banner to the top of the New Encounter form.

That single fixture change is the whole cause of the `clean` over-halt delta
reported in `COMPARISON_TO_v1_16_1.md`. With the banner present, the band the
1.16.1 bundle had mined as its `step_010` `region_stable` postcondition is
identical before and after the Save click, so the compiler's largest-changed-
region search moves down onto the saved-encounter row — which renders the
trial-unique `note` parameter. Read: the over-halt difference is attributable to
the bundled application, not to a change in engine reliability. Every counted
number in this directory stands as measured; only the attribution is corrected.

[c416b7d]: https://github.com/OpenAdaptAI/openadapt-flow/commit/c416b7d404ab6480351ec1d1809bcc26bdee1b4a

## 6. Expected variation

`clean` compiled over-halts reproduced 2/3 in both the primary run and the
independent replication under `replication/`, with the same failing region. It
is reproducible, not a flake — but it is not 3/3, because the surviving trial
turns on the exact glyphs of that trial's unique note (see the attribution note
in §5), so absolute counts may differ by one trial on other hosts. Timings are
host-specific. Outcome classifications (task success, silent incorrect, wrong
action, transaction outcome, business effect) should not vary.
