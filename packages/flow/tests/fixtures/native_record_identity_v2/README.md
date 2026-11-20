# Native record identity v2 fixture

This is a new synthetic qualification case. It exposes the loaded SQLite record
ID in a readonly standard WinForms `TextBox` named `currentRecordId`. The original
`patient_notes.ps1`, its save handler, and the older caption-based readback spec
remain unchanged. Earlier results retain their original case and denominator.

The fixture clears the ID, note and header before loading or changing records.
It publishes an ID only after the selected grid key, the actual `pn_db.py get`
result's ID and a fresh selection read agree. Missing, malformed or mismatched
records leave the ID empty. A failed list load also clears the selection and
list. The original save handler still saves to the selected grid key; the new
fixture adds no save guard.

This PR delivers fixture data and offline generation only. The generated
legacy-shaped readback template is nonauthoritative: it does not enforce exact
canonical-ID matching or admit a runtime. That consumer work remains separate.

`contract.json` declares the database mapping and a data-only readback template.
The generator applies exact overrides to a hash-pinned base and emits a distinct
script, pack and manifest. Run from `packages/flow`:

```sh
python tests/fixtures/native_record_identity_v2/build_fixture.py --check
```

The base pin uses LF bytes; checkout CRLF is explicitly converted to LF before
comparison. No other normalization occurs. Generated files preserve their
committed bytes on either platform. The caller supplies its owned readback
session; the pack contains no endpoint or credentials. Record IDs must be
canonical signed int64 strings, including zero and the integer boundaries.
Search selects a candidate. The intended contract requires exact canonical
record-ID comparison; the existing verifier also accepts delimited caption
values. This candidate remains held for a generic exact-value consumer contract
and planted invalid-display controls.
There is no virtual-grid or complete row-count claim.

Offline tests parse the entire generated PowerShell source and execute only its
actual database/load/selection functions, using stand-in controls and the real
SQLite CLI. They test reset, missing/ambiguous/invalid identity, wrong loaded row,
selection replacement during load, invalid list data and actual subprocess
failure. They also compare CLI IDs with independent SQLite reads and plant
incorrect mapping/schema controls. Nonempty notes prime every failure/reset
case so stale-value clearing controls are not vacuous. JSON root-shape checks
precede PowerShell pipeline enumeration. They open no GUI and send no desktop input.
These checks establish source behavior under those stand-ins, not native
WinForms behavior.

Native qualification is **unperformed**. The intended structural read uses
request contract `openadapt.native-value-read/v3` and the separate
`openadapt.native-value-completeness/v3` proof schema. These are source references
from frozen #2291 at `1a2503da4e240ba24620845cf85737d087c38e39`, with shared
helper commit `4be4b543da10286105090950c57a89d5e7a9a497` (Git blob
`78e47eb02e7498b5111766f25f3604e8ab78ee50`). They do not establish final protocol,
runtime or native qualification; all contract holds remain false or null.
Readonly identity uses `require_editable=False`; it is not an editable-field
baseline. Unsupported, sensitive, truncated or disagreeing values must refuse,
with no caption, preview or OCR fallback. The known-non-password guard and
mandatory `value_contract` negotiation remain prerequisites for future consumer
integration. This fixture does not qualify an older runtime.

A future native case must independently compare the selected key, loaded DB
row, actual managed control value and complete native UIA/Win32 observation,
then check the target and every sibling directly in SQLite. A complete text
observation alone does not prove that the text belongs to the saved row. The
protocol/source freeze, native binding evidence, combined approval campaign and
held-out evidence remain absent in `contract.json`. No prior failed campaign is
reclassified by this new case.
