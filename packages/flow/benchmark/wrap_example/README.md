# `openadapt wrap --example`, real model runs

One file per real run of `openadapt wrap --example` with a real model key:
the command's printed output (`.txt`) and its `example-result.json` (`.json`).
The local folder path has been removed from both. CI runs the same loop with a
scripted model client (`tests/e2e/test_wrap_example_e2e.py`).
