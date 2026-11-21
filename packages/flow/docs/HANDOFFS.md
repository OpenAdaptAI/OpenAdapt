# Agent-to-agent handoffs

When your agent hands a result to another agent, the run's signed receipt
can go with it. The receiving agent checks the receipt against keys it chose
to trust and gets back a verdict. A handoff works over the channels agents
already use: MCP tool results, HTTP requests and webhooks, files and email
attachments, and the A2A (Agent2Agent) protocol.

The format is described in
[HANDOFF_RECEIPTS.md](HANDOFF_RECEIPTS.md).

## What the verdict tells the receiving agent

- **Who signed it.** For example "Signed by this computer" or "Countersigned
  by OpenAdapt Cloud". The role comes from the receiver's pinned keys, never
  from the handoff.
- **Whether it is intact.** A changed receipt, an unknown signer or a changed
  payload is refused.
- **Checks passed, and what was not checked.** These come straight from the
  receipt, with its outcome copied and never upgraded.
- **Confirmed in the other party's system.** These lines appear when the run
  read the result back from the other party's system (a portal read-back or
  an API).
- **What it does not prove.** A valid signature proves who signed, not that
  the claim is true. Only a confirmation read from the other party's own
  system is independent of the sender.

The receipt signs the run, not the payload, unless the payload is the signed
report itself. The verdict says when nothing ties the payload to the receipt.

## A signature doesn't make the claim true

A receipt the sender signed can't prove the sender's claim to you. It shows
who sent the handoff and that it wasn't changed. A sender can still sign a
claim that is false. Before you act on a handoff, read your own record:

```python
from openadapt_flow.handoff import ReceiverRecord, decide

def my_record(claim):
    row = my_db.get(claim["ref"])
    if row is None:
        return ReceiverRecord("not_found")
    if row.note != claim["fields"]["note"]:
        return ReceiverRecord("recorded_differently", ("note",))
    return ReceiverRecord("recorded_as_intended")

verdict = decide(handoff, my_keys, my_record)
verdict.decision   # "accept", "reject", or "unchecked" when you passed no read
```

`decide` accepts a handoff only when both are true:

- the receipt is intact;
- your own system shows the record as the handoff describes it.

It rejects a fabricated or differing claim even when a trusted sender signed
it. It rejects when your read fails. Without a read it never accepts.

`packages/agent/tests/test_handoff_e2e.py` measures this with two parties:

- **The handoffs.** One OpenAdapt server over A2A runs 20 programs and POSTs
  20 signed handoffs to a second party's inbox: 10 true, 5 fabricated and 5
  that differ from the second party's own records.
- **The pass bar.** The second party must accept no false handoff, reject no
  true one, and spend at most 2 s checking each.

## Trusting another party's key

Each party pins the keys it trusts. Nothing in a handoff can make a key
trusted. The sender prints its key, and the receiver pins it under the
sender's name:

```bash
# Sender
openadapt handoff key > sender-key.json
# Receiver, after getting sender-key.json from the sender by a channel it trusts
openadapt handoff trust sender-key.json --org "Example Clinic" --keys trusted-keys.json
```

Receipts from that key then verify as "Signed by Example Clinic's key". They
never verify as "Signed by this computer", which is reserved for your own key.

## From the command line

```bash
# Attach a run's receipt to a result and write one JSON document
openadapt handoff send result.json --run ~/.openadapt/runs/run-123 --out result.handoff.json

# Files and email: keep the attachment as it is, and put result.pdf.handoff.json beside it
openadapt handoff send result.pdf --run ~/.openadapt/runs/run-123 --detached

# A webhook or agent inbox (https, or http to this computer)
openadapt handoff send result.json --run ~/.openadapt/runs/run-123 --post https://agent.example/inbox

# On arrival, against the keys you pin
openadapt handoff verify result.handoff.json --keys trusted-keys.json
```

`verify` exits 0 when the receipt is intact and 1 when it is refused. Add
`--json` to get the verdict as data. `receipt.private.json` never leaves the
computer: `send` refuses to attach it.

## From Python

```python
from openadapt_flow.handoff import make_handoff, receipt_for_run, verify_handoff, load_registry

doc = make_handoff({"outcome": "done"}, receipt_for_run(run_dir))
verdict = verify_handoff(doc, load_registry(Path("trusted-keys.json")))
verdict.ok, verdict.signer_lines, verdict.what_this_proves
```

## Over MCP

```bash
openadapt agent serve --bundles ./bundles --mode production --handoffs
```

This adds two read-only tools:

- **`get_handoff`** returns a finished run's result with its receipt attached.
- **`verify_handoff`** checks a handoff another agent sent. `--handoff-keys
  REGISTRY` sets which keys it trusts.

## Over the A2A protocol

```bash
pip install 'openadapt[a2a]'
openadapt agent serve --bundles ./bundles --mode production --a2a
```

This serves the same workflows as A2A skills, with an Agent Card at
`/.well-known/agent-card.json`. Each run's task result carries the receipt.
See [A2A.md](A2A.md).
