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

- The caller pins the approver root and the key directory that holds the
  roster. Commands a person runs (`jit admit`, `compile-agent`,
  `teach-authorization`) take `--approver-root` or `OPENADAPT_APPROVER_ROOT`,
  and `--key-dir` or `OPENADAPT_JIT_KEY_DIR`; repair approval reads the two
  environment variables. A
  server the agent runs (`openadapt-agent serve --teach`, its repair desk)
  takes them only from the deployment config's `approvers` section
  (`--config`): its root, key directory and recorded head. Without a pin,
  nothing verifies.
- The signer must be on that roster when the check runs, under the
  statement's `authorizer` name, with the statement's `authorizer_public_key`.
  A key that was never added refuses (`teach_auth_unknown_signer`), and so
  does one an approver removed since (`approver remove`). The roster must
  extend every head recorded earlier, so an older roster put back refuses.
- A changed statement refuses (`teach_auth_bad_signature`). The signature
  is over its own domain (`openadapt.teach-authorization/v1`), so a signed
  save approval or roster change never counts as one.
- The roster never holds an agent: a principal that names an agent or model,
  or a key that holds another role, is not an approver. Admission also
  refuses a statement signed by the agent that recorded the draft.

## Register a key and sign

The signer uses an encrypted approver key file, on any operating system;
no Secure Enclave is needed:

```bash
export OPENADAPT_JIT_KEY_PASSPHRASE=...      # never on the command line
openadapt-flow jit keygen --out clinic-lead.pem
# An approver already on the roster adds it (or start a roster with it:
# openadapt-flow jit approver init --principal clinic-lead --key clinic-lead.pem)
openadapt-flow jit approver add --principal clinic-lead \
  --public-key PUBLIC_KEY_HEX --signer-key ADMIN.pem --approver-root ROOT_HEX
```

On Windows and Linux, `openadapt-desktop approver-key setup` also creates
such a key file (its passphrase in the OS keychain) and prints the same
`jit approver` commands. Desktop's Touch ID key on a Mac answers item
approvals only; it doesn't sign teach authorizations.

Then write and sign the statement:

```bash
openadapt-flow teach-authorization create spec.json --out auth/ \
  --authorizer clinic-lead --authorizer-public-key PUBLIC_KEY_HEX
openadapt-flow teach-authorization sign auth/ --approver-key clinic-lead.pem \
  --approver-root ROOT_HEX
openadapt-flow teach-authorization verify auth/ --approver-root ROOT_HEX
```

A statement expires 24 hours after `create` unless `--expires-in-hours`
(or `expires_at` in the Python API) says otherwise, at most 90 days on. `sign` shows what the statement allows and
needs a person at an interactive terminal who types `yes`.

## Where it is required

| Entry point | How it is bound | Without one |
| --- | --- | --- |
| JIT capture (`compile_agent(..., teach_authorization=TeachAuthority(...))`) | The capture's scope is bound from the statement; a `teach_scope` may only narrow it. The statement is kept beside the draft, and its digest is in the signed recorder block | Admission refuses (`teach_auth_not_recorded`) |
| `compile-agent` (`--teach-authorization`, `FirstRunCompiler`, `compile_first_run`, `SkillCache`) | Run 1's scope is the statement's alone; the command's `--expect`, `--on-screen` and `--allow-text` don't widen it. For `SkillCache`, the statement names `SkillCache.task_id(prompt, params)` | Admission refuses |
| Teach over MCP (`serve --teach --teach-authorization`; `teach_act` and `teach_run`) | Run 1 runs under the statement's scope, under its task id; the agent's declared inputs must be the statement's. `teach_run`'s scripts act through the same run 1, so every action and the save are checked against the same scope | Admission (`teach_admit`, `compile-agent admit`) refuses |
| Browser teach writes (`serve --teach-browser --teach-authorization --config`, `teach_start` with a contract) | Run 1 starts only when the statement verifies for task `browser-tool.<name>` (a namespace of its own), expires within 90 days, names the app's origin, the contract as its `record_kind` and a write adapter, takes run 1's inputs, allows the contract's row count, and has exactly the record fields the contract's `record_written` match checks as its identity and fields, with run 1's values (a contract with other checks is refused). During run 1 the adapter reads every write before it is sent (and counts them), the browser reaches only the statement's origins and opens no WebSocket, only the statement's values may be typed, `press` takes no character keys, and the forbidden actions are watched. The tool keeps the verified copy and is signed with the local admission key in the config's `approvers.key_dir`; every later call checks the signature, the kept statement against the config's roster (purpose, task, expiry), the origin and the record kind. The statement's values, forbidden controls and actions bind run 1 only | No write tool is taught, and a write tool without a statement that holds doesn't run. The read-only lane sends no write and needs none |
| Agent-demonstrated fix (`teach_repair`, `serve --teach-authorization`) | Before the click, the desk verifies a statement that covers `repair` and names the program's workflow name as its task; the repair store keeps it. Approval checks it again, for that workflow | The fix is proposed, and `RepairStore.approve` refuses it |
| Self-repair of an agent-taught program (`admit_repair`) | The prior admission's authorization must cover `repair` and verify now; its statement is copied into the repaired bundle | The repair's admission (probe or full) refuses |
| Airbag handoff (`agent_guard.handoff`) | Not bound: the guarded run 1 runs under the guard's task file, and the handoff can't attach a person's signature to it yet | Admission refuses its draft (`teach_auth_not_recorded`). Teach the task through `compile-agent` or `serve --teach` with an authorization instead |

Every JIT draft is compiled from an agent's run, so admission requires the
authorization for every JIT draft, in each admission mode (a person, an
admitter key, an agent under a signed policy). Admission reads the signed
recorder block, not the editable candidate fields, and checks: the kept
statement's digest equals the recorded one; the signature verifies against
the roster now; the task is the draft's; the capture ran under the scope the
statement binds, and its read-back verified.

## Upgrading

Programs and tools made before teach authorizations carry none, so:

- a JIT draft waiting for admission is refused. Teach it again with an
  authorization.
- an admitted JIT program still runs, but a self-repair of it is refused
  until a person teaches it again with an authorization that covers
  `repair` and admits it.
- a write tool taught in browser teach before teach authorizations doesn't
  run. Teach it again under a statement.
- the benchmarks that compile an agent's run and `examples/jit/run_mockmed.py`
  now sign a throwaway authorization for each run
  (`teach_authorization_fixture.demo_authority`), standing in for the person.

## Limits
- A browser-teach write tool is bound to its statement's values for run 1
  only (the summary `sign` shows says so). Later calls take new inputs; the contract's read-back and the pinned
  run-1 writes bound them, and each call checks the statement's signature,
  roster, purpose, task and expiry again. When the statement expires (at
  most 90 days on), the tool stops until it is taught again.

- The approver root comes from the caller: a flag, the environment or a
  deployment config. A process that controls both the environment and the
  key directory can pin its own roster. Keep the key directory and the
  approver keys away from the agents they govern, as for approvals.
- Self-repair's carried admission (`run --self-repair`) and repair approval
  take the root from `OPENADAPT_APPROVER_ROOT` and the roster from
  `OPENADAPT_JIT_KEY_DIR`, not from a governed run's deployment config.
  Without them they refuse.
- `serve --teach` reads the roster from the config's `approvers.key_dir`
  when it names one, else from `OPENADAPT_JIT_KEY_DIR`. Pin `approvers.head`
  in the config too, or a copied older roster in that folder isn't caught.
- Self-repair of a program a person demonstrated (not a JIT draft) is a
  repair a person reviews; it carries no teach authorization.
- A statement may be used for any number of teach runs of its task until it
  expires; its nonce is not spent. Revoking the signer stops new admissions
  and repairs, not programs already admitted.
- Forbidden controls are matched by every name the target goes by: its
  accessible name from Chromium's accessibility tree, the text of the hit
  element and three ancestors (a control with no role, such as a
  `<div onclick>`), and for a `<label>` the name of the control it is for.
  These are read over DevTools, the text and the label in an isolated
  world, so a page script that tampers with how controls are read doesn't
  change them; a page that renames a control through ARIA or its text
  does, and against a hostile page the write adapter and the read-back are
  what bind the save. A click is checked against the control under it.
  Every key (Enter, Space, any other key, typing) is checked when it
  arrives against the control with focus, in any frame, and Enter in a
  form field also against the button the browser would submit the form
  with (`:default`, read in an isolated world). Key combinations with a
  modifier are refused. A click with no position, a name that can't be
  read, focus in a frame the tree can't see, or a surface without a web
  page is refused when the statement forbids any control. In browser
  teach, a page could still move focus between the check and the key.
- A browser-teach tool taught again can be rolled back by an agent that can
  write the tools folder: it can put back the older signed program and its
  statement, which run until that statement expires.
- A server that teaches browser writes signs each tool with the admission
  key in the config's `approvers.key_dir`, so it reads that key. Run it as
  a user the agent can't act as (Desktop's engine, or a service account);
  an agent that can read the key can sign a tool program itself.
- An agent-demonstrated fix is recognized by its provenance, a JSON file in
  the repair store that the store doesn't sign. A process that can write the
  store can strip it, and the approval then asks for no authorization.
