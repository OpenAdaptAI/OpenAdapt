# Public demo captures

The two captures in this directory drive the screenshot workflow in
`openadapt-viewer`. The generator uses public JSON specs and OpenAdapt-owned
drawing code. It doesn't read a desktop, personal account, voice, transcript,
device name, or third-party image asset.

| capture | synthetic workflow | frames | actions |
|---|---|---:|---:|
| `turn-off-nightshift` | change a display setting in a generated settings UI | 20 | 20 |
| `demo_new` | compute `2 X 3` in a generated calculator UI | 14 | 14 |

Run this command after a source spec or the renderer changes:

```console
uv sync --locked --no-install-project --python 3.12.13
uv run --locked --no-sync python -m scripts.generate_synthetic_captures
```

The generator writes `recording.db`, `capture-state.json`,
`synthetic-provenance.json`, `capture-artifact-manifest.json`, and
`capture-terminal.json`. `CaptureSession.load_verified()` checks the terminal,
manifest, database rows, frame hashes, source ordinals, and each before/after
action binding. CI runs the generator again and compares every output byte.

## Evidence boundary

The JSON source declares every synthetic timestamp and action coordinate. The
provenance file records the exact source and generator hashes, the full frame
timeline, and every action's two frame ordinals. The database repeats that
provenance in its recording config. The provenance also binds the exact SQLite
and SQLAlchemy builder versions that produce the committed database bytes.

The seal proves artifact integrity. These fixtures don't measure an operating
system, application, recorder hook, or human action. Their machine-readable
provenance sets `qualification_eligible` to `false`, so they cannot support a
workflow qualification claim.
