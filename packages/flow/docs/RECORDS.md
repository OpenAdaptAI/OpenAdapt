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

By default a field is read through one channel, gets the grade `unconfirmed`
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

Weakest first:

The grade only ever reflects what was proven.

| Grade | What it needs |
|---|---|
| `unconfirmed` | Nothing cross-checked. The default for a single-channel read. Reads that disagree also get it, and go to review. |
| `screen_readback` | Two screen channels agree in the same session, such as clipboard and OCR. |
| `committed_record_readback` | The record was closed, reopened by its identity fields and read again, and the reads agree. |
| `verified` | A channel outside the screens agrees: an export file, a read-only database or an API. |

`grade_for()` computes the strongest grade a set of reads supports, and a
`FieldRead` that claims more is rejected. Only `export`, `database` and `api`
reads can count as independent, so screen reads alone can never be
`verified`.

## Requiring a grade before a write

A step that writes a read value into a system of record can declare the
minimum grade it accepts. Below it, the run halts instead of writing:

```python
from openadapt_flow.records.schema import GradeGate

GradeGate(min_grade="committed_record_readback", fields=("amount_due",)).check(record)
# raises GradeTooLow if amount_due is weaker, missing or still needs review
```

These grades describe how a read value was checked. Write checks keep their
own grades in `openadapt_flow.assurance`.
