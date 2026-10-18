# Records interchange format (v0)

**Status:** draft, version `openadapt.records-interchange/v0`. Fields can still change before v1.

This format carries one unit of work between two kinds of system:

- a **document side**, which turns documents into proposed values and records the decisions people make about them (a document parser, a review tool, a records platform);
- an **executor**, which reads and writes records in an application that has no API, and reports what it committed and how it checked it (OpenAdapt Flow).

The format has no domain vocabulary. An accounts-payable invoice, a loan file, an insurance claim and a clinical referral all use the same fields. Anything specific to one domain goes in `extensions`.

Files:

- JSON Schema: [`schemas/records-interchange-v0.json`](../schemas/records-interchange-v0.json), generated from `openadapt_flow/interchange/model.py`. A test fails if they drift apart.
- Conformance checks: `openadapt_flow/interchange/conformance.py`.
- Fixtures and expected results: [`tests/fixtures/interchange/`](../tests/fixtures/interchange/).

**Where the spec lives.** The spec is kept in the OpenAdapt monorepo for now. If the document-side party agrees to co-own it, it will move to a neutral shared repository under the same license, with the same version history and fixtures.

## One exchange

An exchange is one JSON document. Payload files sit next to it and are addressed by relative path.

| Member | Meaning |
|---|---|
| `schema_version` | Always `openadapt.records-interchange/v0` for this version. |
| `exchange_id` | Unique id chosen by the producer. |
| `direction` | `to_executor` (work to do) or `from_executor` (what was done). |
| `workflow` | Name of the workflow that handles it, such as `ap.post_invoice`. |
| `session_id` | Optional id that groups exchanges from one batch or run. |
| `in_reply_to` | On a `from_executor` exchange, the `exchange_id` it answers. Required there. |
| `artifacts` | Source documents and observations, in the `L1Artifact` envelope shape. |
| `fields` | Typed values, each with its reads, confidence and grade. |
| `identities` | The parties or records the exchange is about, and how each matched. |
| `decisions` | Finite choices made by authorized people. |
| `outcome` | `pending`, `committed`, `halted` or `no_action` (see [Outcomes](#outcomes)). |
| `halt_reason` | Required when `outcome` is `halted`. |
| `no_action` | Required when `outcome` is `no_action`, and not allowed otherwise: who decided, why, and the read-back that shows nothing was written. |
| `provenance` | Producer, tool, time and checksum (required). |
| `seals` | References to Seals (signed receipts) over the checksum. |
| `extensions` | Domain data under reverse-DNS keys. |

Unknown top-level members are rejected. A producer that needs to add a field adds it under `extensions`.

### Outcomes

| Outcome | Meaning | Who sends it | Rules | Counts as a save | Billable |
|---|---|---|---|---|---|
| `pending` | Work to do, or not finished. | Either side | None. | No | No |
| `committed` | The executor wrote the record and read it back. | Executor | At least one read of the target application (IX064). Every identity resolved (IX063). | Yes | Per the run's own billing policy |
| `halted` | The executor stopped. Whatever it wrote before stopping is described by the fields. | Executor | `halt_reason` (IX062). | No | No |
| `no_action` | A person decided the item, and nothing was meant to be written. | Executor | A `no_action` record with a person in `decided_by`, a `reason_code`, and read-back evidence that no record is new or changed, at the check level a commit needs (IX080 to IX084). | No, never | No, never |

A `to_executor` exchange cannot claim `committed` (IX066) or `no_action` (IX084).

#### `no_action`

| Member | Meaning |
|---|---|
| `decided_by` | The person who decided. Must be `human` (IX081). |
| `reason_code` | Why nothing was written, in lowercase snake case, such as `declined`, `held` or `rejected`. |
| `decision_id`, `decided_at` | Optional pointer to the decision, in this exchange or the one it answers. |
| `required_grade` | The check level a commit of the same workflow needs: the weakest grade its committed read-back carries, or its write step's minimum grade. It cannot be `unchecked`. |
| `evidence` | Names of fields whose reads show the state did not change, such as a record's entry count read before the decision and again after reopening. At least one is required, and each must be graded at least `required_grade` (IX082). |

The value of an evidence field is the state the executor first read, such as an entry count. A later read of it that differs means a record was created or changed while the item was handled, which is IX083.

A `no_action` exchange is never a save and never billable. A counter of saves or billable items counts `committed` only. It does not go to a review queue unless the site's policy says decisions need review.

### Artifacts

An artifact follows the `L1Artifact` envelope used by layered records platforms, so the same file can feed an existing acquisition seam:

| Member | Meaning |
|---|---|
| `artifact_id` | Id used by `reads[].artifact_id`. |
| `kind` | `source_document` (a file to parse) or `structured_observation` (values read from a source). |
| `source_type` | How it was acquired, such as `pdf_scan`, `fax_folder`, `rdp_screen` or `api_json`. |
| `file_number`, `date`, `doctype` | The L1 naming keys: the file is emitted as `{file_number}_{date}_{doctype}`. |
| `payload` | Optional `{path, sha256, mime, bytes}` for a file next to the exchange. `path` is a relative POSIX path: no leading `/`, no `..`, no backslash and no drive letter. After symlinks are followed it must still be inside the exchange's directory. |
| `metadata` | Free-form source context. |

### Fields

Each value carries its evidence:

| Member | Meaning |
|---|---|
| `name` | Lowercase snake case, unique in the exchange. |
| `type` | `text`, `code`, `date`, `currency`, `integer`, `decimal`, `boolean` or `enum`, the same kinds as the reading step's record schema. Decimals and currency amounts are strings (`"1249.50"`) so no precision is lost. Dates are ISO 8601. |
| `value`, `unit` | The value, and an optional unit such as a currency code. |
| `confidence` | 0 to 1, from the producer. |
| `status` | `proposed`, `confirmed`, `corrected` or `rejected`. `confirmed` and `corrected` need `confirmed_by`, a person, who is not the producer of the exchange. |
| `grade` | How well the value was checked in the target application (below). |
| `reads` | Each observation: `channel`, observed `value`, `artifact_id`, `session` (`acting`, `reopened` or `independent`), `source_kind` on export reads (below), `locator` (page, line, byte span, screen, and a `region` with `bbox`, frame sha256 and crop sha256), time and run id. |
| `needs_review`, `review_reason` | Set when the value goes to a review queue. A reason is required. |
| `seal` | Optional per-value Seal pointer: `run_id`, `receipt_sha256`, `field_commitment`. |

Channels: `clipboard`, `ocr`, `structure` and `accessibility` read the target application through its screens. `database` and `api` do not. `document_parse` and `human` are the document side.

An `export` read must say where the export came from in `source_kind`:

- `live_session`: the export was triggered from the same signed-in UI session that did the work. It can share that session's view, so it counts as a screen read.
- `committed_store`: the export was produced from saved data by a separate process, a database read or a scheduled job. It counts as an independent read.

The grade is derived from `source_kind`. A producer records where the export came from and does not assert a stronger grade.

Read grades, weakest first. They are names from the check-level ladder (`openadapt_flow/check_level.py`), so a read grade and the check level of a write mean the same thing. A grade is derived from the reads; a person's confirmation or decision never raises it.

| Grade | Meaning | Rule |
|---|---|---|
| `unchecked` | Not cross-checked: one read only, or the reads disagreed. | None. |
| `same_screen` | Two channels in the session that did the work agreed. | Two reads of the target application through different channels. |
| `reopened` | The record was closed, reopened by its identity fields and read again, and the reads agreed. | A read with `session` set to `acting` and a screen read with `session` set to `reopened`. |
| `system_of_record` | A channel that does not use the application's screens agreed. | A read through `database` or `api`, or an `export` with `source_kind` set to `committed_store`, plus one more read. |

Every grade above `unchecked` also needs:

- at least two reads of the target application from different (`channel`, `session`) pairs. Only reads with a value count; `document_parse` and `human` reads do not;
- every such read to equal the field's `value`. A read that saw something else is a violation (IX036), not a weaker grade, so a disagreement cannot hide behind a strong grade.

A producer may claim a weaker grade than its reads support, never a stronger one.

The grade, channel and kind names match `openadapt_flow.records.schema`, the record and field schema owned by the reading step. Until that module is on main they are spelled out in `interchange/model.py`; after it merges they are imported from it, and the wire names do not change.

### Identities

An identity names the fields that identify a party or record (`fields`) and says how it matched in the target application:

- `new`: no record exists; the executor creates one.
- `existing`: one record matched; `record_ref` is required.

When `record_ref` is set, it must equal the value of one of the identity's `fields`, so the reference and the values that identify the record cannot drift apart.
- `ambiguous`: more than one candidate. An exchange sent to an executor may not contain one; the document side must resolve it first. An executor that finds an ambiguous match halts and reports it.
- `not_checked`: not looked up yet. A `committed` exchange may not contain one.

### Decisions

A decision is a finite choice: `question`, `options`, `choice`, `authorized_roles` and `decided_by`. Rules:

- A decision with a `choice` needs `decided_by`.
- `decided_by.kind` must be `human`. A model's suggestion goes in `advisory` and never authorizes anything.
- `decided_by.role` must be in `authorized_roles`.
- `decided_by.id` must differ from `provenance.producer.id`: the system that produced the exchange cannot approve its own work.
- `output_field`, when set, names the field that carries the choice, and that field's value must equal the choice.

In Flow, a decision maps to a `BusinessDecisionSpec` and its signed receipt; `receipt_sha256` points at that receipt.

### Provenance, checksum and Seals

`provenance.checksum` is required. It is `sha256:` followed by the hex digest of the canonical JSON of the exchange with `provenance.checksum` and `seals` removed. Canonical JSON means sorted keys, no whitespace (`","` and `":"` separators) and UTF-8 without escaping. `openadapt_flow.interchange.compute_checksum` implements it. This is not RFC 8785 (JSON Canonicalization Scheme): numbers are written as Python's `json` module writes them, so a producer in another language should write decimals as strings (as the format already requires) and avoid floats whose text form differs between languages.

Each entry in `seals` points at a Seal and states the checksum it signs in `covers`. A Seal over a different checksum fails the check. The checksum is required even without a Seal: it is cheap, it catches accidental edits in transit, and a Seal can be added later without changing the document.

### Extensions

`extensions` maps a reverse-DNS key (at least two dot-separated labels, such as `org.example.referral`) to an object. The format does not interpret extension content. The reference referral template uses `org.openadapt.example.referral`.

## Conformance

A document conforms when it matches the JSON Schema and has no violation from the list below. Codes are stable within a major version.

| Code | Rule |
|---|---|
| `IX001` | Does not match the schema. |
| `IX010` | Checksum does not match the content. |
| `IX011` | A Seal covers a different checksum than the exchange has. |
| `IX020` | A read names an artifact that is not in the exchange. |
| `IX021` | An id or field name is used twice. |
| `IX022` | An identity names a field that is not in the exchange. |
| `IX023` | A decision choice is not one of its options. |
| `IX024` | A decision output field is not in the exchange. |
| `IX025` | A value does not match its declared type. |
| `IX026` | An identity's `record_ref` is not the value of any of its fields. |
| `IX030` | `system_of_record` grade without a read through an independent channel. |
| `IX031` | Read-back grade without two reads of the target application from different channels or sessions. |
| `IX032` | Confirmed or corrected value without a person who confirmed it. |
| `IX033` | `reopened` grade without a read in the acting session and a screen read of the reopened record. |
| `IX034` | A value that needs review has no reason. |
| `IX035` | An export read does not say where the export came from. |
| `IX036` | A read of the target application differs from the value it grades. |
| `IX040` | A decision was made by something other than a person. |
| `IX041` | The producer approved its own work. |
| `IX042` | The decider's role is not authorized for the decision. |
| `IX043` | A decision has a choice but no person who decided it. |
| `IX044` | A decision choice differs from the value of its output field. |
| `IX045` | The producer confirmed its own value. |
| `IX050` | Extension key is not namespaced (use reverse-DNS, e.g. org.example.x). |
| `IX060` | An exchange sent to an executor has an ambiguous identity. |
| `IX061` | An existing identity match has no record reference. |
| `IX062` | A halted exchange has no halt reason. |
| `IX063` | A committed exchange has an identity that was not resolved. |
| `IX064` | A committed exchange has no read of the target application. |
| `IX065` | An exchange from an executor does not say which exchange it answers. |
| `IX066` | An exchange sent to an executor claims a committed outcome. |
| `IX070` | A payload file is missing or its sha256 does not match. |
| `IX071` | A payload path is absolute, uses a backslash or drive letter, or leaves the exchange directory (also through a symlink). |
| `IX080` | A `no_action` outcome has no `no_action` record, or a `no_action` record is on another outcome. |
| `IX081` | A `no_action` was decided by something other than a person. |
| `IX082` | A `no_action` has no read-back evidence at the check level a commit needs. |
| `IX083` | The read-back evidence of a `no_action` shows a new or changed record. |
| `IX084` | An exchange sent to an executor claims a `no_action` outcome. |

Retired: `IX012` (a Seal on an exchange without a checksum). The checksum is now required, so a missing one is `IX001`. Retired codes are not reused within v0.

Run the checks:

```bash
python -m openadapt_flow.interchange check path/to/exchange.json
python -m openadapt_flow.interchange cases tests/fixtures/interchange
python -m openadapt_flow.interchange schema > records-interchange-v0.json
```

### Fixture harness

`tests/fixtures/interchange/cases.json` lists every case with the codes it must produce. Each invalid case is a valid case with one change, so it exercises one rule. A producer on the document side can run the same cases against its own reader, or point `cases` at its own directory in the same layout.

A conforming case can also list `expect_l1_rows`: the `manifest.csv` rows that emitting it must produce. This is the L1 conformance test: fixture X through L1 must produce row Y.

Regenerate fixtures with `python tests/fixtures/interchange/make_fixtures.py`. All fixture data is synthetic.

## Writing an exchange to an L1 extraction directory

`openadapt_flow.interchange.emit_exchange(path, extraction_dir)` checks the exchange and refuses it if it does not conform. Otherwise it calls `emit_l1_artifact` once per artifact:

- a `source_document` with a payload emits the payload file;
- a `structured_observation` emits the exchange JSON itself.

Each call appends a `manifest.csv` row and writes the provenance sidecar, as described in [L1_INTEGRATION.md](L1_INTEGRATION.md).

| Exchange member | `L1Artifact` member | `manifest.csv` column |
|---|---|---|
| `artifacts[].artifact_id` | `artifact_id` | (not written) |
| `artifacts[].kind` | `kind` | (not written) |
| `artifacts[].source_type` | `source_type` | `source_type` |
| `session_id`, else `exchange_id` | `session_id` | `session_id` |
| `artifacts[].file_number` | `patient_ref` or equivalent | `file_number` |
| `artifacts[].date`, `doctype` | `metadata` | `date`, `doctype` |
| `fields` | `payload` of a `structured_observation` | (in the JSON file) |
| `provenance.tool_name`, `producer.id`, `captured_at`, `checksum` | `provenance` | `tool_name`, `captured_at`, `sha256` of the file |

## Versioning

- The version is in `schema_version`. A reader rejects a version it does not know.
- Within v0, members can change. From v1, minor versions only add optional members, and a breaking change needs v2.
- Violation codes keep their meaning within a major version.
