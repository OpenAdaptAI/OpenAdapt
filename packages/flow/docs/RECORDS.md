# Reading records

Flow can read records out of an application's screens, field by field, with
optional proof of where each value came from. This page describes the record
schema. The reading step, the review queue and
export are described in their own sections as they ship.

Import path: `openadapt_flow.records.schema`.

## Record schema

A `RecordSchema` lists the fields read from one kind of record and names the
identity fields used to reopen it.

```python
from openadapt_flow.records.schema import FieldSpec, RecordSchema

invoice = RecordSchema(
    name="vendor_invoice",
    fields=(
        FieldSpec(name="invoice_number", kind="code"),
        FieldSpec(name="vendor"),
        FieldSpec(name="amount_due", kind="currency"),
        FieldSpec(name="due_date", kind="date", date_order="mdy"),
        FieldSpec(name="status", kind="enum", choices=("open", "paid")),
    ),
    identity_fields=("invoice_number",),
)
invoice.to_json_schema()  # JSON Schema for one exported row
```

Field kinds: `text`, `code`, `date`, `currency`, `integer`, `decimal`,
`boolean` and `enum`. A field can set `min_confidence`; a value below it goes
to the review queue. It is unset by default.

## Proof is opt-in

By default a field is read through one channel, gets the grade `unchecked`
and has no Seal. Turn proof on for the whole schema, or per field, with
`ReadProof`:

```python
from openadapt_flow.records.schema import ReadProof

invoice = invoice.model_copy(
    update={"proof": ReadProof(double_read=True, committed_readback=True, seal=True)}
)
FieldSpec(name="amount_due", kind="currency", proof=ReadProof(double_read=True))
```

| Setting | What it adds |
|---|---|
| `double_read` | Reads through two channels and compares them. |
| `committed_readback` | Closes the record, reopens it by its identity fields and reads again. |
| `seal` | Covers the value with a signed Seal. |

A field's own `proof` replaces the schema's. `schema.proof_for(name)` gives
the settings in effect.

## Field reads

A `FieldRead` is one value with its evidence:

- `reads`: what each channel returned (`clipboard`, `ocr`, `structure`,
  `accessibility`, `export`, `database` or `api`);
- `agreement`: `agree`, `disagree`, `ambiguous` (differs only in look-alike
  characters such as O and 0) or `single`;
- `confidence` from 0 to 1;
- `region`: screenshot hash, bounding box and crop hash;
- `seal`: a `SealRef` pointing at the signed run receipt;
- `grade`: see below.

A `RecordRead` groups the field reads of one record. Its grade is the weakest
field grade.

## Read grades

Weakest first. The names are check levels 0, 1, 2 and 4 from the
[check-level table](APPROVAL_AND_VERIFICATION.md#how-a-save-is-checked), so a
read grade means the same as the check level of a save. The grade only ever
reflects what was proven.

| Grade | What it needs |
|---|---|
| `unchecked` | Nothing cross-checked. The default for a single-channel read. Reads that disagree also get it, and go to review. |
| `same_screen` | Two screen channels agree in the same session, such as clipboard and OCR. |
| `reopened` | The record was closed, reopened by its identity fields and read again, and the reads agree. |
| `system_of_record` | A channel independent of the acting session agrees: a read-only database, an API, or an export of committed data. |

`grade_for()` computes the strongest grade a set of reads supports, and a
`FieldRead` that claims more is rejected. Only `database` and `api` reads and
exports of committed data count as independent, so screen reads alone can
never be `system_of_record`.

### Exports from the application

An `export` read must say where it came from, in `source_kind`. The grade
is derived from it:

| `source_kind` | Taken from | Counts as | Best grade |
|---|---|---|---|
| `live_session` | The same live UI session that typed or showed the value. It can include that session's unsaved state. | A screen read | `same_screen` |
| `committed_store` | Saved data, through a separate process, a database read or a scheduled export. | An independent read | `system_of_record` |

```python
ChannelRead(channel="export", raw="AC-1042", source_kind="live_session")
ChannelRead(channel="export", raw="AC-1042", session="independent",
            source_kind="committed_store")
```

A workflow can use either kind. A write step that needs `system_of_record` values
(see below) halts on values backed only by a live-session export.

## The reading step

`openadapt_flow.records.read.read_field()` reads one field on a live backend.
By default it uses one channel: the clipboard when you pass one, otherwise
OCR of the field's region. The value comes back `unchecked` with no Seal.

```python
from openadapt_flow.records.read import SystemClipboard, read_field

field = read_field(spec, backend, (412, 188, 160, 22), clipboard=SystemClipboard())
```

For the clipboard read, Flow sets the clipboard to a marker value, clicks the
field, presses the copy keys (default `Meta+a`, `Meta+c`) and reads it back.
A clipboard still holding the marker means the copy did nothing, so it counts
as no read rather than as the previous field's value. Every read records the
screenshot hash, the region and the crop hash in `region`.

With `proof=ReadProof(double_read=True)`, both the clipboard and OCR are read
and compared. The reported value is the clipboard's, because it is the
application's own text. Comparison reuses the read-back rules in
`openadapt_flow.runtime.effects.readback_values`: reads that differ only in
look-alike characters are `ambiguous` and go to review, never treated as a
match. With `seal=True`, pass a `SealRef`; it is attached to the value.

Confidence is a fixed rule:

| Reads | Confidence |
|---|---|
| Two or more agree | 0.95 + 0.05 × OCR engine confidence |
| Clipboard only | 0.7 |
| OCR only | 0.6 × OCR engine confidence |
| Look-alike characters only | 0.4 |
| Disagree | 0.1 |
| Nothing parsed | 0 |

A field goes to review when its reads disagree or are ambiguous, when a value
is not valid for its kind, when `double_read` is on and only one channel read
a value, or when it sets `min_confidence` and falls below it.

For `reopened`, close the record, reopen it by its identity
fields, read the field again with `session="reopened"`, and pass the new read
to `add_read()`. For `system_of_record`, pass a database or API read, or an export of committed data
(`channel_read(spec, "export", value, source_kind="committed_store")`). A live-session export gives at most `same_screen`. A later read that disagrees sends
the field to review.

## Read steps in a workflow

A workflow can read as well as act. A step with `action: read` carries a
`read` spec: the schema name, the `FieldSpec`, the value box, the proof
settings, the copy keys, and what to do when the read needs review
(`on_review`: `halt`, the default, or `continue`).

```python
from openadapt_flow.ir import ActionKind, ReadStepSpec, Step
from openadapt_flow.records.schema import FieldSpec, ReadProof

Step(
    id="read-amount",
    intent="read amount due",
    action=ActionKind.READ,
    read=ReadStepSpec(
        schema_name="invoice",
        field=FieldSpec(name="amount_due", kind="decimal"),
        region=(412, 188, 160, 22),
        proof=ReadProof(double_read=True),
    ),
)
```

On replay, the step reads the field with `read_field()` and puts the result in
`StepResult.field_read`. With an anchor, the value box moves with the anchor,
as clicks do. The channels are OCR and the clipboard, both deterministic, so a
healthy read step makes no model calls. Pass the clipboard to the replayer as
`Replayer(..., read_clipboard=SystemClipboard())`. Without one, a step that
needs a double read halts with "no clipboard channel"; a step that does not
reads by OCR alone. A read step is never irreversible, and it declares no
postconditions because it changes nothing.

To record a read, mark the value box during a recording. A marked label box
becomes the step's anchor:

```python
recorder.mark_read(
    FieldSpec(name="account_number", kind="code"),
    (120, 80, 160, 22),
    schema_name="ledger_account",
    label_region=(20, 80, 90, 22),
    label_text="Account",
)
```

`compile_recording()` turns each marked read into a READ step, and
`render_workflow_py()` renders it as `record["account_number"] =
flow.read(...)`.

### Seals from run receipts

A read step with `proof=ReadProof(seal=True)` gets a `SealRef` naming the run.
When the run's receipt is written (`report-run`), Flow writes `records.json`
next to `receipt.json`: every field read in the run, with each requested Seal
pointing at the receipt by sha256 and path. The same is available as
`openadapt_flow.records.run.seal_record()`.

The run receipt covers the run, not each value, so `field_commitment` stays
unset. When offline Seals with per-field commitments are available, they fill
it. A Seal pointer never claims more than the receipt it points at.

### Format compatibility

The read step adds three optional fields: `Step.read`, `Step.read_gate` and
`StepResult.field_read`. The bundle schema stays at version 2, which is how
this repo adds optional fields. A workflow without read steps loads unchanged
and keeps its sealed digest: absent `read` and `read_gate` are omitted from
the digest, by reviewed rules in `bundle_validation`. A runtime from before
this change refuses a bundle that contains a read step, because it does not
know the `read` action. It never runs one with the read silently dropped.
A read step is not exported to the shared `openadapt-types` action schema,
which has no typed field read.

## Requiring a grade before a write

A step that writes a read value into a system of record can declare the
minimum grade it accepts. Below it, the run halts instead of writing:

```python
from openadapt_flow.records.schema import GradeGate

GradeGate(min_grade="reopened", fields=("amount_due",)).check(record)
# raises GradeTooLow if amount_due is weaker, missing or still needs review
```

In a workflow, set `read_gate` on the writing step. Replay checks it against
the values read so far in the run and halts before the step acts:

```python
Step(id="post", intent="post amount", action=ActionKind.CLICK, anchor=save,
     read_gate=GradeGate(min_grade="screen_readback", fields=("amount_due",)))
```

These grades describe how a read value was checked. Write checks keep their
own grades in `openadapt_flow.assurance`.

## Review queue

`openadapt_flow.records.review.ReviewQueue` holds only problems: field reads
that need review and halts while reading a record. Agreeing reads never enter
it. It is a directory (`items/`, `decisions/`), so it works offline.

```python
from openadapt_flow.execute.keys import load_or_create_private_key
from openadapt_flow.records.review import ReviewQueue, apply_decision

queue = ReviewQueue("./review")
queue.add_record(record, created_by="runner:host-1")
for item in queue.open_items():
    print(item.field, item.reason, item.candidates)

decision = queue.decide(
    item.item_id,
    action="accept_read",  # or correct, reject, reread
    channel="clipboard",
    reviewer="person:clerk-1",
    signing_key=load_or_create_private_key(data_dir),
    schema=schema,
)
settled = apply_decision(field_read, decision)
```

The queue enforces these rules:

- Only a `person:` principal can decide. Model and agent principals are
  refused.
- The principal that created an item cannot decide it.
- A decision is written once and signed with the local Ed25519 key over the
  decision and the hash of the item. `queue.check()` verifies both.
- A corrected value must be valid for the field's kind.
- A decision settles the value but keeps its grade. A person's choice is not a
  read, so it never raises the grade.

## Export

`openadapt_flow.records.export.export()` writes settled records to a sink.

```python
import sqlite3
from openadapt_flow.records.export import CsvSink, DbApiSink, WebhookSink, export

result = export(records, schema, CsvSink("accounts.csv"))
export(records, schema, DbApiSink(sqlite3.connect("accounts.db"), "accounts"))
export(records, schema, WebhookSink("http://127.0.0.1:8080/records"))
for skipped in result.skipped:
    print(skipped.record_key, skipped.reason)
```

Each row has one column per field, typed by kind (`integer` as a number,
`boolean` as true or false, amounts as exact decimal strings), and these
provenance columns:

| Column | Meaning |
|---|---|
| `_record_key` | Identity field values. The database sink upserts on it. |
| `_run_id`, `_read_at` | The run that read the record, and when. |
| `_grade` | The weakest field grade. |
| `_receipt_sha256` | The Seal that covers the record, once sealed. |
| `_provenance` | JSON: per field, the grade, channels, confidence, screenshot hash, region, crop hash and review decision. |

A record is exported only when no field still needs review and each field is
at least `min_grade` (default `unchecked`, so every settled record) or was settled by a signed
review decision. Pass `min_grade="system_of_record"` to export only values confirmed
by an independent channel. Skipped records are returned with the reason.

`DbApiSink` takes any DB-API 2.0 connection. For Postgres, pass a `psycopg`
connection and `paramstyle="format"`. `WebhookSink` refuses a URL that is not
on this machine unless you pass `allow_remote=True`.

A `ColumnMapping` renames fields and adds fixed columns for a layout you
define. Standard target models are templates outside the core; see
`examples/records/`.
