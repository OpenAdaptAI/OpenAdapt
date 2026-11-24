# record/run/v1 test vectors

Shared vectors for every receipt verifier: Python (`openadapt_types.receipt`),
Cloud and the website (TypeScript). Regenerate with `python generate.py`; the
output is byte-stable. The keys come from public labels, so they are test keys,
not secrets.

## Files

- `registry.json`: the pinned key registry (`openadapt.receipt-keys/v1`), one key
  per role: `this_computer`, `customer_key` (org "Example Clinic"),
  `openadapt_cloud`, `demo`. The `stranger` key that signs `unknown-signer*` is
  not in it.
- `registry-unknown-role.json`: a registry with role `notary`. A verifier must
  refuse the whole registry (`unknown_role`).
- `<name>.json`: `{description, registry, document, openings?, expected}`.
  `document` is a DSSE envelope (`receipt.json`) or a public card
  (`openadapt.receipt-card/v1`). `openings` is the private sidecar
  (`receipt.private.json`). `expected` has `ok`, `reason`, the exact
  `signer_lines`, and for a pass `outcome`, `demo` and `disclosed`.

## Rules a verifier implements

1. **Envelope.** Exactly `payloadType`, `payload`, `signatures`. `payloadType` is
   `application/vnd.in-toto+json`, otherwise `unknown_schema`. No signatures:
   `unsigned`.
2. **Signatures.** Each is exactly `{keyid, sig}`, Ed25519 over
   `PAE = "DSSEv1 " + len(type) + " " + type + " " + len(payload) + " " + payload`
   where payload is the base64-decoded bytes, used as-is. A key id not in the
   pinned registry: `unknown_signer`, even beside a valid signature. Bad
   signature: `bad_signature`. Revoked key: `revoked_key`. Demo next to any other
   role: `mixed_demo`. The role comes only from the registry.
3. **Statement.** UTF-8 JSON, duplicate keys refused. `_type` is
   `https://in-toto.io/Statement/v1` and `predicateType` is
   `https://openadapt.ai/record/run/v1`, otherwise `unknown_schema`. Every
   string in `subject` and `predicate` is scanned for embedded content
   (`data:` URIs, `scheme://` URLs, base64 runs of 96 or more characters, image
   magic, `<svg`/`<img`, any string over 256 characters): `embedded_content`.
   Closed schema (see `openadapt_types/receipt.py`); the payload must equal its
   canonical form (sorted keys, no whitespace, no ASCII escaping), otherwise
   `malformed`. Contradictions (counts that disagree with `lines`, a VERIFIED
   receipt with a refuted line) are `inconsistent`. The predicate may carry an
   optional `evidence_scope` block (`openadapt.evidence-scope/v1`, closed; see
   `openadapt_types/evidence_scope.py`); a receipt without it still verifies.
   The block is never `null`, an optional field in it (`basis`,
   `read_back_at`, `recheck_window_s`) is left out rather than set to `null`,
   and `wire.protocols_parsed` is always present (`[]` when none): those
   payloads are not canonical, so `malformed`. With it, a `VERIFIED` outcome needs the word `VERIFIED` (level 3 or 4, with
   a basis) or `CONFIRMED`, and no other outcome may carry either; otherwise
   `inconsistent`.
4. **Commitments.** `sha256("openadapt.receipt-commitment/v1" 0x00 path 0x00
   salt(32 raw bytes) 0x00 canonical_json(value))`, as lower-case hex. Salts are
   64 lower-case hex characters.
5. **Openings.** A card reveals only fields on its `disclosure_version`
   allow-list (`openadapt.public-disclosure/v1`: `program_name`,
   `flow_version`, `github_login`), otherwise `disclosure_not_allowed`. Opened
   values are scanned for embedded content and must match their commitment
   (`opening_mismatch`). A private sidecar names the payload's SHA-256.
6. **Signer lines.** One per verified role, in the order this computer,
   customer key, OpenAdapt Cloud, public log: "Signed by this computer",
   "Signed by <org>'s key", "Countersigned by OpenAdapt Cloud", "Logged
   publicly". A demo-only receipt shows "Demo key: checks only in this demo".
   A refusal shows one "Not verified: ..." line; an unknown key shows "Not
   verified: unknown signer".
7. **Outcome.** Copied from the statement, never upgraded.
8. **Counterparty confirmations.** `predicate.confirmations[]` is optional and
   omitted when empty. Each item is closed: `kind` (`counterparty_confirmation`),
   `for_step` (null or the step of a line), `system_kind`, `read_method`
   (`portal_read_back`, `api`, `acknowledgment_message`), `state` (`sent`,
   `acknowledged`, `recorded_as_intended`, `recorded_differently`), `strength`
   (`read_back`, `acknowledgment`, `none`), `differing_count` and `commitments`
   (keys `counterparty`, `observed_at`, optional `reason`, `field.<n>`). Rules:
   `sent` has strength `none`; `acknowledged` has `acknowledgment`; both
   `recorded_*` states need a portal or API read and `read_back`;
   `differing_count` is at least 1 exactly for `recorded_differently`. Openings
   use paths `confirmations.<i>.<key>`, live only in the private sidecar, and
   are never on the public allow-list. Values: counterparty
   `<system_kind>:cp-<16 hex>`; `observed_at` ISO seconds with Z; a field
   `<field_id>:<sha256>` with `:differs` when it differs; `reason` one of
   `rejected_by_counterparty`, `conflicting_report`, `ambiguous_key`,
   `receiver_unreachable`, `not_found`, `no_answer_by_deadline`. When the reason
   is opened it must suit the state: `sent` takes any but `conflicting_report`;
   `acknowledged` takes `ambiguous_key`, `not_found` or
   `no_answer_by_deadline`; both `recorded_*` states take only
   `conflicting_report` (otherwise `inconsistent`).
   Each confirmation prints its own line in `confirmation_lines`, never in
   `signer_lines`: "Confirmed in the other party's system[ at <time>]",
   "Recorded differently in the other party's system[ at <time>]: N field(s)
   differ", "Acknowledged by the other party[ at <time>]", "Sent to the other
   party[ at <time>], not confirmed". The time appears only when
   `observed_at` is opened. `independent` is true only for a portal or API read.

A receipt never holds screenshots, typed values, URLs or page text. Evidence
files are referenced only by SHA-256 digest.
9. **Handoff payload.** A subject named `payload` binds the bytes handed to
   another party (an MCP tool result, an HTTP body or a file): its `sha256` and
   a closed `media_type` (`application/json`, `application/cbor`,
   `text/plain`, `text/csv`, `application/pdf`, `application/octet-stream`).
   Only `payload` has a media type. Given the bytes received, a verifier
   refuses a receipt with no payload subject (`payload_not_bound`) or a
   different digest (`payload_mismatch`), and reports `payload_bound: true`.
10. **Approval.** `predicate.approval` is optional and omitted when absent; a
   verifier then prints "No approval recorded". It is Flow's item approval
   binding: `mode` (`each_item`, `problems_only`, `not_gated`),
   `approver_kind` (`person`, `agent`, `none`) and `commitments` to
   `principal` (`<kind>:pr-<16 hex>`, never a name), `scope` (a 64-hex digest)
   and `approved_at` (ISO seconds), opened only from the private sidecar at
   `approval.<key>`. `not_gated` has kind `none`. Kind `none` commits to
   nothing; a `person` or `agent` approval commits at least to `principal`
   and `scope` (`approved_at` when the run recorded it). Lines: "Approved by a person: each item",
   "Approved by an agent delegate: items that failed a check", "Not gated: no
   approval was required for these writes", and for a gated run in which no
   item was approved, "Approval required for each item; no item was
   approved" (or "items that fail a check").
11. **Report digests.** `report.json` is the SHA-256 of the report file's exact
   bytes. `report.canonical` is the SHA-256 of the report's RFC 8785 (JCS)
   canonical JSON: keys sorted by UTF-16 code units, no whitespace,
   ECMAScript number form (`0.0` is `0`, `1e-7` is `1e-7`, `1e21` is
   `1e+21`), strings as `JSON.stringify` writes them. Flow writes both. A
   receipt names at least one. A verifier compares file bytes only with
   `report.json` and a canonical digest only with `report.canonical`, never
   one with the other. `jcs.json` holds canonicalization cases;
   `report-canonical.json` holds a report, its file bytes and both digests.
12. **Follow-up chains.** A follow-up record is a `record/run/v1` statement
   with `predicate.follows = {"receipt": {"sha256": <original payload SHA-256>},
   "sequence": N}` (omitted when unset) and at least one confirmation. It
   copies the original's `subject` and every predicate field except
   `confirmations` and `follows`. A chain verifies only when the original
   verifies and is not itself a follow-up, every update verifies on its own,
   is signed only by keys that signed the original, names the original's
   payload digest, counts 1, 2, 3, copies the original
   unchanged, and has the same demo marking as the original. Refusals:
   `original_refused`, `original_is_follow_up`, `update_refused`,
   `not_a_follow_up`, `wrong_original`, `out_of_sequence`, `changed_original`,
   `mixed_demo`, `different_signer`. A valid chain's `lines` are the original's confirmation
   lines, then `Update N: <line>` for each update's. `chain-*.json` holds
   `{chain: {original, updates}, expected: {ok, reason, lines}}`.
