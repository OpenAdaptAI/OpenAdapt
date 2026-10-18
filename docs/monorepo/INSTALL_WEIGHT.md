# Install weight

Each package in this repository is published on its own. A package depends
only on what it imports, and dependencies point one way:

```text
openadapt-types, openadapt-privacy   (leaves: shared models, redaction)
        -> openadapt-capture         (the recorder)
        -> openadapt-flow            (the engine)
        -> openadapt-agent           (the MCP server)
        -> openadapt-desktop, openadapt (the app and the launcher)
```

Heavy stacks, such as plots, microphone narration, the browser driver and model
backends, are opt-in extras. Someone who installs `openadapt-privacy` to redact
data never gets Flow, Desktop, OpenCV, onnxruntime or torch.

The `install weight` CI job (`.github/workflows/packages.yml`) enforces this. It
builds every in-tree package, installs each one alone into a fresh virtual
environment with Python 3.12, and fails if the install:

- pulls a distribution that isn't in that package's allow list,
- pulls a forbidden distribution, even one that an allow pattern matches,
- doesn't import, or
- exceeds its size budget (site-packages) or its distribution count.

The budgets are in
[`scripts/install_weight/budgets.json`](../../scripts/install_weight/budgets.json).

## Run it locally

```bash
python scripts/install_weight/weigh.py check
python scripts/install_weight/weigh.py check --only privacy --only types
```

You need `uv` on `PATH`. Each environment is deleted after it's measured. The
allow lists cover the macOS and Linux dependency sets. A Windows run reports
the Windows-only dependencies (for example `pywinauto`) as not allowed.

In-tree wheels are built as `<version>+weight`. That local version label still
satisfies every `>=`, `<` and `==` specifier on the release version, and it sorts
above the same release on PyPI. So when one in-tree package depends on another,
the install uses the in-tree wheel. `privacy` and `desktop` aren't in `packages/`
yet. Until they are, the check installs their latest PyPI releases, and once a
directory exists it weighs the in-tree build instead.

## Change a budget

If a new dependency is intended, add it to that package's `allow` list in the
same pull request, and say in the description why the package needs it.
Prefer an extra when only one feature needs the dependency. Raise `max_mb` or
`max_dists` only with a measurement. The CI job prints a table and uploads
`install-weight.json`.

## Measurements

The measurements below are from 2026-10-18 on macOS 15.7.3 with an Apple M2
Max, using uv 0.11.29 and Python 3.12. Size is the environment's
`site-packages` directory before any bytecode is compiled. Import time is
`import <module>` in a new interpreter: "first" includes compiling bytecode,
and "warm" is the median of three runs after that. The raw data is in
[`install-weight-data/`](install-weight-data/).

### Before: the published releases

| Package | Release | Distributions | Size | Import, first / warm |
|---|---|--:|--:|--:|
| openadapt-types | 0.23.0 | 29 | 293.2 MB | 1.08 s / 0.36 s |
| openadapt-privacy | 1.1.3 | 7 | 19.0 MB | 0.03 s / 0.02 s |
| openadapt-capture | 1.3.0 | 41 | 131.7 MB | 6.96 s / 0.54 s |
| openadapt-flow | 2.2.0 | 28 | 293.2 MB | 1.01 s / 0.25 s |
| openadapt-agent | 2.2.1 | 46 | 298.9 MB | 0.96 s / 0.32 s |
| openadapt-desktop | 0.19.0 | 58 | 147.9 MB | (no import cost) |
| openadapt | 1.17.0 | 55 | 439.0 MB | 0.03 s / 0.02 s |

openadapt-types 0.23.0 was a shim over Flow, so it installed Flow's OpenCV,
RapidOCR and onnxruntime to deliver Pydantic models. openadapt-capture 1.3.0
installed matplotlib, sounddevice, soundfile, pympler, alembic and mako, though
a default recording uses none of them.

### After: this tree

| Package | Distributions | Size | Import, first / warm | Change |
|---|--:|--:|--:|---|
| openadapt-types | 6 | 7.8 MB | 0.007 s / 0.004 s | Pydantic only; the models load on first use |
| openadapt-privacy | 7 | 19.0 MB | 0.02 s / 0.03 s | unchanged (PyPI 1.1.3) |
| openadapt-capture | 24 | 83.2 MB | 6.54 s / 0.43 s | plots, narration and profiling are extras |
| openadapt-flow | 29 | 293.5 MB | 0.99 s / 0.17 s | adds openadapt-types; no longer builds every model at import |
| openadapt-agent | 47 | 299.2 MB | 1.09 s / 0.18 s | adds openadapt-types |
| openadapt-desktop | 44 | 100.8 MB | (no import cost) | PyPI 0.19.0, resolved against the in-tree capture |
| openadapt | 56 | 439.2 MB | 0.02 s / 0.02 s | adds openadapt-types |

The capture "first" import time is mostly the first load of the macOS PyObjC
frameworks.

On Linux (aarch64, `python:3.12-slim-bookworm` with `libxcb1`), the same tree
measures as follows: types 6 / 7.9 MB, privacy 7 / 28.7 MB, capture 20 / 107.9
MB, Flow 29 / 328.6 MB, agent 47 / 334.5 MB, desktop 40 / 130.3 MB, launcher 58
/ 476.9 MB. That Linux run predates the lazy `openadapt_types` root, so its
import times are not comparable. The budgets are the Linux sizes plus about 25
percent.

### `uvx openadapt quickstart --help`

The launcher keeps `openadapt-flow[browser,hosted]` and `openadapt-agent` as
base dependencies, so `uvx openadapt quickstart` works in one step.

| | Cold (empty uv cache) | Warm |
|---|--:|--:|
| openadapt 1.17.0 from PyPI | 20.4 s | 0.26 s |
| this tree's wheels | 21.6 s | 0.16 s |

A cold start downloads and unpacks about 900 MB, so download speed sets the
time: Playwright, OpenCV, onnxruntime and RapidOCR are the largest wheels.
`--help` doesn't download Chromium; the first browser action does.

## Known follow-ups

- On Linux, `import openadapt_capture` needs the `libxcb` system library
  (through `xcffib`) even for `capture info`. Hosted runners have it. Minimal
  containers don't.
- Flow's `uv.lock` predates the fold and still pins openadapt-types 0.21. CI
  doesn't enforce it.
