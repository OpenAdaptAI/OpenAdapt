# Protocol: model-drafted write contracts, admitted by qualification

Written before the reported runs. Changes after a run go under
"Deviations" in [RESULTS.md](RESULTS.md), with the run they affect.

## Question

Can a model draft a system's write contract at setup time, from the system's
description and one trace, so that setup takes minutes instead of about an
hour? And does admission by qualification keep every admitted contract
honest: no planted fault missed, no false stop on a clean write?

The baseline is the Vikunja setup in the second first-contact retest: 64.5
minutes and 12 person decisions with the deterministic generator and hand
patches.

## Apps

| App | Store | Task | Description given | Tuned on before? |
|---|---|---|---|---|
| MockLoan (OpenAdapt's fault server) | SQLite | create a disbursement (JSON POST) | `db` | yes (zero-config benchmark) |
| Workbench, MockMed profile (OpenAdapt's task-generation app) | PostgreSQL | update a patient's phone and email (HTML form) | `db` | yes |
| Mealie v2.8.0 | PostgreSQL | update a recipe's description and yield (JSON PATCH) | OpenAPI (`/openapi.json`) | no: no agent had installed or probed it |

Mealie runs on the lab's WSL host (PostgreSQL 16.4, both pinned images),
reached from the Mac through an SSH tunnel to loopback. All data is
synthetic.

## Procedure per app

1. **Operator setup** (`apps.py`, timed separately). Bring the app to a
   seeded state, record one demonstration as a HAR file, and write
   `task.yaml` and `sandbox.yaml`. These are facts: URLs, sign-ins and
   connections.
2. **`openadapt verify draft`** with the pinned model (`claude-opus-5-5`,
   effort `medium`, prompt `verify-draft-prompt/1`). The benchmark never
   answers the one question; a question refuses the draft.
3. **Held out**, only for an admitted contract, with no model. A second
   qualification run, in which the harness chooses:
   - the values: 5 clean writes, and the faults written with other values;
   - the rows: the faults planted on other rows (seed 1).

   Two app-level faults that the admission suite never plants are added:
   - the same write also sent to another record through the app;
   - for a create, the write sent twice through the app.

## Measures

- **Setup minutes**: wall time of step 2, from the command's start to the
  admitted contract or the refusal.
- **Person decisions**: answers to the command's question. As in the
  Vikunja log, the facts in `sandbox.yaml` are listed as operator inputs,
  not decisions.
- **Admitted versus rejected drafts**: one draft per model round.
- **Planted-fault catches**: refuted faults over applicable faults, in the
  admission and held out.
- **False stops**: clean writes not confirmed, or whose recheck didn't
  confirm.
- **Model cost**: from each call's token counts at the model's list price,
  in the spend ledger (`results/spend.jsonl`).

## Kill criterion

The approach is killed if any of these holds:

- on the untuned app, setup takes more than 5 minutes;
- on the untuned app, setup takes more than 1 person decision;
- any admitted contract misses a planted fault.

The admission suite catches its own faults by construction. So a miss is
scored on the held-out faults as well.

## Spend cap

USD 30 in all, enforced by the ledger. A call is refused when the total plus
the worst case of one call would pass it.
