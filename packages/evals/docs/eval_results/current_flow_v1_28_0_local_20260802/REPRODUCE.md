# Reproduce this evidence from a clean checkout

Everything below runs on one machine with no cloud VM, no hosted runner, no
model API, and no paid provider. The only network access is the initial download
of the pinned wheel from PyPI.

## 1. Pin the exact engine artifact

```bash
python - <<'PY'
import hashlib, json, pathlib, urllib.request
VERSION = "1.28.0"
EXPECTED = "4d156035ea411e3cbdbc40978d653d50727a7d8664646be62f7f9e95ba0c7202"
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

Expected artifacts for `openadapt-flow` 1.28.0:

| Artifact | SHA-256 |
|---|---|
| `openadapt_flow-1.28.0-py3-none-any.whl` | `4d156035ea411e3cbdbc40978d653d50727a7d8664646be62f7f9e95ba0c7202` |
| `openadapt_flow-1.28.0.tar.gz` | `6e108f3469da20226427fee7a11378e067ca08c1fbbfcdbb4018ab9c3d142a8b` |

## 2. Check out the release-tagged, tracked-clean engine source

All three runners refuse to proceed unless the source checkout is tracked-clean
and its `HEAD` carries the tag matching the wheel's `__version__`, so a report
can never bind a wheel to unrelated source.

```bash
git clone https://github.com/OpenAdaptAI/openadapt-flow.git flow-1.28.0
git -C flow-1.28.0 checkout v1.28.0   # commit b646276a086c74b65ba850cdef2e475ca53f10c0
```

## 3. Create the measurement environment

```bash
uv venv --python 3.12 .venv-evidence
uv pip install --python .venv-evidence/bin/python \
    "openadapt-flow==1.28.0" "playwright==1.61.0" requests
.venv-evidence/bin/python -m playwright install chromium
```

Flow is imported from the locally extracted wheel, not from this install; the
install only provides the dependency set. Every runner asserts that the imported
`openadapt_flow` resolves inside the extracted wheel directory and aborts
otherwise.

Playwright is not a core `openadapt-flow` dependency from 1.25.0 onward: browser
support lives in the `browser` extra (`openadapt-flow[browser]`). The explicit
`playwright==1.61.0` pin above supplies it, and pins the exact driver these
numbers were measured with.

## 4. Run the comparison, the transaction probe, and the lease-safety probe

```bash
.venv-evidence/bin/python scripts/run_current_flow_local_benchmark.py \
  --flow-source flow-1.28.0 \
  --flow-wheel openadapt_flow-1.28.0-py3-none-any.whl \
  --out out/comparison

.venv-evidence/bin/python scripts/run_flow_transaction_probe.py \
  --flow-source flow-1.28.0 \
  --flow-wheel openadapt_flow-1.28.0-py3-none-any.whl \
  --out out/transaction_probe --no-fail-on-violation

.venv-evidence/bin/python scripts/probe_remote_lease_safety.py \
  --flow-source flow-1.28.0 \
  --flow-wheel openadapt_flow-1.28.0-py3-none-any.whl \
  --out out/remote_lease_safety

.venv-evidence/bin/python scripts/extract_over_halt_regression.py \
  --results out/comparison/results.json --condition clean \
  --out out/comparison/clean_postcondition_over_halt.json
```

Drop `--no-fail-on-violation` to make a violated transaction invariant exit
non-zero. It is passed here for symmetry with the 1.24.0 evidence, which needed
the artifacts written while an invariant failed. On this release no invariant
failed, so the probe exits 0 either way.

`replication/` is the identical comparison command run a second time into a
separate output directory, with no other change.

## 5. Environment this evidence was measured on

| | |
|---|---|
| Platform | `macOS-15.7.3-arm64-arm-64bit` (Apple silicon) |
| Python | 3.12.13 |
| Playwright | 1.61.0 |
| Chromium | Playwright-managed, headless |
| Flow wheel | `openadapt_flow-1.28.0-py3-none-any.whl`, SHA-256 `4d156035…c7202` |
| Flow source | `b646276a086c74b65ba850cdef2e475ca53f10c0`, tag `v1.28.0` |
| Evals commit | `71edc889035d998cac518ddf69b42860730533d7` |
| Comparison runner | SHA-256 `68f9e5a27f4f04d831574167ebd6b362bf05184e81708a315d9896969b48a126` |
| Probe runner | SHA-256 `097cc900b44872712ebd7a5bfbdb53f87c85bf5bd8dcbef88fa893743d240ce8` |
| Lease-safety runner | SHA-256 `2d0cf696df9779446469f0e94eb2f22e3e7634ac924998ebd93735b8d5a6791d` |
| Model calls / cost | 0 / $0.00 |

## 6. Why this comparison is controlled

**Both measurement harnesses are byte-identical to the 1.27.1 run.** The
comparison runner is `68f9e5a2…8a126` in both, and the probe runner is
`097cc900…40ce8` in both. Nothing in either file changed between the two
evidence sets, so no delta below can be an artefact of measuring differently.
This is the first release-over-release comparison in this directory where the
comparison runner did not move at all.

**The bundled application is byte-identical too.** MockMed ships inside the Flow
wheel (`openadapt_flow/mockmed/`), so pinning a different wheel can repin the
application under test. Between `v1.27.1` and `v1.28.0` that tree is unchanged
at `f0736c7a82ca2aba2beb334feaf461c5f06532a5`. Verify it without trusting this
file:

```bash
for tag in v1.27.1 v1.28.0; do
  git -C flow-1.28.0 rev-parse "$tag^{tree}:openadapt_flow/mockmed"
done
```

The fixture caveat that governed the 1.16.1 -> 1.24.0 comparison therefore does
not apply, exactly as it did not apply to 1.24.0 -> 1.27.1.

## 7. The lease-safety probe is a runtime-contract measurement

`scripts/probe_remote_lease_safety.py` does not drive a browser or a server. It
supplies a fake backend that implements only the two-phase remote actuation
lease — the exact protocol surface a pixel-only no-DOM canvas backend exposes —
and measures what the runtime does with a consequential remote click. It
therefore proves a runtime contract, not the behaviour of a real Citrix or RDP
session. Its scope block says so, and no wider claim is made from it.

## 8. Expected variation

Compiled replay was 3/3 correct in every condition in both the primary run and
the independent replication under `replication/` — 18 counted compiled trials
with zero over-halts, zero silent incorrect successes, zero wrong actions, and
zero model calls. Timings are host-specific; see `COMPARISON_TO_v1_27_1.md` for
the one timing figure that moved and for what this design can and cannot
conclude about it. Outcome classifications (task success, silent incorrect,
wrong action, transaction outcome, business effect) should not vary.
