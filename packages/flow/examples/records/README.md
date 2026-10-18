# Reading records: accounts ledger example

A mock accounts-ledger app (`accounts_app.py`) with three synthetic accounts.
`demo.py` reads each account into a typed record. Proof is opt-in; the demo
turns all of it on with `ReadProof(double_read=True, committed_readback=True,
seal=True)` and shows each step:

1. Double read: clipboard and OCR of each field in the same session.
2. Committed-record read-back: close each account, reopen it by account
   number and read it again.
3. Independent read: compare with the app's report export.
4. Review queue: one field is drawn wrong on screen (the clipboard and the
   export are right). It goes to review, and a person settles it with a signed
   decision.
5. Write gate: a step that needs `committed_record_readback` for `balance`
   halts for the account whose balance a person had to settle.
6. Export to CSV and SQLite with provenance columns.

Run from `packages/flow`:

```bash
python examples/records/demo.py --out /tmp/records-demo
python examples/records/demo.py --out /tmp/records-demo --simulated-ocr  # fast, no OCR engine
```

Agreeing fields reach `verified`. A field a person settled keeps the grade its
reads support, so its record exports as `unconfirmed`.

`templates/omop/` holds an OMOP CDM template, kept outside the core.
