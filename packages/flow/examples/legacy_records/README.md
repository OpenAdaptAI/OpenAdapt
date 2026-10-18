# Legacy records: reference workflows and proof-of-concept demo

> **PROTOTYPE. Synthetic data only.** Nothing here is production software, and nothing here is part of OpenAdapt's core. Every person, supplier, record id and document is made up.

This example shows OpenAdapt reading records out of desktop apps that have no API, writing records back, and handing the result across an open format with a checksum and a Seal reference. It runs two workflows with the same structure against two mock desktop apps:

| | Referral triage (template) | Invoice posting |
|---|---|---|
| App | Mock EMR (`apps/mock_emr.py`) | Mock Ledger (`apps/mock_ledger.py`) |
| Inbox | Fax PDFs in a folder | Scanned invoice PDFs in a folder |
| Exception confirmed by | Intake staff | Accounts-payable clerk |
| Single approval by | Physician (priority) | Controller (approve, hold, reject) |
| Identity branch | New chart, or one existing match; more than one match halts | Supplier must exist; a missing supplier halts |
| Read-back | Close, reopen by record id, read clipboard and OCR | Same, plus the ledger's CSV export |
| Best grade | `committed_record_readback` | `verified` (the export is read outside the screens) |

## Run the demo

```bash
cd packages/flow
FLOW_PYTHON=.venv/bin/python APP_PYTHON=/usr/bin/python3 \
  examples/legacy_records/demo.sh /tmp/legacy-records-demo --record
```

The script starts an Xvfb display, seeds both apps, and runs three scenes:

1. **Referral triage.** Three faxes: a new patient whose health-card number was faxed with the letter O (staff correct it), an existing chart (one match), and a name and date of birth that match two charts (the run halts and writes nothing).
2. **Dropped save.** The same template against an EMR that shows "Saved" but keeps nothing. The read-back after reopening finds no entry, and the run halts.
3. **Invoice posting.** Three invoices: one with an amount scanned as `1249.5O` (the clerk corrects it), one clean, and one for a supplier that is not in the ledger (the run halts).

It ends by checking every exchange it wrote with `python -m openadapt_flow.interchange check`. Outputs in the output folder:

- `out/*/` the exchanges (`*.json`), Seal files (`seals/`), read-back frames (`frames/`), the review queue (`review_queue/`) and `run.log`;
- `out/extractions/` the L1 directory with `manifest.csv`, written through `emit_l1_artifact`;
- `demo.mp4` with `--record`.

Requirements: `Xvfb`, `xdotool`, `xclip`, ImageMagick `import`, a Python with `tkinter` (and Xft fonts) for the apps, and an environment with `openadapt-flow` installed for the workflows. Over RDP, point `DISPLAY` at the session's X display and set `DEMO_XVFB=0`.

## What each step uses

| Step | Uses |
|---|---|
| Read the document | `openadapt_flow.intake` (`read_document`, `extract_fields`), local, no model |
| Exceptions and the approval | `pipeline.py`: a pattern or confidence check, then named people from the terminal or `answers*.json`. The person who confirms cannot be the person who approves. |
| Write and read back | `x11.py`: keys only; observes the window title, the screen (RapidOCR) and the clipboard after Ctrl+A, Ctrl+C |
| Grade | `evidence.py`: the reading step's `grade_for` rule |
| Exchange, checksum, L1 | `openadapt_flow.interchange` (`check`, `with_checksum`, `emit_exchange`) |
| Seal | `evidence.py`: Ed25519 over the checksum with a key that exists only in memory |

The runs make no model calls.

## What is a stand-in

- **The driver.** `x11.py` is a scripted driver. In a deployment the app steps are a recorded and compiled Flow workflow run by the governed runtime over RDP, with its identity gate, postconditions and repair.
- **The approval.** It is recorded as an interchange decision, not yet as a Flow `BusinessDecisionSpec` with a signed request and receipt.
- **The Seal.** It is a prototype signature. It will be replaced by offline Seals with a local site key.
- **Grades.** The rule matches `openadapt_flow.records.schema.grade_for` and uses that module when it is installed.
- **OCR look-alikes.** The demo shows OCR reading `TEST-R1` as `TEST-Rl`. The value goes to the review queue rather than counting as confirmed. Look-alike-aware comparison is separate work.
