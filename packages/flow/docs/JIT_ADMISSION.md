# Admitting a compiled CUA run

A bundle compiled from a computer-use agent run starts as a `DRAFT`. It
becomes `ADMITTED` in one of three ways, and all three write the same signed
`admission.json`. A person doesn't have to be in the loop for every draft.
They decide once which keys and which policy may admit, and Flow checks the
evidence each time. An admitted bundle never goes back to being a draft: a
changed program is a new recording and a new draft. The code is in
[`openadapt_flow/jit/lifecycle.py`](../openadapt_flow/jit/lifecycle.py) and
[`openadapt_flow/jit/authority.py`](../openadapt_flow/jit/authority.py). For
the wrapper itself, see [JIT.md](JIT.md).

| Mode | Who admits | A person's part | Needs a step review |
| --- | --- | --- | --- |
| Agent under policy | a registered agent, with its own key | signs an admission policy once | no; the policy's scope and canaries stand in for it |
| Admitter key | a CI job or script, with an admitter key | creates and registers the key | no |
| Person | a named person at an interactive terminal | everything | yes |

## Setting up keys

Every key is an encrypted Ed25519 key. Its passphrase comes from
`$OPENADAPT_JIT_KEY_PASSPHRASE`, so it stays out of shell history. Flow
refuses an unencrypted key.

```bash
export OPENADAPT_JIT_KEY_PASSPHRASE=...        # from your secret store
openadapt-flow jit keygen --out ci.pem         # prints the public key
openadapt-flow jit register --role admitter_key --principal ci:release \
    --public-key <hex>                          # a person, at a terminal
```

The roles are `policy_signer` (a person who signs admission policy),
`admitter_key` (a CI job or script) and `agent` (an agent that admits under
a policy). The registry is `principals.json` in the key directory
(`$OPENADAPT_JIT_KEY_DIR` or `~/.openadapt/jit`). One key holds one role. A
policy signer's name can't be an agent or model name.

## An agent under a standing policy

A person writes an admission policy and signs it with their registered
policy-signer key:

```json
{
  "policy_id": "notes-v1",
  "signer": "dana.signer",
  "signed_at": "2026-10-17T12:00:00Z",
  "expires_at": "2026-11-17T12:00:00Z",
  "allowed_hosts": ["ehr-test.example.org"],
  "allowed_actions": ["click", "type", "key"],
  "allowed_effects": ["record_written", "field_equals"],
  "required_readback": "independent_readback",
  "min_clean_canaries": 3,
  "agent_principals": ["agent:admitter"],
  "flow_policies": ["clinical-write"]
}
```

```bash
openadapt-flow jit sign-policy policy.json --key dana.pem --out signed-policy.json
```

The agent then runs canaries and admits, with its own key:

```python
from openadapt_flow.jit.authority import (
    SignedAdmissionPolicy, load_encrypted_key, principal_from_key,
)

agent = principal_from_key(
    load_encrypted_key("agent.pem"),
    key_dir,
    policy=SignedAdmissionPolicy.load("signed-policy.json"),
)
for _ in range(3):
    jit.canary(principal_id=agent, note="test note")
decision = jit.admit_with_key(agent)  # {"outcome": "admitted" | "needs_person"}
```

You can also admit from a shell: `openadapt-flow jit admit PROGRAM --key
agent.pem --policy signed-policy.json`. It exits 0 when the draft is
admitted and 3 when it needs a person.

Flow checks these at each step itself. It doesn't take the caller's word for
any of them:

- **When the agent's principal is built and again at admission:** the
  policy's signature against the registered signer's key, that the policy
  hasn't expired and isn't dated in the future, and that it names this
  agent.
- **Before each canary runs, and at admission:** the draft is in scope. The
  app's host, every step's action and every declared effect kind must be
  allowed, and so must the draft's Flow risk policy if the admission policy
  lists any.
- **At admission:** at least `min_clean_canaries` canary runs for this
  bundle digest. Each must be signed by the local admission key, end
  `VERIFIED` with zero model calls, and have an independent (tier 1)
  read-back on every write step. None may have failed.
- **At admission:** the checks every mode makes (below).

A draft outside the policy is not admitted and does not fail. Flow writes
`needs_person.json` next to it, and `openadapt-flow jit review --queue
CACHE_DIR` lists it. That includes a draft with a failed canary or a
screen-only read-back. A person can admit it the usual way. Too few canaries,
an expired or unsigned policy, or an unregistered key is refused outright.

The manifest records the agent principal (`admitted_by_principal_id`), the
policy's id, digest, signer and expiry, the signed policy itself, the number
of clean canaries, and the agent's signature over the manifest
(`admitter_signature`).

## A CI job with an admitter key

```bash
OPENADAPT_JIT_KEY_PASSPHRASE=... openadapt-flow jit admit PROGRAM --key ci.pem
```

The principal is the name the key is registered under; `--principal` is
refused with `--key`. There is no `--yes`. The job needs a passing canary
for this digest, run by the same key (`jit.canary(principal_id=ci)`) or by a
person. It doesn't need a step review: the person who registered the key
stands behind what it admits. The manifest records the key's principal and
its signature.

## A person at a terminal

`openadapt-flow jit review` and `jit admit --principal NAME` need an
interactive terminal and a typed `ok`. The steps are in the table under
[The steps](#the-steps). This is the only mode with a step review.

## What every mode keeps

- **A draft never runs unattended.** `run()` returns `needs_human_admit`
  and executes nothing. The bundle carries a `jit_draft.json` marker.
  `Replayer.run`, which every execution path goes through, refuses a marked
  bundle without a valid admission, including a copy of it. That covers
  `openadapt-flow replay` and `run`, `serve-execute` (refused before any
  effect), the server that `emit-mcp` writes, and your own code that calls
  `Replayer.run`. `emit-skill` and `emit-mcp` still write their output for a
  draft, labelled DRAFT. A canary is the only run a draft gets. It is
  attended by a person, a registered admitter key, or a registered agent
  under a valid policy.
- **A model never authorizes.** A plain name is treated as a person's name.
  `agent`, `model`, `bot`, blank, or a name starting with `agent:`,
  `model:`, `bot:`, `llm:`, `service:` and similar can't confirm, review,
  attend, admit or reject. An agent acts only through a registered key and
  under a policy a person signed. The admission decision is made by Flow's
  checks, not by the agent or a model.
- **A run can't approve its own contract.** The identity that made the
  recording (`recorded_by`, the dialect, or the agent's `principal_id`)
  can't attend a canary or admit. This holds even when it is a registered
  agent named in the policy. Verification checks it again.
- **`VERIFIED` still needs an independent read-back.** Admission in every
  mode needs a tier 1 read-back on every write step.
- **The evidence is signed.** Canary records are signed with the local
  admission key, and admission ignores any that aren't. An agent that can
  write the cache directory can't add a clean canary. Verification
  re-checks a key or policy admission against the registry, so removing an
  agent's key from `principals.json` revokes what it admitted.

### Limits

- Flow checks keys and names, not people. Code that can call `admit()` with
  a person's name can admit, and the Python methods don't check for a
  terminal.
- The registry, the local admission key and the canary signing key all live
  in the key directory. Whoever can write it can register a key or re-sign
  an admission. Run agents without access to the key directory. If an agent
  can read it, it can sign canary records too.
- An admitted bundle keeps running after its policy expires. Expiry stops
  new admissions.

## The steps

These are the steps for a person. Key and policy admissions skip the review
and make the other checks. Every record below is pinned to the bundle's content digest. If the bundle
changes, earlier reviews and canaries no longer count.

| Step | Call | Refuses unless |
| --- | --- | --- |
| Confirm parameters | `confirm_params(principal_id, accept_params, confirm="ok")` | `accept_params` is exactly the task's parameters, and no review exists yet. It recompiles the bundle and returns the new step ids. Skip it when the task has no parameters. |
| Review | `review(principal_id, reviewed_steps, confirm="ok")` | The parameters were confirmed for this digest, and `reviewed_steps` is every step of the program, in order. |
| Canary | `canary(principal_id, **params)` | The review is for this digest, and every write step has an independent read-back through the attached verifier. The run must end `VERIFIED` with zero model calls. |
| Admit | `admit(principal_id, confirm="ok")` | A review and a signed, passing canary exist for this digest, no effect is a placeholder, and the program passes its policy. With `require_distinct_reviewer_and_admitter=True` on `compile_agent` (off by default) or `--require-distinct`, the reviewer can't also admit. |

### Independent read-back

A write step is any step `run_gate.is_consequential` flags. Each effect on a
write step is labeled:

- `independent_readback`: the contract is not an on-screen read, and the
  verifier reads the system of record (an API, database, FHIR or file read;
  tier 1).
- `screen_readback`: the contract or the verifier's read goes through the
  application's screens, in the acting session or in a separate signed-in
  session (tier 2). This does not count for admission, although a run can
  still end `VERIFIED` on a tier 2 read.
- `placeholder` or `unverified`: nothing to read, or a verifier with no
  declared tier.

A canary does not run while any write step lacks an `independent_readback`,
so nothing is written to the target app that can't be checked.

### The canary

The canary runs the reviewed program once against the target app and really
writes to it. Use a test record or a sandbox copy of the app. A canary that
fails goes to the exception inbox. The agent is never called after a halt.

### The admission manifest

Every mode writes `admission.json` into the bundle:

- `admission_mode`: `person`, `admitter_key` or `agent_policy`
- `admitted_by_principal_id` and `admitted_at` (UTC)
- `reviewed_by_principal_id`, `reviewed_at` and the reviewed step ids
  (empty when no person reviewed)
- for a key or an agent: `admitter_public_key` and `admitter_signature`;
  for an agent, also the policy fields listed above
- the canary's principal, time, target origin, outcome and model call count
- the read-back label for every write step
- the bundle digest, task, parameters, policy, and the model key's source
  and fingerprint (never the key)
- `signature`: Ed25519 over the rest, with the local admission key

The key lives in `$OPENADAPT_JIT_KEY_DIR` or `~/.openadapt/jit`, outside the
cache directory, so write access to a bundle is not enough to re-sign it.
Anyone who can write the key directory can. Runs verify against the public
key in that directory.

### What a run checks

Before an admitted program runs, it must still match its admission: the
signature verifies, the bundle's digest is the admitted digest, the task
signature and policy are the admitted ones, the app's origin is the origin
the canary ran on, and the verifier gives every write step an independent
read-back. Any difference refuses the run, and nothing executes.

## The customer's model key

The recording run is the only step that calls a model, and your agent loop
makes those calls with your key. `load_customer_model_key(provider)` reads
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, or a JSON key file you name with
`config_path`. Nothing else is tried. If your key is missing, it raises
`MissingModelKey` and the recording does not start. OpenAdapt holds no model
key to fall back to. The draft records where the key came from and a
fingerprint.

## Before you record

Before the browser opens, Flow prints this warning to stderr. An
`on_warning` callback also gets it, for a UI; the callback can't replace the
print:

> Before you record: the recording keeps every screen the agent sees and
> every value it types, and the trace may contain sensitive data such as
> patient or customer records, names, and account numbers. Use a sandbox or
> test environment and test data when you can.

The draft keeps the text and where it was shown. Flow can't tell whether
anyone read it.
