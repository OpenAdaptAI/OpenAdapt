# Teach authorization

A teach authorization (`openadapt.teach-authorization/v1`) is a short
statement a person signs before an agent teaches or repairs a program. It
names what the agent may do:

- the task (`task_id` and a one-line summary);
- the app origins the browser may reach;
- the inputs the agent may type, each spelled out or given as a SHA-256 the
  operator's value must match;
- the one permitted write: the record kind, the identity of the intended
  record, the fields and values the save may write, and how many rows;
- forbidden actions (drag, right click, a `DELETE` request, a new window, a
  download, a file chooser) and forbidden controls by label;
- whether it covers `teach`, `repair` or both, and when it expires.

The teach scope (`openadapt_flow.teach_scope`, see
[THREAT_MODEL.md](THREAT_MODEL.md)) is then bound from the signed statement,
not from the agent's own description of the task. Admission checks the
signature again. The module is `openadapt_flow.teach_authorization`.

## Who signs, and how it is checked

The signer is a person on the signed approver roster, with the same approver
key that answers saves ([APPROVAL_MODES.md](APPROVAL_MODES.md#the-approver-key)).
The signature is an approver attestation over the canonical statement, kept
beside it as `teach-authorization.json.sig`. Verification uses the approver
verifier (`approval_mode.human_attestation.refusal`):

- The caller pins the approver root (`--approver-root`, or
  `OPENADAPT_APPROVER_ROOT`) and the key directory that holds the roster
  (`--key-dir`, or `OPENADAPT_JIT_KEY_DIR`). Without a pin, nothing verifies.
- The signer must be on that roster when the check runs, under the
  statement's `authorizer` name, with the statement's `authorizer_public_key`.
  A key that was never added refuses (`teach_auth_unknown_signer`), and so
  does one an approver removed since (`approver remove`). The roster must
  extend every head recorded earlier, so an older roster put back refuses.
- A changed statement refuses (`teach_auth_bad_signature`). So does a
  signature from the decision portal's key (`teach_auth_method`): that key
  signs only answers a person gave on their phone.
- The roster never holds an agent: a principal that names an agent or model,
  or a key that holds another role, is not an approver. Admission also
  refuses a statement signed by the agent that recorded the draft.

## Register a key and sign

On a Mac, Desktop's approver key (behind Touch ID) signs: Desktop runs
`teach-authorization sign STATEMENT --approver-signer stdio
--approver-public-key HEX` and answers the signing request over stdin, as it
does for `item-approval`.

Without a Secure Enclave (Windows, Linux, a Mac without Desktop), use an
encrypted software key file:

```bash
export OPENADAPT_JIT_KEY_PASSPHRASE=...      # never on the command line
openadapt-flow jit keygen --out clinic-lead.pem
# An approver already on the roster adds it (or start a roster with it:
# openadapt-flow jit approver init --principal clinic-lead --key clinic-lead.pem)
openadapt-flow jit approver add --principal clinic-lead \
  --public-key PUBLIC_KEY_HEX --signer-key ADMIN.pem --approver-root ROOT_HEX
```

On Windows and Linux, `openadapt-desktop approver-key setup` also creates
the key file (its passphrase in the OS keychain) and prints the same
`jit approver` commands.

Then write and sign the statement:

```bash
openadapt-flow teach-authorization create spec.json --out auth/ \
  --authorizer clinic-lead --authorizer-public-key PUBLIC_KEY_HEX \
  --expires-in-hours 24
openadapt-flow teach-authorization sign auth/ --approver-key clinic-lead.pem \
  --approver-root ROOT_HEX
openadapt-flow teach-authorization verify auth/ --approver-root ROOT_HEX
```

`sign` with a key file shows what the statement allows and needs a person at
an interactive terminal who types `yes`.

## Where it is required

| Entry point | How it is bound | Without one |
| --- | --- | --- |
| JIT capture (`compile_agent(..., teach_authorization=TeachAuthority(...))`) | The capture's scope is bound from the statement; a `teach_scope` may only narrow it. The statement is kept beside the draft, and its digest is in the signed recorder block | Admission refuses (`teach_auth_not_recorded`) |
| `compile-agent` (`--teach-authorization`, `FirstRunCompiler`, `compile_first_run`, `SkillCache`) | Run 1's scope is the statement's alone; the command's `--expect`, `--on-screen` and `--allow-text` don't widen it | Admission refuses |
| Teach over MCP (`serve --teach --teach-authorization`; `teach_act`) | Run 1 runs under the statement's scope, under its task id; the agent's declared inputs must be the statement's | Admission (`teach_admit`, `compile-agent admit`) refuses |
| Browser teach writes (`serve --teach-browser --teach-authorization`) | Run 1 of a write starts only when the statement verifies, names the app's origin and names the write contract as its `record_kind`. The tool keeps a copy, and every run of the tool checks it again | No write tool is taught, and a write tool without one doesn't run. Read-only tasks need none: the lane sends no write |
| Agent-demonstrated fix (`teach_repair`, `serve --teach-authorization`) | The desk verifies a statement that covers `repair` and names the workflow as its task before the click; the repair store keeps it | The fix is proposed, and `RepairStore.approve` refuses it |
| Self-repair of an agent-taught program (`admit_repair`) | The prior admission's authorization must cover `repair` and verify now; its statement is copied into the repaired bundle | The repair's admission (probe or full) refuses |
| Airbag handoff (`agent_guard.handoff`) | Not bound: the guarded run 1 runs under the guard's task file, and the handoff can't attach a person's signature to it yet | Admission refuses its draft (`teach_auth_not_recorded`). Teach the task through `compile-agent` or `serve --teach` with an authorization instead |

Every JIT draft is compiled from an agent's run, so admission requires the
authorization for every JIT draft, in each admission mode (a person, an
admitter key, an agent under a signed policy). Admission reads the signed
recorder block, not the editable candidate fields, and checks: the kept
statement's digest equals the recorded one; the signature verifies against
the roster now; the task is the draft's; the capture ran under the scope the
statement binds, and its read-back verified.

## Limits

- The approver root comes from the caller: a flag, the environment or a
  deployment config. A process that controls both the environment and the
  key directory can pin its own roster. Keep the key directory and the
  approver keys away from the agents they govern, as for approvals.
- Self-repair of a program a person demonstrated (not a JIT draft) is a
  repair a person reviews; it carries no teach authorization.
- In the browser-teach lane the statement bounds the origin and the record
  kind; the writes themselves are bounded by the lane's write contract and
  its pinned run-1 writes, not by the statement's field values.
- An agent-demonstrated fix is recognized by its provenance, a JSON file in
  the repair store that the store doesn't sign. A process that can write the
  store can strip it, and the approval then asks for no authorization.
