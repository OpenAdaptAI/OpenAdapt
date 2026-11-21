# Protocol: model-drafted write contracts, admitted by qualification

Written on 2026-11-21 after the first two Mealie runs and before the
second untuned app (Directus) was touched. What changed and when is under
"Addendum" below; every run, void or not, is in [RESULTS.md](RESULTS.md).

## Question

Can a model draft a system's write contract at setup time, from the system's
description and one trace, so that setup takes minutes instead of about an
hour? And does admission by qualification keep every admitted contract
honest: no planted fault missed, no false stop on a clean write?

The baseline is an earlier setup of a held-out app with the deterministic
generator and hand patches: 64.5 minutes and 12 person decisions (private
study).

## Apps

| App | Store | Task | Description given | Tuned on before? |
|---|---|---|---|---|
| MockLoan (OpenAdapt's fault server) | SQLite | create a disbursement (JSON POST) | `db` | yes (zero-config benchmark) |
| Workbench, MockMed profile (OpenAdapt's task-generation app) | PostgreSQL | update a patient's phone and email (HTML form) | `db` | yes |
| Mealie v2.8.0 | PostgreSQL | update a recipe's description and yield (JSON PATCH) | OpenAPI (`/openapi.json`) | no agent had installed or probed it before this benchmark; tuned on by this benchmark after its first valid run |
| Directus 11.5.1 | PostgreSQL | update a ticket's status and priority (JSON PATCH) | OpenAPI (`/server/specs/oas`) | no: never installed or probed before its reported run |

Mealie and Directus run on the lab's WSL host (PostgreSQL 16.4, pinned images),
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
  earlier setup log, the facts in `sandbox.yaml` are listed as operator inputs,
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

## Addendum, 2026-11-21, before Directus

Written after the Mealie runs and before any draft was run on Directus. The
only Directus contact before this addendum was the operator setup dry run
(start the stack, create the collection, the second user and three
tickets).

- **Why a second untuned app.** On Mealie no draft passed the lost-update
  case, and the store seems to keep nothing that could show one: no version,
  no author and no save history. So Mealie can't separate "the approach
  fails" from "this store can't show a lost update". Directus was picked by
  one constraint: its store records each save with its author
  (`directus_revisions` and `directus_activity`), and two users can sign in.
  The blind trial reserves OpenProject, Taiga and BookStack, so they were
  avoided.
- **Changes since the first valid Mealie run.** These were made in response
  to Mealie, so Mealie no longer counts as untuned:
  - the attribution probe repeats the first write, to tell a new id or a
    clock from a value;
  - a lower-cased copy of a value counts as written only for an email
    address;
  - only the tables that moved are re-read;
  - a round stops at a clean write that isn't confirmed;
  - a case the sandbox can't build (no second user, no other record) is
    untestable: never a pass, and an operator fix;
  - the draft filter is an allowlist (SQL reads only, no `${...}`);
  - prompt `verify-draft-prompt/2` asks for the contract as JSON text,
    because YAML with unquoted colons cost a round in two Mealie runs;
  - the workbench gets a second session as its second actor;
  - a parameter whose demonstrated value another parameter shares is not
    varied (it can't be swapped in the trace alone);
  - a sign-in token can be read from a nested reply field
    (`data.access_token`).
- **Frozen.** The code and prompt for the Directus run are the commit that
  adds this addendum. The tuned apps are rerun on the same commit. The
  first valid Directus run is the reported number. Only an operator setup
  error voids a run.
