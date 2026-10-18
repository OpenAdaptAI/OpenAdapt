# Countersigned Seals

A Seal is a receipt that OpenAdapt Cloud signed with an Ed25519 key
(`openadapt.seal-signature/v1`). It says who made a statement about one write.
It does not say when the statement existed, and it can't let a holder prove a
patient's MRN to an auditor without publishing it. `openadapt-flow countersign`
adds both, plus a dossier file for disputes:

- **Trusted timestamps** (RFC 3161) over the receipt hash, from any TSA.
- **Salted field commitments** so the holder can reveal chosen fields later
  and keep the others hidden.
- **A countersignature** by a countersigning issuer, using the Seal scheme as is.
- **A dossier**: one HTML or zip file a third party can check offline.
- **`countersign verify`**: the Seal check with the executor, the verification
  tier, the timestamps, and the disclosures printed every time.

A Seal with a trusted timestamp is a Timestamped Seal. One that also carries
a countersignature is a Countersigned Seal.

Neither a Countersigned Seal nor a Timestamped Seal is a notarial act, a
government certification, or an eIDAS qualified electronic seal or qualified
electronic time stamp. This verifier checks cryptographic integrity against
pinned keys and RFC 3161 timestamp tokens; it does not consult EU Trusted
Lists (EUTL) or attest to the accuracy of the underlying system of record. A
Seal is a technical audit artifact and makes no representation as to legal
admissibility or legal effect.

OpenAdapt meters Seal issuance equally across all verification outcomes
(VERIFIED, REFUTED, and INDETERMINATE); fees are never contingent on whether
a workflow or verification check succeeds.

Nothing here runs during replay, and nothing here calls a model. Only digests
leave the machine: the TSA receives one 32-byte hash.

## Install

```bash
pip install 'openadapt-flow[countersign]'
```

This adds `asn1crypto` (pure Python, no dependencies) to parse and encode
the CMS structures in RFC 3161. Signatures and X.509 checks use
`cryptography`, which Flow already requires. `rfc3161ng` was the
alternative. It pulls in `pyasn1`, `pyasn1-modules`, and `requests`, and it
leaves certificate path checks to the caller, so it saves no code here.

## Where the data goes

The receipt and its evidence manifest are unchanged. Everything new lives
beside them in the same wire document:

```json
{
  "schema_version": "openadapt.effect-receipt/v1",
  "receipt": { "...": "unchanged, still carries seal_signature" },
  "evidence": { "...": "unchanged, still bound by evidence_digest" },
  "extensions": {
    "openadapt.countersign/v1": {
      "schema": "openadapt.countersign/v1",
      "statement": {
        "schema_version": "openadapt.countersign-statement/v1",
        "receipt_schema": "openadapt.effect-receipt/v1",
        "receipt_sha256": "…",
        "execution_id": "…",
        "nonce": "…",
        "evidence_digest": "sha256:…",
        "baseline_sha256": "sha256:… or null",
        "baseline_read_at": "… or null",
        "commitments_sha256": "sha256:… or null"
      },
      "commitments": {"schema": "openadapt.field-commitments/v1", "algorithm": "sha256-salted-256", "fields": {"f-1a2b3c4d": "sha256:…"}},
      "receipt_timestamp": {"tsa": "…", "imprint_sha256": "…", "token": "base64 DER TimeStampToken"},
      "baseline_timestamp": {"binding": {"…": "…"}, "tsa": "…", "imprint_sha256": "…", "token": "…"},
      "countersignature": {"attestation": {"…": "…"}, "seal_signature": {"schema": "openadapt.seal-signature/v1", "…": "…"}}
    }
  }
}
```

Every part of the block is optional except `statement`. Verifiers that
predate this block look for `receipt` and ignore the rest, so
`openadapt seal verify` and `openadapt_types.seal.verify_sealed_receipt`
still give the same answer.

The timestamp covers SHA-256 of the canonical `statement`. The statement
contains `receipt_sha256`, so the token covers the receipt hash, and it also
covers the commitments and the run's baseline. A token over the receipt hash
alone would leave the commitments untimed, and a holder could write new ones
after a dispute started.

## Commands

```bash
# 1. Before the executor runs: timestamp the baseline read for this run.
openadapt-flow countersign timestamp-baseline --baseline baseline.json \
  --read-at 2026-10-17T04:58:00Z --nonce "$RUN_NONCE" -o baseline-ts.json

# 2. After the receipt exists: commit fields, timestamp, countersign.
openadapt-flow countersign add receipt.json -o countersigned.json \
  --fields-file fields.json --baseline-timestamp baseline-ts.json

# 3. Check it. Exit 0 only for a genuine Seal on a verified outcome.
openadapt-flow countersign verify countersigned.json

# 4. In a dispute: reveal one field, then export a dossier.
openadapt-flow countersign disclose countersigned.json --field f-1a2b3c4d -o mrn.disclosure.json
openadapt-flow countersign dossier countersigned.json --disclosure mrn.disclosure.json \
  -o dossier.html
```

`openadapt-flow seal` already encrypts bundles, so these commands live under
`countersign`.

`fields.json` is a JSON object of field name to value, such as
`{"mrn": "004217"}`. Values never go on the command line, because shell
history and process lists would keep them. Delete the file once the vault
copy is in place.

Timestamps come from Sigstore's public timestamp service
(`https://timestamp.sigstore.dev/api/v1/timestamp`) unless you pass
`--tsa-url` for another RFC 3161 service. The Sigstore service is free, its
tokens state 1-second accuracy, and it receives only the 32-byte digest and a
nonce. `--no-timestamp` skips the timestamp.

Sigstore's public TSA is the default for evaluation and low volume. It has no
commercial SLA and may rate-limit. High-volume deployments should set their
own RFC 3161 TSA with `--tsa-url`, or use timestamping through Cloud.

Customers may point `--tsa-url` at a service run by an eIDAS qualified trust
service provider (QTSP). Deciding whether a given token is qualified is the
relying party's job, using an EUTL-conformant validator. `countersign verify`
prints the TSA's subject DN, policy OID, serial number, and genTime, and never
calls a token qualified.

`countersign add` and `timestamp-baseline` check each new token before they
write anything: the token must verify against `--tsa-cert` (default: the
bundled Sigstore root, plus the local TSA's certificate under `--tsa-local`).
A TSA that can't be reached, refuses, or returns a token that does not verify
leaves no output file and no vault entry, so a document never carries a
timestamp its creator could not check. With another `--tsa-url`, pin that
TSA with `--tsa-cert`.

`--tsa-local DIR` uses an offline TSA that keeps its key in `DIR`. Its time
is this machine's clock, so its tokens prove nothing about time to anyone
else. It exists for tests and trials without network access.

## What `countersign verify` shows

```
SIGNATURE GENUINE, NOT A FLOW REPLAY (AGENT). Pass --accept-non-flow to accept a receipt from another executor

  Executor            AGENT: a computer-use agent acted. This is not a Flow replay (declared by the run record)
  Verification tier   independent_readback (the verifier re-read the system of record or a separate session; oracle_tier 2)
  Effect verdict      CONFIRMED
  Outcome             verified
  ...
  Seal signature      VERIFIED against a pinned key (seal-ed25519-…, issued 2026-10-17T05:00:00Z)
  Evidence manifest   bound: digest and labels match the receipt
  Statement           matches the receipt
  Receipt timestamp   VERIFIED 2026-10-17T05:20:40Z (the TSA states no accuracy) by CN=DigiCert SHA256 RSA4096 Timestamp Responder 2026 1,…; policy …, serial … (chains to pinned CN=DigiCert Trusted Root G4,…)
  Timestamp skew      ok, genTime - issued_at = +40 s (limit 300 s)
  Baseline timestamp  absent
  Countersignature    absent
  Field commitments   2 committed; bound by timestamp
  Disclosure 1        f-1a2b3c4d (mrn) OK
```

The executor line always comes first. An `openadapt.execute-evidence-receipt/v1`
reads as a Flow replay because Cloud issues that schema only for a Flow runner
whose callback evidence it verified. In an effect receipt, `executor_kind` is
what the run record declared. A declared `flow_replay` there is labelled
"DECLARED by the run record and not attested by an issuer; treat as unknown".

Exit codes follow `openadapt seal verify`:

| Situation | Exit |
|---|---|
| Genuine Seal, outcome `verified` or `rolled_back_verified`, every present part checked and passed | 0 |
| Genuine Seal on `REFUTED` or `INDETERMINATE` (`halted_before_effect`, `reconciliation_required`) | 1 |
| Effect receipt from anything but an issuer-attested Flow replay, without `--accept-non-flow` | 1 |
| A timestamp is present but no TSA certificate was pinned (`not_checked`) | 1 |
| Any failed check: signature, evidence, statement, timestamp, countersignature, disclosure | 1 |
| A disclosure checked against commitments that nothing binds | 1 |

`--json` prints the same report as data.

`verify` also compares the receipt timestamp's genTime with the receipt's
`issued_at` and prints the difference. Within 300 seconds either way is `ok`.
A genTime more than 300 seconds after `issued_at` is `backdated_receipt`, and
more than an hour after is `late_timestamp`. A genTime more than 300 seconds
before `issued_at` is `clock_skew`. Each prints as a `WARNING:` line under the
headline and appears in `--json` as `warnings` and `timing`. The warning says
the times disagree; it doesn't say who is at fault, and it doesn't change the
exit code.

## Trust

Verification reads only what the holder pinned:

- Seal keys come from the same places `openadapt-agent seal` reads: `--keys`,
  `OPENADAPT_SEAL_KEYS_PATH`, `OPENADAPT_SEAL_KEYS_JSON`, or
  `~/.openadapt/seal-keys.json`. Nothing is fetched.
- TSA trust comes from `--tsa-cert`: the TSA's certificate or a CA above it.
  Without it, the verifier pins Sigstore's TSA root, shipped in
  `openadapt_flow/countersign/trust/sigstore-tsa-root.pem` (SHA-256 fingerprint
  `2A:CA:8F:EA:...:D6:33`, valid until 2035-04-06). A token from another TSA
  then fails until you pin that TSA's certificate.
  A certificate inside the token or the document is used to build the path,
  never as an anchor. Each link must be valid at the token's `genTime`.
  The TSA certificate must carry a critical extended key usage of
  `timeStamping` alone (RFC 3161 section 2.3). Each issuer must be a CA whose
  key usage allows certificate signing, whose path length allows the chain
  below it, and whose extended key usage, if any, includes `timeStamping`.
  The ESS signing-certificate-v2 attribute (RFC 5035) must name the signer
  certificate by a SHA-2 hash and, when present, by issuer and serial number.
  A token with only the SHA-1 ESS v1 attribute is refused. A TSTInfo with a
  critical extension is refused.

The countersignature is a `openadapt.seal-signature/v1` over an
`openadapt.countersign-attestation/v1` object. It reuses the Seal key registry,
pinning, and `openadapt_types.seal.verify_seal_signature` unchanged. The
attestation's `schema_version` is inside the signed hash, which keeps an
attestation from being read as a receipt. Cloud has no countersigning
endpoint yet; `CloudCountersignIssuer` is an interface stub that raises, and
`LocalCountersignIssuer` signs with a local key for tests and self-hosted
trials.

## Field commitments

Each field gets its own 32-byte random salt. The commitment is

```
sha256("openadapt.field-commitment/v1\0" || canonical({label, name, salt, value}))
```

with `salt` in base64 and canonical JSON as in RFC-0002 (sorted keys, compact
separators, UTF-8). Labels are `f-` plus random hex, so the block names no
field. A disclosure bundle (`openadapt.field-disclosure/v1`) gives the label,
name, value, and salt of each revealed field, and anyone can recompute it.

Per-field salts are what make selective disclosure work. A per-run salt,
which RFC-0002 allows for identity digests, has to be revealed to check any
one field, and once it is out every other low-entropy field from that run can
be found by enumeration again. The commitments sit beside the receipt's own
identity digests and do not replace them.

A disclosure counts only when the commitments are bound by a verified
timestamp or countersignature. Otherwise anyone could have written the block
after the fact, and `countersign verify` reports `commitments_unbound`.

## The vault

`countersign add` writes the salts and values to
`$OPENADAPT_COUNTERSIGN_VAULT/<receipt_sha256>/field-secrets.json` (default
`~/.openadapt/countersign-vault`), mode 0600 in a 0700 directory, together
with a copy of the countersigned document. The vault holds PHI. Back it up and encrypt
it the way you do the system of record. Losing it loses the ability to
disclose; it never weakens the commitments. On Windows, the mode bits do not
apply; rely on the profile directory's ACL.

## The exception queue

`countersign add --keys seal-keys.json` queues an exception for each genuine
Seal on a REFUTED or INDETERMINATE outcome. A Seal that doesn't verify against
the pinned keys isn't queued. Entries are stored in
`<vault>/exceptions/<receipt_sha256>.json`, mode 0600. They hold digests,
ids, and timestamps, and never field values or notes. The Needs Attention
queue in the console is a read-only view of run folders, so it can't record
how an exception ended. This queue uses the same category names
(`effect_refuted`, `effect_indeterminate`).

An entry starts `open` and ends in one of two states. One state never counts
as the other:

- **`resolved_by_seal`** (`countersign resolve <entry> --with fix.json`): a
  later receipt carries a genuine Seal on a `verified` or
  `rolled_back_verified` outcome, checked against the keys the entry was
  queued under. The receipt must link to the failed run, through
  `remediates_receipt_id` or a shared non-PHI `correlation_id`. Both runs must
  have the same `workflow_digest`, contract, and committed parameters. The
  parameters are compared on this machine, from the vault, against the
  commitments each timestamp or countersignature binds. Commit the run's
  parameters with `--fields-file` on both receipts. Any executor counts,
  including a person's verify-run receipt (`executor_kind` `human`), because a
  verified outcome already requires an independent readback. The entry records
  `resolution_receipt_id`.
- **`closed_by_attestation`** (`countersign close`): a signed
  `openadapt.exception-disposition/v1` record. It names an opaque approver id
  of kind `person` and a role, `dispositioned_at` in RFC 3339 UTC, a
  `disposition_code` (`remediated_manually_in_sor`, `cancelled_by_requester`,
  `superseded_by_external_workflow`, `verified_manually_oracle_unavailable`),
  and a `verification_method` (`sor_direct_inspection`,
  `ehr_audit_log_review`, `counterparty_confirmation`,
  `task_cancelled_no_check_needed`). The ticket id goes in only as an
  HMAC-SHA256 digest, under a key kept in the vault. Free-text notes
  (`--notes-file`) stay in the vault, mode 0600; only their HMAC is in the
  record. The signature must verify against `--approver-keys`. The key that
  sealed the failed run, or the run's agent or script executor, can't close it.

No flag marks an entry resolved or closed on someone's word.

Closed entries are kept for 6 years (2,190 days) after they close. Set
`--retention-days` on `add` or `OPENADAPT_EXCEPTION_RETENTION_DAYS`; the
minimum is 183 days. `countersign purge --export FILE` first writes the
expired entries to `FILE` (mode 0600, never overwritten) and reads it back,
and only then deletes them. Open entries are never purged. `countersign
exceptions` lists entries; `--status all --json` lists every one.

## The dossier

`countersign dossier` writes one HTML page, or a zip with the page plus the raw
token files and an `openssl` recipe. The page contains the receipt, the
evidence manifest, the countersign block, the report computed at export, and the
pinned keys used. When opened, it checks in the browser the receipt hash,
the Ed25519 Seal signature against the embedded key, the evidence digest, and
each disclosed commitment. RFC 3161 tokens need `openssl ts -verify` or
`openadapt-flow countersign verify dossier.html`; the page prints the command.

The dossier also lists every exception in the vault (`--vault`), open and
closed, under a header that adds up: Total Seals = Verified +
Resolved_by_seal + Closed_by_attestation + Open. A genuine Seal on an
unverified outcome that was never queued counts as open. Each entry shows its
status, verdict, receipt hash, and how it ended: the resolving receipt id, or
the disposition code, approver id and role, verification method, and time.
Notes, ticket ids, and field values are not included.

By default the dossier has no PHI. Disclosed values appear only when you pass
a disclosure bundle, and only when every bundle matches commitments that a
verified timestamp or countersignature binds; otherwise `dossier` refuses.
Only the label, name, value, and salt of each disclosed field are copied in. Screenshots appear only with `--screenshot` and only
after automatic redaction by openadapt-privacy. Without the `privacy` extra
they are left out, and the page says so. Automatic redaction can miss text,
and screenshots are not bound to the receipt, so review a dossier before you
send it.

## What a timestamp proves, and what it does not

A verified RFC 3161 token shows that the TSA signed this exact statement
hash with a time of `genTime`, within the TSA's stated accuracy. If the TSA's
key and clock were sound, the hash existed no later than that time. So the receipt,
its evidence digest, and its commitments existed by then and have not
changed since.

It does not prove:

- that the receipt is true, or that the write happened. The read-back and
  the Seal carry that claim; the timestamp only fixes when the claim existed.
- when the write happened. A receipt can be timestamped long after the run.
- that the baseline preceded the executor. A baseline token proves the
  snapshot for this run's nonce existed by its `genTime`. Comparing that
  with the earliest executor action needs another clock, such as the EHR's
  audit log.
- anything about who asked for the timestamp. TSAs stamp any hash.
- that the TSA's clock was right, or that its key was not compromised.
  Revocation (CRL or OCSP) is not checked offline, and a token signed after
  the TSA key's compromise date verifies the same. Long-term validity needs
  archive timestamps (RFC 4998 or CAdES-A), which this does not implement.
- that the time is precise. Sigstore and the local TSA claim one
  second; DigiCert's tokens state no accuracy.

A token from `--tsa-local` proves only what this machine's clock said.

## Tests

`tests/test_countersign.py` runs offline. It covers the enumeration an
unsalted hash allows over a small MRN domain, wrong disclosed values, a real Sigstore token
(`tests/fixtures/countersign/sigstore-token.der`) checked against the bundled
root, a real DigiCert token
(`tests/fixtures/countersign/digicert-token.der`, captured on 2026-10-17 with
`openssl ts -query`) checked against DigiCert Trusted Root G4, `openssl ts
-verify` on local tokens, tampering after countersigning, baseline nonce
binding, exit codes for REFUTED and INDETERMINATE Seals, and a dossier with
no PHI by default. It also checks that a TSA certificate needs a critical
`timeStamping`-only EKU; that issuers without certificate signing, with
another EKU, or beyond their path length break the chain; that a token which
does not verify, or an unreachable TSA, leaves no output and no vault
entry; that the countersigning issuer receives only digests; and that a
dossier refuses unbound or non-matching disclosures and copies only their
defined keys.
