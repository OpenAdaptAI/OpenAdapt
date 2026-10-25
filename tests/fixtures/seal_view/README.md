# A real quickstart run, sealed

`run-1` (saved and checked) and `run-broken` (stopped: the save never reached
the record) from one `openadapt quickstart` run on 2026-10-20, in MockMed with
synthetic data, as the engine wrote them: `report.json` and its `seal.json`.
`keys.json` is the public half of the Seal key that signed them; the private
half is not in this repository. `program.json` is the program graph of
the run's bundle (`openadapt.seal_view.bundle_graph`, remote-safe projection).

tests/test_seal_view.py writes each run's offline Seal page from these files and
checks it in a browser.
