# Reproduce this evidence from a clean checkout

Everything below runs on one machine with no cloud VM, no hosted runner, no
model API, and no paid provider. The only network access is the initial download
of the pinned wheel from PyPI.

## 1. Pin the exact engine artifact

```bash
python - <<'PY'
import hashlib, json, pathlib, urllib.request
VERSION = "1.27.1"
EXPECTED = "99d8f3ef014481356f4bcfc65f694ed2fd47a75e2025c5ddfdae4bfab2194094"
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

Expected artifacts for `openadapt-flow` 1.27.1:

| Artifact | SHA-256 |
|---|---|
| `openadapt_flow-1.27.1-py3-none-any.whl` | `99d8f3ef014481356f4bcfc65f694ed2fd47a75e2025c5ddfdae4bfab2194094` |
| `openadapt_flow-1.27.1.tar.gz` | `aeabf2b11ae6151fe76c02ee783e8368d10ee984465f858f9d37429b17a15468` |

## 2. Check out the release-tagged, tracked-clean engine source

Both runners refuse to proceed unless the source checkout is tracked-clean and
its `HEAD` carries the tag matching the wheel's `__version__`, so the report can
never bind a wheel to unrelated source.

```bash
git clone https://github.com/OpenAdaptAI/openadapt-flow.git flow-1.27.1
git -C flow-1.27.1 checkout v1.27.1   # commit ee52def4190fc08bc3ecdee8ea28a4aae205f1d7
```

## 3. Create the measurement environment

```bash
uv venv --python 3.12 .venv-evidence
uv pip install --python .venv-evidence/bin/python \
    "openadapt-flow==1.27.1" "playwright==1.61.0" requests
.venv-evidence/bin/python -m playwright install chromium
```

Flow is imported from the locally extracted wheel, not from this install; the
install only provides the dependency set. Both runners assert that the imported
`openadapt_flow` resolves inside the extracted wheel directory and abort
otherwise.

Note that from 1.25.0 Playwright is no longer a core `openadapt-flow`
dependency: browser support moved to the `browser` extra
(`openadapt-flow[browser]`). The explicit `playwright==1.61.0` pin above
supplies it, and pins the exact driver these numbers were measured with.

## 4. Run the comparison and the transaction probe

```bash
.venv-evidence/bin/python scripts/run_current_flow_local_benchmark.py \
  --flow-source flow-1.27.1 \
  --flow-wheel openadapt_flow-1.27.1-py3-none-any.whl \
  --out out/comparison

.venv-evidence/bin/python scripts/run_flow_transaction_probe.py \
  --flow-source flow-1.27.1 \
  --flow-wheel openadapt_flow-1.27.1-py3-none-any.whl \
  --out out/transaction_probe --no-fail-on-violation

.venv-evidence/bin/python scripts/extract_over_halt_regression.py \
  --results out/comparison/results.json --condition clean \
  --out out/comparison/clean_postcondition_over_halt.json
```

Drop `--no-fail-on-violation` to make a violated invariant exit non-zero. It is
passed here for symmetry with the 1.24.0 evidence, which needed the artifacts
written while an invariant failed. On this release no invariant failed, so the
probe exits 0 either way.

`replication/` is the identical comparison command run a second time into a
separate output directory, with no other change.

## 5. Environment this evidence was measured on

| | |
|---|---|
| Platform | `macOS-15.7.3-arm64-arm-64bit` (Apple silicon) |
| Python | 3.12.13 |
| Playwright | 1.61.0 |
| Chromium | Playwright-managed, headless |
| Flow wheel | `openadapt_flow-1.27.1-py3-none-any.whl`, SHA-256 `99d8f3ef…4094` |
| Flow source | `ee52def4190fc08bc3ecdee8ea28a4aae205f1d7`, tag `v1.27.1` |
| Evals commit | `8132e6078a7510f83193863165a958ec57e4818d` |
| Comparison runner | SHA-256 `68f9e5a27f4f04d831574167ebd6b362bf05184e81708a315d9896969b48a126` |
| Probe runner | SHA-256 `097cc900b44872712ebd7a5bfbdb53f87c85bf5bd8dcbef88fa893743d240ce8` |
| Model calls / cost | 0 / $0.00 |

**The transaction probe harness is byte-identical to the 1.24.0 run.** Its
runner digest is `097cc900…40ce8` in both. Every difference in
`transaction_probe/` is therefore attributable to the engine.

**The comparison runner changed, but only outside the measurement.** Its digest
was `ac58c0b9…5970f` for 1.24.0 and is `68f9e5a2…8a126` here. Exactly two
commits touched the file in between, and neither touches the arms, the oracle,
the classification rules, the trial count, or the retry policy:

- `1801027` replaced a hard-coded 1.16.1 reproduce string in the emitted
  `theme_postcondition_over_halt.json` with a helper that formats the measured
  version (12 lines, metadata only).
- `c4b7e9b` deleted one blank line, as part of a repository-wide ruff cleanup.

So the harness is functionally identical and the deltas in
`COMPARISON_TO_v1_24_0.md` are not an artefact of measuring differently.

**The bundled application also changed with the wheel.** MockMed ships inside
the Flow wheel (`openadapt_flow/mockmed/`), so pinning a different wheel changes
the engine and the application it is measured against. This is the same caveat
recorded for 1.24.0 and it still applies. Where a delta is attributable to a
named engine change rather than to the fixture, `COMPARISON_TO_v1_24_0.md` says
so and names the commit.

## 6. Expected variation

Compiled replay was 3/3 correct in every condition in both the primary run and
the independent replication under `replication/` — 18 counted compiled trials
with zero over-halts, zero silent incorrect successes, zero wrong actions, and
zero model calls. Timings are host-specific. Outcome classifications (task
success, silent incorrect, wrong action, transaction outcome, business effect)
should not vary.
