# OMOP CDM template

`measurement.py` turns exported record rows into OMOP CDM v5.4 `measurement`
rows. It is a template. OpenAdapt's core exports to a schema you define; OMOP
is one such schema.

```python
from measurement import FieldConcept, to_measurements

rows = export_rows(records, schema).rows
measurements = to_measurements(
    rows,
    person_id={"R-001": 1},  # from your identity mapping
    date_field="read_date",
    concepts={"score": FieldConcept(measurement_concept_id=0, source_value="score")},
    type_concept_id=32817,  # your choice of type concept
)
```

The template ships no concept mappings. Use your own vocabulary. Concept id
`0` means "no matching concept" in OMOP.

Each row keeps `openadapt_grade` and `openadapt_receipt_sha256`, so the read
grade and Seal stay with the value.
