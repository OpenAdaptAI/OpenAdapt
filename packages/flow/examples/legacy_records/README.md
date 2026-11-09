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
| Read-back | Read the chart before the write; close, reopen by record id, read clipboard and OCR; the triage entry count must have gone up by one | Close, reopen by bill id, read clipboard and OCR, plus the live-session export (Ctrl+E) and the scheduled export (a separate process) |
| Best grade | `reopened` for the record id and subject id (read before the write and again after reopening). `same_screen` for the triage values, which are only read after reopening. | `system_of_record`, from the scheduled export of saved data |

## Outcomes

| Outcome | Referral triage | Invoice posting | Review queue |
|---|---|---|---|
| `committed` | Triage entry written; the reopened chart shows it and one more entry than before | Bill entered; the reopened bill and the scheduled export show it | Only for values that need review |
| `no_action` | The physician chose `decline` (`reason_code` `declined`). Evidence: the chart search result before the decision and the same search run again after it (clipboard and OCR), and for one matching chart its triage entry count before the decision and after reopening. | The controller chose `hold` or `reject` (`held`, `rejected`). Evidence: the supplier's bill count on screen before the decision, and in the scheduled export after it. | Only with `--review-decisions` or `"policy": {"review_decisions": true}` in the answers file |
| `halted` | More than one chart matches, the chart shows a different subject, the save was dropped, an exception was not settled, the no-write evidence is too weak, or a screen did not appear | The supplier is missing or different, the bill cannot be reopened or exported, an exception was not settled, the no-write evidence is too weak, or a screen did not appear | Always |

Both workflows find the record before asking for the decision, so the screen read before the decision is the state a later read must match. A halt reason names what a save keystroke may already have created, such as a new chart saved before its triage entry.

A `no_action` result never counts as a save and is never billable. Its evidence must reach the grade a commit of the same workflow carries: `same_screen` for the referral (the weakest grade of a committed triage entry) and `system_of_record` for the invoice. Weaker evidence halts the run instead.

Grades are names from the check-level ladder and follow the reading step's rule: `reopened` needs a read in the acting session and a read of the reopened record, and `system_of_record` needs a read that does not go through the app's screens.

## Run the demo

```bash
cd packages/flow
FLOW_PYTHON=.venv/bin/python APP_PYTHON=/usr/bin/python3 \
  examples/legacy_records/demo.sh /tmp/legacy-records-demo --record
```

The script starts an Xvfb display, seeds both apps, and runs three scenes:

1. **Referral triage.** Three faxes: a new patient whose health-card number was faxed with the letter O (staff correct it), an existing chart (one match), and a name and date of birth that match two charts (the run halts and writes nothing).
2. **Dropped save.** The same template against an EMR that shows "Saved" but keeps nothing. After reopening, the chart's triage entry count has not gone up, and the run halts. This also catches a re-run of the same fax, where an earlier entry with the same values is still on screen.
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
| Exceptions and the approval | `pipeline.py`: a pattern or confidence check, then named people from the terminal or `answers*.json`. The person who confirms cannot be the person who approves. In scripted mode, an exception the answers file does not settle is not confirmed and the document halts; a scripted decision must be one of the options. |
| Write and read back | `x11.py`: keys only; observes the window title, the screen (RapidOCR) and the clipboard after Ctrl+A, Ctrl+C |
| Grade | `evidence.py`: derived from the reads. An export's `source_kind` decides whether it counts as independent (`committed_store`) or as a screen read (`live_session`). |
| Exchange, checksum, L1 | `openadapt_flow.interchange` (`check`, `with_checksum`, `emit_exchange`) |
| Seal | `evidence.py`: Ed25519 over the checksum with a key that exists only in memory |

The runs make no model calls.

## What is a stand-in

- **The driver.** `x11.py` is a scripted driver. In a deployment the app steps are a recorded and compiled Flow workflow run by the governed runtime over RDP, with its identity gate, postconditions and repair.
- **The approval.** It is recorded as an interchange decision, not yet as a Flow `BusinessDecisionSpec` with a signed request and receipt.
- **The Seal.** It is a prototype signature. It will be replaced by offline Seals with a local site key.
- **Grades.** The rule is `openadapt_flow.records.schema.grade_for`, applied through `openadapt_flow.interchange.conformance.supported_grade`.
- **The run path.** A deployment runs a compiled Flow workflow over RDP. This demo uses the scripted driver, labelled as a prototype.
- **OCR look-alikes.** The demo shows OCR reading `TEST-R1` as `TEST-Rl`. The value goes to the review queue rather than counting as confirmed. Look-alike-aware comparison is separate work.
