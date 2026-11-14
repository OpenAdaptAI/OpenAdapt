# CHANGELOG

## v0.2.0 (2026-11-14)

### Install without the vision stack

- Depend on `openadapt-flow[receipts]>=3.0.0,<4`. Flow 3's base install has
  no vision, OCR, browser or GUI stack, so `pip install openadapt-verify`
  installs 21 distributions and 41 MB of site-packages instead of 33 and
  302 MB (uv install, Python 3.12, macOS arm64; 67 MB instead of 348 MB with
  pip, which also writes bytecode). The direct Pillow requirement is gone.
- Support Python 3.10, 3.11 and 3.12. The 3.11 floor came from the Pillow
  advisory, and Pillow is no longer installed.
- `tests/test_import_boundary.py` runs every verify module, the quickstart and
  both plain-Python helpers in an interpreter that refuses NumPy, OpenCV,
  Pillow, RapidOCR, ONNX Runtime, Playwright, Torch and the GUI modules.

### Features

- `openadapt-verify audit`: run your own automation through an HTTP fault
  proxy and count the wrong saves its success check reports as success
  (`no_persist`, `rejected`, `partial`, `wrong_value`, `duplicate`,
  `extra_write`, `lost_response`, and `wrong_record` when given a field),
  judged by the contract's independent read path. The fault goes to the
  first write whose body carries a declared value, or that matches
  `--write-path`. Prints a table and writes a
  JSON report. `--demo` audits two bundled scripts against a synthetic store.
- `openadapt-verify quickstart`: three scripted agent writes (correct, wrong
  value, claimed but not saved) against REST, SQLite and FHIR R4, and
  PostgreSQL and MySQL when given a disposable database. Each gets
  `CONFIRMED`, `REFUTED` or `UNCERTAIN` with a checked signed receipt.
- `@verified(contract)`: Flow's `WriteGuard` as a decorator for sync and async
  functions.
- `checked_write(contract, values)`: the canonical `begin_write` and
  `verify_write` pair as a context manager, with a portable write receipt.
- `mysql` extra (PyMySQL, MIT). No PostgreSQL extra: its read-only-capable
  drivers are LGPL-3.0.

The verifier, ledger, journal, charset guards and receipt code are Flow's;
this package adds no check of its own.
