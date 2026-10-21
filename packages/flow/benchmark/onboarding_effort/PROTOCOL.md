# Onboarding effort: protocol

This file is frozen with the workflow pool (see `pool/MANIFEST.json`). A change
after the freeze commit needs a new protocol version and a note in the report.

## Question

How many demonstrations, corrections, product questions and engineer-hours
does OpenAdapt need to take a workflow nobody has tuned for to its endpoint,
and how much more to bring the same workflow up on a second installation?

## Pool

`pool/<workflow>/spec.yaml` holds each task: the application, the task as a
person would describe it, its inputs, the item generator seed, the
independent oracle, and the second installation. Rules:

- No workflow in the pool was ever recorded, compiled or qualified before the
  freeze commit. Workflows the repository already tunes (OpenEMR patient
  registration and referral, openIMIS claim intake, MockMed triage) are
  excluded; they serve only as harness calibration.
- Each spec, its item generator, its scripted demonstration and its oracle are
  hashed into `pool/MANIFEST.json` and committed before any teaching. The
  ledger records the manifest digest on every row.
- Before the freeze, each task is validated without OpenAdapt: a plain
  Playwright script performs one item and the oracle must say `VERIFIED`; on a
  fresh baseline with nothing done the oracle must say `MISSING`. This checks
  the task and the oracle, not the product.
- Applications run as separate local services on synthetic data. AGPL and GPL
  applications (openIMIS, OpenEMR) are never copied into the package.

## Oracle

The oracle is independent of the product. It reads the application's database
directly (SQL through the container's own client), never the screen and never
the product's own verifier, and judges each item:

- `VERIFIED`: exactly the expected record change for that item, every
  expected field equal, on the intended entity (identity), and no change to
  any other entity the item did not name (collateral);
- `MISSING`: no change for the item;
- `WRONG`: a change on the wrong entity, a wrong field value, a duplicate, or
  collateral damage.

## Implementer

`implementer.py` plays the onboarding engineer. It uses only the product's
teach surfaces, in this order, and logs every human-equivalent action:

1. **Demonstrate** one item: a scripted human-like recording through the
   product recorder (`openadapt_flow.recorder.Recorder`, real pointer and
   keyboard input on a real browser), or the code-as-action agent's run-1
   path (`compile-agent --vision code`). The two are counted separately.
2. **Compile** (`openadapt-flow compile`) and answer its parameter questions
   from the task's declared inputs (`--params-from`). Each answered proposal
   is one question.
3. **Lint** (`openadapt-flow lint`).
4. **Write verify.yaml** from what the engineer knows about the app (where the
   record lands, its key, which input fills which field). Counted in lines.
5. **Qualify** (`openadapt-flow qualify --certify <policy>`): clinical-write
   for clinical and claims writes, permissive otherwise.
6. On a failure, pick the next remedy from a fixed menu by the failure's
   class: another demonstration plus `induce` (loops, branches), `for-each`
   over the item list (data-driven loops), `disambiguate` answers, `teach`
   with a correction recording, a re-demonstration, or a verify.yaml edit.
   Each is one correction (plus the demonstrations it needs).
7. **Held-out batch**: 25 items the teaching never saw, each run as a
   certified run on one baseline, each judged by the oracle.

Budget per workflow and installation: 5 demonstrations, 4 corrections,
3 qualify attempts, 120 machine minutes of teaching (the held-out batch is
not counted against it), and $3 of model spend. A workflow that exhausts its
budget is a failure and the ledger names the step where it stuck.

## Endpoint

The workflow is certified by `qualify` under its policy, and one batch of 25
held-out items, run after the last correction, ends 25/25 `VERIFIED` by the
oracle. The engine's own outcome is recorded beside the oracle's; an item the
engine calls VERIFIED and the oracle does not is a silent wrong success and is
reported on its own line.

## Second installation

Each spec names one change to the installation: another version, a theme or
locale, a required or custom field, another role with other permissions, or
another screen size or device scale. On it the implementer first re-runs the
existing bundle (qualify plus the 25-item batch, new items):

- **zero-touch**: it reaches the endpoint with no action beyond pointing it at
  the new installation;
- **repair only**: it reaches the endpoint after product repair surfaces
  (`teach` from a halted run, `repair`, a verify.yaml edit) and no new
  demonstration of the whole task;
- **re-teach**: it needs a new demonstration of the whole task;
- **failed**: none of these within the budget.

## Accounting

Every action is one ledger row (`ledger.jsonl`): workflow, installation,
phase, action, counts, the product command and its exit status, machine
seconds, tokens, model dollars, and engineer minutes from the rate card.

Engineer minutes are human-equivalent estimates from this fixed rate card,
not measured human time:

| action | minutes |
|---|---|
| scripted demonstration | 2 + 0.1 per recorded action |
| code-as-action demonstration (write the task prompt) | 3 |
| answer one product question | 1 |
| write or change one line of verify.yaml or hooks | 1 |
| read and triage one failed product command | 5 |
| type one product command | 0.5 |

Machine time (the product and the browser working) is reported separately,
measured on this computer under its recorded load. Model tokens and dollars
are measured from the API responses.

## Behavioral generalization score

`behavioral_score.py` executes held-out items and checks identity and
effects. It replaces `structural_trace_coverage` for any generalization claim;
that score stays in the product, labelled structural only.
