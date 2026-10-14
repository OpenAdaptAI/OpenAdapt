# Legacy v0.46.0 install: reproduction and fix

Recorded 2026-10-14 on macOS 15 (Apple silicon) with uv-managed CPython
3.11.15 and 3.10.21. Each run used a new virtual environment with pip 26.2.1.

## What happened

`pip install --dry-run openadapt==0.46.0` on Python 3.11 exited 1:

```text
Downloading openai-whisper-20240930.tar.gz
...
ModuleNotFoundError: No module named 'pkg_resources'
ERROR: Failed to build 'openai-whisper' when getting requirements to build wheel
```

v0.46.0 pins `openai-whisper>=20240930,<20240931`, which PyPI has only as a
source archive. Its `setup.py` imports `pkg_resources`. pip builds it in an
isolated environment with the newest setuptools, and setuptools 81 and later
no longer include `pkg_resources`. v0.46.0 declares `Requires-Python
>=3.10,<3.12`, so pip doesn't offer it on 3.12 or later.

## Fix

Cap setuptools for builds only:

```bash
echo 'setuptools<81' > build-constraints.txt
python -m pip install --build-constraint build-constraints.txt openadapt==0.46.0
```

| Run | Result |
|---|---|
| Python 3.11, bare `pip install --dry-run` | exit 1 (above) |
| Python 3.11, `--build-constraint`, `--dry-run` | exit 0 |
| Python 3.10, `--build-constraint`, `--dry-run` | exit 0 |
| Python 3.11, `--build-constraint`, full install | exit 0; `import openadapt` works; openadapt 0.46.0, openai-whisper 20240930, torch 2.0.1 |

`--build-constraint` appears in `pip install --help` from pip 25.3 on (25.2
doesn't have it). A new Python 3.11 virtual environment starts with pip 24.0,
so the documented steps upgrade pip first. Linux and Windows weren't tested.
