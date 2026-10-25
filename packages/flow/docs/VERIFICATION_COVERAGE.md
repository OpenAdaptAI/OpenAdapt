# Record checks that cover their own blind spots

Design. Status: proposed, with an implementation in small pull requests. Any
change to when a run is `verified` waits for a safety review.

## The problem

A record check reads the system of record after a save and compares what it
finds with a contract. The contract comes from the same place as the save:
the demonstration, the task's parameters, or a synthesized check built from
the reads the app made. So the check is blind wherever the contract is blind.
Three blind spots have been measured or are easy to construct:

1. **Changes the contract never mentions.** In the OpenEMR fault run of
   OpenAdaptAI/OpenAdapt#1365, `verify-run` caught 25 of 30 bad saves and
   reported `verified` on all 5 saves where another chart was edited in place.
   The contract described the new chart, and nothing in it said "and no other
   chart changes". OpenAdaptAI/OpenAdapt#1375 added
   `forbid_collateral_change`, measured only on rebuilt database states. It
   judges one effect at a time, so two effects on one read set flag each
   other's targets, and it can't tell a legitimate edit by another user from
   a stray write.
2. **Checks that were never shown to fail.** A synthesized check is built
   from observed values and available reads. If the read is scoped to one
   patient, an edit to another patient can't be seen, however the judge is
   written. `scaffold-verifier` tests the check by mutating the records the
   check already reads, so it can't find this either.
3. **The wrong input, saved correctly.** A check confirms that the database
   holds the parameters it was given. If the referral said `02139` and the
   extraction read `02193`, the save of `02193` passes every check. The error
   happened before the save, and the read-back can't see it.

## One claim, three parts

`verified` means exactly this, and nothing more:

> The declared delta happened, nothing else in the declared slice changed,
> and the inputs match their source.

Each part is checked by its own mechanism, and the receipt says which parts
were checked and how. The parts are:

| Part | Name in the receipt | What it rules out | Mechanism |
|---|---|---|---|
| Delta | `delta` | missing, partial, wrong-value, duplicate and delayed writes | the existing effect judge (`record_written`, `field_equals`, `exact_new_set`) |
| Frame | `frame` | edits, deletions and additions outside the declared records and fields | frame conditions (section 1) |
| Source | `source` | a misread or mistyped input saved correctly | provenance, second extraction, round trip (section 3) |

A check is admitted only after it is shown to catch the faults it claims to
catch (section 2). Its coverage travels with it into the receipt.

The name "frame condition" comes from program verification: a procedure's
specification says what it may modify, and everything else is unchanged.
The same idea applies to a save.

## 1. Frame conditions

### The contract declares what may change

The save's contract already names its target records: every `record_written`
and `field_equals` selector, and every `exact_new_set` member. A new `frame:`
section names the slice that must otherwise stay unchanged, and the fields on
the target records that the save may change.

```yaml
frame:
  # Records are compared by this field. Default: id (stable_id).
  identity_field: pid
  # Fields that change on their own on every record, such as a last-seen time.
  ignore_fields: [date]
  # Fields of a target record that the save may change. Default: the fields
  # the effects name. Any other field of a pre-existing target must not move.
  target_fields: [phone_home, street]
  # Who changed what, from the app's own audit log or change feed.
  attribution:
    kind: audit_read
    read: {kind: sql, ...}          # the same verifier kinds, read-only
    record_field: patient_id        # the audit entry's record reference
    actor_field: user
    self: [{param: bot_user}]       # the bot's own user or session ids
    scope_field: comments           # what the entry wrote (a table)
    scope_encoding: base64
    scope_pattern: ...              # a regex with a (?P<scope>...) group
    writes: [patient_data, history_data]  # the tables the save writes
```

The slice is the verifier's read set: the records it returns before and after
the save, through the same read-only channel the delta check uses. No new
connection is needed when there is no attribution read.

### The judgment

The frame judge compares the baseline with the read after the save, once for
the whole contract, not once per effect:

- A record is **declared** when any effect's selector matches it before or
  after the save. Declared records are judged by the delta check, except that
  a pre-existing declared record may change only in its target fields.
- Every other record that was **added, removed or changed** is a frame
  change. Each change is then attributed:
  - **another actor**: the audit log names a different user for that record
    in the window, or the record's own modified-by field names one and its
    version moved. This is a concurrent edit. It is listed in the evidence and
    does not count against the save.
  - **this run**: the audit log or modified-by field names one of the bot's
    own ids. This is collateral.
  - **unattributed**: nothing names an actor. This is collateral too. A change
    nobody can explain is never assumed to be someone else's.
- An audit entry by the bot for a record that no effect declares is
  collateral, even if the record looks unchanged in the read. The log shows a
  write the contract does not cover.
- An audit entry by the bot for a declared record is excused only for a table
  in `writes`. An entry for the saved patient in any other table, or one whose
  table can't be read, is collateral (OpenAdaptAI/OpenAdapt#1505).

Collateral refutes the frame. The outcome is `reconciliation_required`, the
same as a wrong value, and the reason names the records and fields.

Without a baseline, or when the baseline is outside the execution window, the
frame is `not_checked`. Without attribution, every frame change is
unattributed, which is the behavior of OpenAdaptAI/OpenAdapt#1375 extended to
additions and to multi-effect contracts.

### Where attribution comes from

| App offers | Attribution | Example |
|---|---|---|
| An audit log | `audit_read`: a second read-only read of log entries in the window | OpenEMR's `log` table records `patient-record-update` with the user and `patient_id` |
| A change feed or history | `audit_read` over the feed | FHIR `_history`, `AuditEvent` |
| A modified-by field and a version or ETag on each record | `record_field` | `meta.versionId` plus a last-modified-by extension |
| Nothing | none: every frame change is unattributed | MockMed |

### What this replaces

`Effect.forbid_collateral_loss` and `forbid_collateral_change` stay as they
are for contracts without a `frame:` section, so every existing contract hash
holds. When `frame:` is present, the frame judge replaces the per-effect
collateral rules for that contract. `verify-run` applies a default frame (the
read set, compared by `stable_id`, no attribution) to any contract with a
baseline. That keeps #1375's behavior, adds unexplained additions, and stops
two effects on one read set from refuting each other.

## 2. Mutation-qualified checks

### The gate

Before a record check can be admitted, an automatic fault suite runs it
against a test copy of the system of record. The faults are applied to the
**test copy's store**, and the check reads them through **its own read**, so
a read that can't see a fault fails the gate. Mutating only the records the
check already reads would test the judge, not the read.

Each case resets the copy, takes the check's baseline, applies the clean save,
applies one fault, and runs the check. The clean save must pass. Each fault
must be refuted or ruled indeterminate.

| Fault class | What the copy holds after the save | Applies when the check claims |
|---|---|---|
| `wrong_value` (one per declared field) | the target with that field changed | delta |
| `wrong_record` | the values written to a look-alike record instead | delta |
| `unrelated_edit` | the clean save, plus another record in the slice edited | frame |
| `partial` | the target with its later fields blank | delta |
| `duplicate` | the target twice | delta |
| `missing` | no target | delta |
| `delayed` | no target until after the check's timeout, then the target | delta |

`delayed` passes when the check does not confirm before the write lands. A
check that confirms from a cache or a stale read fails it.

### Coverage

Coverage is faults killed over faults applicable. A fault is applicable when
the check claims the part it targets: a check with no frame claims no
`unrelated_edit`. The gate writes `check_coverage.json`:

```json
{
  "schema_version": "openadapt.check-coverage/v1",
  "check_sha256": "sha256:...",
  "suite": {"id": "openadapt.fault-suite/public", "version": "1.0.0"},
  "test_copy": "sqlite",
  "claims": ["delta", "frame"],
  "classes": {"wrong_value": {"applicable": 9, "killed": 9}, "...": {}},
  "killed": 15,
  "applicable": 15,
  "clean_pass": true
}
```

`check_sha256` is the check's contract digest (`verify_config_sha256`). A
changed check needs a new coverage record. The record goes in the bundle next
to the check, and its digest and totals go into the receipt.

### Admission policy

A check is admitted only when the clean save passes and it kills every
applicable fault. A policy can also require a minimum: a coverage floor over
the whole suite, or named classes that must be applicable (for example,
"every check must claim a frame"). `verify-run` reads the policy from the
`admission:` section and never issues `verified` for a check whose coverage
record is missing, stale or below the policy.

`scaffold-verifier`'s fault test becomes a call to this gate. With `--db` or a
fixture it runs on a store copy. Without one it falls back to recorded
states and says so in the coverage record (`test_copy: recorded_states`),
and a policy can refuse that kind.

### What is public

The gate, the seven operators, the coverage format and a small sample suite
are public. A grown fault corpus and deployment coverage floors are private
under `source-policy.yaml` (`grown_corpus`, `deployment_thresholds`). The
public default policy is the rule above, "catch every fault you claim", not a
tuned number.

## 3. Intent and provenance

### Every written value names its source

A run parameter can carry its provenance:

```json
{
  "postal_code": {
    "value": "02139",
    "source": {"kind": "document", "sha256": "sha256:...", "line": 14, "label": "Postal code"},
    "extractor": "intake:agent"
  }
}
```

The source is one of:

- `document`: a line (and the label before the value) in a referral, an
  invoice or another file, identified by the digest of the file. Intake
  (`openadapt_flow/intake`) already finds this line for every field and then
  drops it; it now keeps it;
- `worklist_cell`: a row and a column in a worklist file, identified by its
  digest;
- `instruction`: the authorized task itself. The value is the instruction, so
  it matches its source by construction, and the receipt says so.

A parameter without provenance is treated as `instruction`. That keeps every
existing run honest about what was checked: its source part reads
`instruction`, not `document`.

### A second extraction must agree before the save

Before the save, an independent extractor reads the same source with a
different method: a deterministic parse of labeled fields, a second model,
or OCR on an image. Intake has two methods today: `intake:local` matches
labels, and `intake:agent` asks a model for the line and reads the value
locally. Each serves as the other's second extraction. Values compare by kind (text, code, date, currency), the
same comparison the screen read-back uses. A disagreement, or a value the
second extractor can't find, routes the item to a person, and the save does
not start. The extractor's method must differ from the first one's: two runs
of the same model don't count.

### A round trip after the save

After the save, the round trip compares the record as read back with the
source document, not with the parameters. For each declared field it takes
the value the read returned and the value the independent extractor read from
the cited span. A mismatch refutes the source part. This catches a correctly
persisted wrong entry even when the pre-save check was skipped or the
parameters changed between the check and the save.

### What it doesn't catch

A source document that is itself wrong is out of scope. So is a span both
extractors misread the same way. The receipt names the extractors, so a
reviewer can judge how independent they were.

## 4. What the receipt says

The receipt gets one optional field, `checked`. Like `checked_how`, it is
inside `receipt_sha256`, so the Seal signs it. A receipt issued before it
existed still checks.

```json
"checked": {
  "delta": "held",
  "frame": "held",
  "source": "instruction",
  "coverage": {"killed": 15, "applicable": 15, "sha256": "sha256:..."}
}
```

Each part is `held`, `refuted` or `not_checked`. The source part can also be
`instruction`, which means the inputs were the authorized instruction itself
and no document was involved. The evidence lists the attributed concurrent
edits, the extractors and the per-class coverage.

`verified` requires `delta` and `frame` to be `held`, `source` to be `held` or
`instruction`, and coverage to meet the admission policy when one is set.
Anything else is `reconciliation_required` (or `halted_before_effect` when
nothing landed). The report repeats the `checked` field in words, one line per
part, with no other wording about confidence.

### Where the rule applies

Two code paths issue `verified`:

- the effect receipt from `verify-run` and the guarded save
  (`verify_run.verify_run`), which judges a save something else made;
- Flow replay (`execution_profiles.classify_execution_outcome`), whose
  `report.json` the offline Seal signs.

The first implementation applies the rule to the effect receipt, where the
OpenEMR experiment was measured. Flow replay gets the same `checked` block and
the same rule in a separate pull request, through the same frame judge and
provenance functions. Until it lands, a Flow replay report does not carry
`checked`, and nothing on a public surface may say that its `verified` covers
the frame or the source.

### What changes for existing runs

These are the changes the safety review must approve:

- `verify-run` without a baseline can no longer issue `verified`, because the
  frame can't be checked. Today it can, for a contract that counts records
  absolutely.
- A record added to the read set that no effect declares now refutes the
  frame. Today only `exact_new_set` catches it.
- A contract with two effects on one read set no longer refutes itself when
  `forbid_collateral_change` is on.
- An admission policy can block `verified` on coverage.

Nothing in this design turns a refusal into a confirmation, except
attribution, which lets a concurrent edit by another user stop counting as
collateral. That exception is confined to edits an audit log or a
modified-by field attributes to a named actor other than the bot.

## Measurement

The benchmark lives in `benchmark/verification_coverage/`. It reruns the
OpenEMR fault experiment of OpenAdaptAI/OpenAdapt#1365 as fresh agent runs,
not rebuilt states, and adds three classes: a concurrent edit by another user
(must stay `verified`), a collateral edit by the bot's own user (must be
caught), and a misread source (must be caught before the save, and by the
round trip if the save happens anyway). The same suite runs on openIMIS.
Results are reported per fault class, before and after, with each run's
engine commit.

## Implementation plan

1. This document.
2. Frame conditions: `runtime/effects/frame.py`, the `frame:` section,
   attribution reads, and the `checked` field in the receipt.
3. The fault gate: `openadapt_flow/fault_gate.py`, store test copies (SQLite,
   an in-memory REST store, and fixture adapters in the benchmark), coverage
   records, the admission policy, and `scaffold-verifier` on the gate.
4. Provenance: parameter sources, the deterministic label extractor, the
   pre-save cross-check and hold, and the round trip in `verify-run`.
5. The benchmark runs and results.
6. Flow replay: the frame judge and provenance in the replayer, and
   `checked` in `report.json`.
