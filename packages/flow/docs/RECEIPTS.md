# Receipts

A receipt is a signed, itemized record of one run: what it changed, how each
change was checked, and who signed it. It doesn't grade the run.

## What every run writes

`openadapt-flow run` and `replay` with `--run-dir`, and the quickstart, write
two files beside `report.json` when the run ends, whatever its outcome:

| File | What it is | Share it? |
|---|---|---|
| `receipt.json` | The receipt: a DSSE envelope over an in-toto statement, predicate type `https://openadapt.ai/record/run/v1`, signed with this computer's key | Yes, as a public card (below) |
| `receipt.private.json` | The salts and values behind the receipt's commitments (`openadapt.receipt-openings/v1`, mode 0600) | No. It never leaves the computer |

A stopped run gets a receipt too. The outcome (`VERIFIED`, `STOPPED`,
`UNKNOWN` or `FAILED`) is copied from the run and never upgraded: signing
isn't grading. `OPENADAPT_RECEIPT=off` turns receipts off. Writing one never
changes the run's exit code and calls no model.

## What a receipt says

- the SHA-256 of `report.json` and of the bundle that ran;
- the program's id (a hash, never its name) and version digest;
- the hour the run started;
- the outcome and a reason code;
- one line per write, with where it went (`gui` or `api`), the operation,
  its check (`confirmed`, `refuted` or `not_checked`), the check's strength
  (`read_back`, `same_screen` or `none`), and what it didn't check;
- check counts;
- a disclosure map: salted commitments to the program name, Flow version and
  run id;
- an evidence scope (`evidence_scope`, `openadapt.evidence-scope/v1`): the
  check level the weakest save reached, the receipt word (`VERIFIED` only at
  level 3, a separate session, or level 4, the system of record, and then
  with its basis; otherwise `SHOWN` or `RECORDED`), the wire the run watched,
  and what the evidence did not cover. It always lists server-side async and
  outbound effects, and, because a run report has no per-read time yet,
  `read_back_time`. `VERIFIED` means the read-back showed the value when it
  ran, not that it persisted. A run whose saves were checked below level 3
  gets `UNKNOWN` (`below_verified_level`), never `VERIFIED`. Receipts written
  before this field, and a `VERIFIED` run whose report has no `readback`
  block (it made no save), have no evidence scope and still verify.

**A receipt and its public card never contain screenshots, typed values,
URLs or page text.** Evidence files, screenshots included, appear only by
SHA-256 digest. A verifier refuses a receipt or card that carries an image,
a base64 blob, a URL or long text (`embedded_content`).

A receipt may also carry **counterparty confirmations**: evidence read from
another party's system about a handoff, such as a referral a portal later
shows as recorded. Each confirmation states the kind of system, how it was
read (a portal read-back, an API, or an acknowledgment message), its state
(`sent`, `acknowledged`, `recorded_as_intended` or `recorded_differently`) and
its own strength. Who the other party is, when it was observed, which fields
it showed and any reason are salted commitments, never shown on a public card.
The verifier prints each one on its own line, for example "Confirmed in the
other party's system at 2026-11-20T23:41:07Z". That line never merges with or
upgrades the signer line: "Signed by this computer" still means only that this
computer signed the statement.

A receipt handed to another party can also be **bound to the bytes it hands
over**: a `payload` subject names the SHA-256 and media type of the MCP tool
result, HTTP body or file. The receiver checks the bytes it got against it.
And it says **how its writes were approved**, from Flow's item approval
binding ([APPROVAL_AND_VERIFICATION.md](APPROVAL_AND_VERIFICATION.md)): the
approval mode (`each_item`, `problems_only` or `not_gated`) and the kind of
approver (a person or an agent delegate) are visible; who approved, the
scope and the time are commitments. A receipt with no approval block says
"No approval recorded", so a receipt never implies authority it doesn't
record.

## Who signed it

Each signature names a key id. The verifier's pinned key registry maps that
id to a role; the receipt can't choose its own role.

| Role | Line shown |
|---|---|
| This computer | Signed by this computer |
| The customer's key | Signed by `<org>`'s key |
| OpenAdapt Cloud | Countersigned by OpenAdapt Cloud |
| A public log | Logged publicly |
| A demo key | Demo key: checks only in this demo |
| Anything else | Not verified: unknown signer |

OpenAdapt Cloud countersigns the same envelope; it doesn't issue a separate
object. An unknown key, role or schema fails closed, even beside a valid
signature, and so does a demo key next to a real one.

## Check one

```bash
openadapt verify RUN_DIR              # a run folder, or a folder of them
openadapt verify receipt.json         # one file
openadapt verify https://...          # a published link
openadapt verify FILE --keys keys.json  # also trust an openadapt.receipt-keys/v1 registry
```

`openadapt receipt verify` is the same command. It checks the signatures,
that `report.json` still has the bytes the receipt names, and, with
`receipt.private.json` beside it, every opened value.

Older files still verify, labelled "older format": `seal.json`
(`local-seal/v1`), `public-seal.json` (`public-seal/v1`), Cloud-signed
Execute receipts (`seal-signature/v1`), run summaries (`run-receipt/v2`),
write receipts (`write-receipt/v1`) and transaction receipts
(`tx-receipt/v1` to `v3`, with `--public-key`). For one release Flow also
writes `seal.json` beside `receipt.json`; see
[OFFLINE_SEALS.md](OFFLINE_SEALS.md) for that format, its keys and its
public-log anchor.

## Share one

The public card is the same signed envelope with only allow-listed values
opened. Because the payload is never re-signed, a card can't say anything the
full receipt doesn't. The allow-list is closed and versioned:
`openadapt.public-disclosure/v1` may open the program name, the Flow version
and a GitHub login, and nothing else.

`openadapt receipt publish RUN_DIR` shows every public field and uploads only
after you say yes; `openadapt receipt unpublish RUN_DIR` takes it down. It
uploads an allow-listed card only: never `report.json`, `receipt.private.json`
or a screenshot. Until OpenAdapt Cloud accepts receipt cards, the card it
uploads is the older `public-seal/v1` card.

`openadapt receipt view RUN_DIR` writes the run's offline page. It may show
the run's screenshots, read from the local run folder on the same computer,
labelled "From this computer's run folder: not part of the receipt, never
uploaded".

The format, the commitment formula and shared test vectors are in
[`packages/types/tests/vectors/record-run-v1`](../../types/tests/vectors/record-run-v1/README.md).

## The run summary

```bash
openadapt-flow report-run <run-dir> --receipt share/ --production
```

```bash
openadapt-flow report-run <run-dir> --receipt share/ --production
```

Writes `share/run-summary.png`, `run-summary.json`, and `run-summary.md` locally and
contacts nothing. Only a `VERIFIED` run may use the success rail, so an
unverified run still emits nothing.

### The summary is additive, never redacted

The receipt is **generated from a closed allow-list, never redacted from the run
report**. Subtractive redaction of a run report is unwinnable: burned-in pixels,
OCR text captured precisely because it identifies a record, and free-form halt
reasons all leak, and one missed field is a breach. So the receipt declares its
complete field set and refuses any key outside it:

- outcome, profile, and transaction class (closed enums)
- exact authorization / identity / postcondition / effect coverage
- step, heal, and model-call counts
- the zero over-halt counter
- duration
- the resolution-rung histogram
- evidence classes
- substrate
- a validated package version
- the bundle and receipt digests
- explicit provenance
- an hour-truncated timestamp

There is no screenshot, OCR text, typed value, parameter, URL, hostname,
coordinate, workflow name, operator label, or free text.

### Read every byte before you post it

`run-summary.json` is every byte that would leave the machine, so you can read it
before you post it. A receipt emitted directly by the bundled tutorial is marked
`synthetic-tutorial` and contains no real data by construction. A separate
`report-run --receipt` invocation refuses to guess provenance: pass
`--production` for a saved run. The `tutorial` command emits its bundled
reference receipt directly; a deserialized report cannot prove that provenance.
Route a production receipt through `sanitize` / `review-sanitized` /
`approve-sanitized` before it crosses a trust boundary. See
[`SANITIZED_ARTIFACTS.md`](SANITIZED_ARTIFACTS.md).
