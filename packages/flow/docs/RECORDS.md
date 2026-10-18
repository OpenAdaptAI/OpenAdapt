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
OCR of the field's region. The value comes back `unconfirmed` with no Seal.

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

For `committed_record_readback`, close the record, reopen it by its identity
fields, read the field again with `session="reopened"`, and pass the new read
to `add_read()`. For `verified`, pass a database or API read, or an export of committed data
(`channel_read(spec, "export", value, source_kind="committed_store")`). A live-session export gives at most `screen_readback`. A later read that disagrees sends
the field to review.

## Requiring a grade before a write

A step that writes a read value into a system of record can declare the
minimum grade it accepts. Below it, the run halts instead of writing:

```python
from openadapt_flow.records.schema import GradeGate

GradeGate(min_grade="reopened", fields=("amount_due",)).check(record)
# raises GradeTooLow if amount_due is weaker, missing or still needs review
```

These grades describe how a read value was checked. Write checks keep their
own grades in `openadapt_flow.assurance`.
