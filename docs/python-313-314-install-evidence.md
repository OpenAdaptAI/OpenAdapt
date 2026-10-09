# Python 3.13 and 3.14 install: reproduction and fix

Recorded 2026-10-08 and 2026-10-09 on macOS arm64 with Homebrew Python
3.12.9, 3.13.7, and 3.14.6, pip 26.2.1, and uv 0.11.29. Each pip run used a
new virtual environment.

## What happened

`pip install openadapt` on Python 3.13 or 3.14 installed openadapt 1.3.0, an
old CLI with no `quickstart` command, and reported no error.

| Interpreter | `pip install --dry-run openadapt` resolved |
|---|---|
| 3.12.9 | openadapt 1.16.0, openadapt-flow 1.35.1, openadapt-types 0.17.1, click 8.5.0 |
| 3.13.7 | openadapt 1.3.0, click 8.5.0 |
| 3.14.6 | openadapt 1.3.0, click 8.5.0 |

## Why

PyPI metadata, read from `https://pypi.org/pypi/<name>/json` on 2026-10-08:

- openadapt 1.7.0 through 1.16.0 declare `Requires-Python <3.13,>=3.10`.
- openadapt 1.0.0 through 1.6.4 declare `>=3.10` with no upper bound. 1.4.0
  through 1.6.4 depend on openadapt-flow. 1.3.0 depends only on click.
- Every openadapt-flow release that 1.4.0 through 1.6.4 accept needs
  rapidocr-onnxruntime, whose newest release (1.4.4) declares `<3.13,>=3.6`.
  openadapt-flow 1.35.1 and openadapt-agent 2.0.1 also declare `<3.13`.

On 3.13, pip skips every launcher with the upper bound, can't satisfy the
engine for 1.4.0 through 1.6.4, and settles on 1.3.0. The launcher's own
Python check never ran, because that launcher was never installed.

uv ignores Requires-Python upper bounds, so `uv tool install --python 3.14
openadapt` installed openadapt 1.16.0 with openadapt-flow 1.35.1 on 3.14.
`openadapt quickstart` then stopped with the launcher's Python message.

## Fix

- `requires-python = ">=3.10"` with no upper bound, so pip installs the
  current launcher on any newer Python.
- Every openadapt-flow and openadapt-agent requirement, in the base install and
  in every extra, carries `python_version < '3.13'`.
- On an unsupported interpreter, `openadapt` help, `quickstart`, `flow`,
  `connect`, `doctor`, and `deploy` print the fix:
  `uv tool install --python 3.12 openadapt`.
- `tests/test_python_support.py` keeps the marker, the classifiers, the CLI
  range, and the CI guard in step. The `unsupported-python-guard` CI job builds
  the wheel on 3.13 and 3.14 and resolves `openadapt`, `openadapt[browser]`,
  and `openadapt[capture]` by name with PyPI in view. It fails unless pip
  picks this launcher with no engine. It then installs the wheel and checks
  that each engine command prints the fix.

## Check of the fix

A wheel built from this change with its version set to 1.99.0, placed in a
`--find-links` directory next to PyPI, stands in for the next release:

| Interpreter | `pip install --dry-run --find-links <dir> openadapt` resolved |
|---|---|
| 3.12.9 | openadapt 1.99.0, openadapt-agent 2.0.1, openadapt-flow 1.35.1, openadapt-types 0.17.1, click 8.5.0 |
| 3.13.7 | openadapt 1.99.0, click 8.5.0 |
| 3.14.6 | openadapt 1.99.0, click 8.5.0 |

Rechecked 2026-10-09 with the same stand-in and pip 26.2.1. Every extra a
user is likely to ask for also resolves to the new launcher, so no install
command backtracks to 1.3.0:

| Request on 3.13.7 and 3.14.6 | OpenAdapt packages pip resolved |
|---|---|
| `openadapt`, `openadapt[browser]`, `openadapt[privacy]`, `openadapt[agent]` | openadapt 1.99.0 |
| `openadapt[capture]` | openadapt 1.99.0, openadapt-capture 1.2.2 |
| `openadapt[ml,evals]` | openadapt 1.99.0 with the research packages, no openadapt-flow |
| `openadapt[all]` | openadapt 1.99.0 with capture and the research packages, no openadapt-flow |

As a negative control, restoring `requires-python = ">=3.10,<3.13"` and
running the CI guard's resolve step on 3.13.7 fails, because pip chooses
openadapt 1.3.0.

The fix reaches users only with the next launcher release. Until then, pip on
3.13 and 3.14 still resolves to 1.3.0.

## uv installs

Recorded 2026-10-09 with uv 0.11.29 on macOS 15.7.3 arm64, with Homebrew's
`python3` (3.14.6) first on PATH and an empty uv Python directory, which is
what a new Mac user has.

The installer at `https://openadapt.ai/install.sh` runs
`uv tool install --upgrade openadapt` with no `--python`. The installer in
openadapt-flow (`scripts/install.sh`) passes `--python 3.12`.

- Today, with 1.16.0 on PyPI, `uv tool install openadapt` picks Python 3.14,
  because uv ignores the `<3.13` upper bound. It installs openadapt 1.16.0 and
  openadapt-flow 1.35.1 on 3.14. `openadapt quickstart` then stops with the old
  remedy text.
- With the 1.99.0 stand-in, the same command installs the launcher alone on
  3.14. `openadapt` and `openadapt quickstart --break-it` print the fix and
  exit with status 1.
- Running the printed fix, `uv tool install --python 3.12 openadapt`, replaces
  that tool environment. uv reports that the requested interpreter doesn't
  match the existing environment, then installs openadapt-flow 1.35.1 and
  openadapt-agent 2.0.1 on Python 3.12.9.

## The engine on Python 3.14

In the 1.16.0 tool environment above, where uv installed Flow on 3.14 despite
its metadata, `openadapt flow tutorial` reached `VERIFIED` on Python 3.14.6.
`openadapt flow tutorial --break-it` halted with transaction outcome
`RECONCILIATION_REQUIRED`, as it does on 3.12. That environment had
rapidocr-onnxruntime 1.4.4, onnxruntime 1.31.0, and playwright 1.63.0.

This is one browser tutorial on one Mac, not a support claim. It does show
that the block is mostly published metadata: openadapt-flow 1.35.1,
openadapt-agent 2.0.1, and rapidocr-onnxruntime 1.4.4 all declare
`Requires-Python <3.13`. Supporting 3.13 and 3.14 for real takes releases in
those repositories first (Flow without the cap and with an OCR dependency that
installs on 3.13, then the agent bridge), a Flow CI matrix on 3.13 and 3.14,
and then a wider marker and `_SUPPORTED_PYTHON` here.

## Printed next step

The Flow 1.35.1 tutorial ends with `openadapt-flow record --backend web --url
<your app>`. `uv tool install openadapt` puts only `openadapt` on PATH, so that
command fails for users of the installer script. The launcher now rewrites
Flow's printed `openadapt-flow <verb>` commands to `openadapt flow <verb>`
during the tutorial. A real `openadapt quickstart --break-it` run with Flow
1.35.1 on Python 3.12 printed `openadapt flow record --backend web --url <your
app>` and no `openadapt-flow` command.
