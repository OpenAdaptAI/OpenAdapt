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
| `in_reply_to` | On a `from_executor` exchange, the `exchange_id` it answers. |
| `artifacts` | Source documents and observations, in the `L1Artifact` envelope shape. |
| `fields` | Typed values, each with its reads, confidence and grade. |
| `identities` | The parties or records the exchange is about, and how each matched. |
| `decisions` | Finite choices made by authorized people. |
| `outcome` | `pending`, `committed` or `halted`. |
| `halt_reason` | Required when `outcome` is `halted`. |
| `provenance` | Producer, tool, time and checksum. |
| `seals` | References to Seals (signed receipts) over the checksum. |
| `extensions` | Domain data under reverse-DNS keys. |

Unknown top-level members are rejected. A producer that needs to add a field adds it under `extensions`.

### Artifacts

An artifact follows the `L1Artifact` envelope used by layered records platforms, so the same file can feed an existing acquisition seam:

| Member | Meaning |
|---|---|
| `artifact_id` | Id used by `reads[].artifact_id`. |
| `kind` | `source_document` (a file to parse) or `structured_observation` (values read from a source). |
| `source_type` | How it was acquired, such as `pdf_scan`, `fax_folder`, `rdp_screen` or `api_json`. |
| `file_number`, `date`, `doctype` | The L1 naming keys: the file is emitted as `{file_number}_{date}_{doctype}`. |
| `payload` | Optional `{path, sha256, mime, bytes}` for a file next to the exchange. |
| `metadata` | Free-form source context. |

### Fields

Each value carries its evidence:

| Member | Meaning |
|---|---|
| `name` | Lowercase snake case, unique in the exchange. |
| `type` | `text`, `code`, `date`, `currency`, `integer`, `decimal`, `boolean` or `enum`, the same kinds as the reading step's record schema. Decimals and currency amounts are strings (`"1249.50"`) so no precision is lost. Dates are ISO 8601. |
| `value`, `unit` | The value, and an optional unit such as a currency code. |
| `confidence` | 0 to 1, from the producer. |
| `status` | `proposed`, `confirmed`, `corrected` or `rejected`. `confirmed` and `corrected` need `confirmed_by`, a person. |
| `grade` | How well the value was checked in the target application (below). |
| `reads` | Each observation: `channel`, observed `value`, `artifact_id`, `session` (`acting`, `reopened` or `independent`), `source_kind` on export reads (below), `locator` (page, line, byte span, screen, and a `region` with `bbox`, frame sha256 and crop sha256), time and run id. |
| `needs_review`, `review_reason` | Set when the value goes to a review queue. A reason is required. |
| `seal` | Optional per-value Seal pointer: `run_id`, `receipt_sha256`, `field_commitment`. |

Channels: `clipboard`, `ocr`, `structure` and `accessibility` read the target application through its screens. `database` and `api` do not. `document_parse` and `human` are the document side.

An `export` read must say where the export came from in `source_kind`:

- `live_session`: the export was triggered from the same signed-in UI session that did the work. It can share that session's view, so it counts as a screen read.
- `committed_store`: the export was produced from saved data by a separate process, a database read or a scheduled job. It counts as an independent read.

The grade is derived from `source_kind`. A producer records where the export came from and does not assert a stronger grade.

Read grades, weakest first:

| Grade | Meaning | Rule |
|---|---|---|
| `unconfirmed` | Not read back, one channel only, or the channels disagreed. | None. |
| `screen_readback` | Read from the screen in the session that wrote it. | Needs a read through a screen channel. |
| `committed_record_readback` | The record was closed, reopened by id and read again. | Needs a screen read with `session` set to `reopened`. |
| `verified` | Read through a channel that does not use the application's screens. | Needs a read through `database` or `api`, or an `export` with `source_kind` set to `committed_store`. |

The grade, channel and kind names match `openadapt_flow.records.schema`, the record and field schema owned by the reading step. Until that module is on main they are spelled out in `interchange/model.py`; after it merges they are imported from it, and the wire names do not change.

### Identities

An identity names the fields that identify a party or record (`fields`) and says how it matched in the target application:

- `new`: no record exists; the executor creates one.
- `existing`: one record matched; `record_ref` is required.
- `ambiguous`: more than one candidate. An exchange sent to an executor may not contain one; the document side must resolve it first. An executor that finds an ambiguous match halts and reports it.
- `not_checked`: not looked up yet. A `committed` exchange may not contain one.

### Decisions

A decision is a finite choice: `question`, `options`, `choice`, `authorized_roles` and `decided_by`. Rules:

- `decided_by.kind` must be `human`. A model's suggestion goes in `advisory` and never authorizes anything.
- `decided_by.role` must be in `authorized_roles`.
- `decided_by.id` must differ from `provenance.producer.id`: the system that produced the exchange cannot approve its own work.
- `output_field`, when set, names the field that carries the choice.

In Flow, a decision maps to a `BusinessDecisionSpec` and its signed receipt; `receipt_sha256` points at that receipt.

### Provenance, checksum and Seals

`provenance.checksum` is `sha256:` followed by the hex digest of the canonical JSON of the exchange with `provenance.checksum` and `seals` removed. Canonical JSON means sorted keys, no whitespace (`","` and `":"` separators) and UTF-8 without escaping. `openadapt_flow.interchange.compute_checksum` implements it.

Each entry in `seals` points at a Seal and states the checksum it signs in `covers`. A Seal over a different checksum, or a Seal on an exchange without a checksum, fails the check.

### Extensions

`extensions` maps a reverse-DNS key (at least two dot-separated labels, such as `org.example.referral`) to an object. The format does not interpret extension content. The reference referral template uses `org.openadapt.example.referral`.

## Conformance

A document conforms when it matches the JSON Schema and has no violation from the list below. Codes are stable within a major version.

| Code | Rule |
|---|---|
| `IX001` | Does not match the schema. |
| `IX010` | Checksum does not match the content. |
| `IX011` | A Seal covers a different checksum than the exchange has. |
| `IX012` | A Seal is present but the exchange has no checksum. |
| `IX020` | A read names an artifact that is not in the exchange. |
| `IX021` | An id or field name is used twice. |
| `IX022` | An identity names a field that is not in the exchange. |
| `IX023` | A decision choice is not one of its options. |
| `IX024` | A decision output field is not in the exchange. |
| `IX025` | A value does not match its declared type. |
| `IX030` | Verified grade without a read through an independent channel. |
| `IX031` | Read-back grade without any read of the target application. |
| `IX033` | Committed-record grade without a read of the reopened record. |
| `IX034` | A value that needs review has no reason. |
| `IX035` | An export read does not say where the export came from. |
| `IX032` | Confirmed or corrected value without a person who confirmed it. |
| `IX040` | A decision was made by something other than a person. |
| `IX041` | The producer approved its own work. |
| `IX042` | The decider's role is not authorized for the decision. |
| `IX050` | Extension key is not namespaced (use reverse-DNS, e.g. org.example.x). |
| `IX060` | An exchange sent to an executor has an ambiguous identity. |
| `IX061` | An existing identity match has no record reference. |
| `IX062` | A halted exchange has no halt reason. |
| `IX063` | A committed exchange has an identity that was not resolved. |
| `IX070` | A payload file is missing or its sha256 does not match. |
| `IX071` | A payload path is absolute or leaves the exchange directory. |

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
