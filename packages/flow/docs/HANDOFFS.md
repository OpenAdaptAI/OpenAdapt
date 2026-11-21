# Handoffs: did the other party record what we sent?

A handoff is one item a run sends to another party's system, such as a claim
to a payer or a referral to a receiving clinic. A read-back of your own
system can't tell you whether the other side recorded it. A **counterparty
confirmation** can: it is evidence read from the other party's system about
what that system recorded.

Each handoff is in one of four states:

| State | Meaning |
|---|---|
| `sent` | We sent it. The other side hasn't confirmed anything yet, or it refused the item. |
| `acknowledged` | The other side says it has the item, but not what it recorded (an X12 999, a 277CA `A1` or `A2`). |
| `recorded_as_intended` | A read-back of the other system found the item, and every compared field matches what we sent. |
| `recorded_differently` | A read-back found the item, but some fields differ. The ledger names the field ids, never the values. |

Only a read-back that compares fields can say `recorded_*`. An
acknowledgment message says what the other side received or accepted, never
what it stored in each field.

## See who still needs to follow up

```bash
openadapt handoff list --ledger /var/lib/openadapt/deferred
openadapt handoff list --config deployment.yaml --all
openadapt handoff list --ledger DIR --run runs/claim-0412 --json
```

```text
Needs follow-up (2)
  receiving clinic: recorded differently: birth_date
    sent 2026-11-21 06:00, follow up by 2026-11-22 06:00; who: referrals desk
  payer x12-277ca: sent, no answer yet; they refused it
    sent 2026-10-18 06:00, follow up by 2026-10-20 06:00; who: billing
Waiting (1)
  payer x12-277ca: acknowledged
    sent 2026-10-18 06:00, follow up by 2026-10-20 06:00

Done: 1 (--all lists them)
```

A handoff needs follow-up when it was recorded differently, refused, matched
more than one record, or is past its follow-up time and still isn't
confirmed. It is done when it was recorded as intended, or a status-only
report accepted it. `openadapt-flow handoff list` is the same command. It
reads ledgers only, and never contacts the other party.

## Producer 1: payer acknowledgments

A claim saved with a deferred check (`effects.kind: deferred_report`, see
[EFFECT_KIT.md](EFFECT_KIT.md#checked-later-deferred_report)) is a handoff to
the payer. Its handoff record lives in `<ledger>/handoffs`. `deferred settle`
records what each report says, including rows that settle nothing yet:

| The report says | Handoff |
|---|---|
| nothing yet | `sent`, follow-up due at the check's deadline |
| received (999 `A` or `E`, 277CA `A1`, any other pending status) | `acknowledged` |
| accepted (277CA `A2`) | `acknowledged`, accepted: done |
| rejected (999 `R`, `M`, `W`, `X`; 277CA `A3`, `A4`, `A6`, `A7`, `A8`) | `sent`, reason `rejected_by_counterparty` |
| two rows that disagree | `acknowledged`, reason `conflicting_report` |
| two waiting claims on one key | unchanged, reason `ambiguous_key` |
| nothing by the deadline | unchanged, reason `no_answer_by_deadline` |

The deferred check settles the run exactly as before; the handoff only
listens. Only X12 reports count as the payer's own answer. A CSV export or a
saved status page may be your own system's report, so those checks are not
handoffs.

## Producer 2: a read-back of the other system

The sender records the handoff when it sends the item, together with the
field values it sent:

```python
from openadapt_flow.handoffs import HandoffLedger, intake
from openadapt_flow.handoffs.readers import FhirSearchProfile, FhirSearchReader

ledger = HandoffLedger("/var/lib/openadapt/handoffs")
h = ledger.send(
    referral_id,
    {"family_name": (family, "text"), "birth_date": (dob, "date"), "sex": (sex, "code")},
    counterparty="receiving clinic", system_kind="ehr",
    follow_up="referrals desk", follow_up_hours=24,
)
# later
profile = FhirSearchProfile.load("fhir-patient-intake.yaml")
reader = FhirSearchReader(profile, fhir_base, token_env="RECEIVER_FHIR_TOKEN")
intake.check(ledger, h.id, referral_id, reader)
```

The reader finds the item by its key, which is the sender's reference that
the receiver keeps. Then it reports one of these:

| Read | Handoff |
|---|---|
| one record, every field matches | `recorded_as_intended` |
| one record, some fields differ | `recorded_differently`, with those field ids |
| no record | unchanged (`sent`), reason `not_found`; it follows up when due |
| more than one record | unchanged, reason `ambiguous_key` |
| the read failed (network, 4xx, 5xx, bad body) | unchanged, reason `receiver_unreachable`; it never undoes an earlier confirmation |

Fields compare by kind after normalizing, the same way as on-screen
read-backs (`readback_values`). `text` ignores case and runs of whitespace.
`code` ignores separators. `date` compares the calendar date. A field the
other side doesn't hold, or holds in a form that doesn't parse, differs.

What to read is a **profile**, which is data. `FhirSearchProfile` names the
resource, the search parameter, where each record holds the key, and a path
and kind for each field. Every record the server returns is checked for the
exact key, so a server that ignores the search parameter can't make the
wrong record "found". A UI read-back plugs in the same way: any reader that
returns a `ReadResult`.

## On the receipt

`openadapt_flow.handoffs.line` turns a handoff into the receipt's
`counterparty_confirmation` line (`predicate.confirmations[]` in
`record/run/v1`, [RECEIPTS.md](RECEIPTS.md)):

| Ledger | Line |
|---|---|
| `x12_999`, `x12_277ca`, `report_csv`, `report_text` | `read_method: acknowledgment_message`, `strength: acknowledgment` |
| `api_readback` | `api`, `read_back` |
| `ui_readback` | `portal_read_back`, `read_back` |
| `sent` | `strength: none` |
| differing field ids | `differing_count`; each compared field opens as `<field id>:<digest>`, with `:differs` when it differs |

The other party, the time and the fields are salted commitments. They open
only in `receipt.private.json`, never on a public card. A reason the schema
doesn't allow for a state stays in the local ledger and is left off the
line. `handoffs_for_run` finds a run's handoffs, and `attach_confirmations`
puts their lines on a statement before it is signed.

## Privacy

The ledger is a private directory (0700, files 0600). It holds no record
values. Keys and sent field values are HMAC-SHA256 digests of the normalized
value, under a salt kept in the ledger directory. A read-back compares
digests. No model calls.

## Limits

- **A receipt is signed once.** The lines a receipt carries are the states
  at signing time, usually `sent`. Later states live in the ledger and in
  `handoff list`. A follow-up record that names the receipt is still to be
  designed.
- **Payer reports are status only.** They never compare fields, so a payer
  handoff is never `recorded_*`.
- **The FHIR profile needs the receiver to keep the sender's key.** It must
  be in a searchable identifier, so the read can find the record.
- **`handoff send` and `handoff verify`** are being built separately. Until
  they land, the referral producer is the Python API above.
- **Real-app evidence covers one receiver.** That receiver is a synthetic
  OpenEMR 7.0.3 read through FHIR. See
  [`benchmark/handoff_receiver`](../benchmark/handoff_receiver/README.md).
