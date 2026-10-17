# Admitting a compiled CUA run

A bundle compiled from a computer-use agent run starts as a `DRAFT`. It
becomes `ADMITTED` only when a named person takes it through three steps. It
never goes back: a changed program is a new recording and a new draft. The
code is in [`openadapt_flow/jit/lifecycle.py`](../openadapt_flow/jit/lifecycle.py).
For the wrapper itself, see [JIT.md](JIT.md).

```
DRAFT --review--> (reviewed) --canary--> (canary passed) --admit--> ADMITTED
  \--reject--> REJECTED
```

## What a draft can't do

- Run unattended. `run()` returns `needs_human_admit` and executes nothing.
  The governed runner refuses a draft unless a named principal attends it,
  and the only attended run is the canary.
- Approve itself. No principal named `agent`, `model`, or anything starting
  with `agent:`, `model:`, `cua:` or `llm:` can review, attend, admit or
  reject. Neither can the identity that made the recording: its
  `recorded_by`, its dialect, or the agent's own `principal_id`.

Flow checks identities, not people. It records the principal it is given and
refuses the ones it knows are not people. Your own sign-in or approval tool
decides who may call these steps.

## The steps

Every record below is pinned to the bundle's content digest. If the bundle
changes, earlier reviews and canaries no longer count.

| Step | Call | Refuses unless |
| --- | --- | --- |
| Review | `review(principal_id, reviewed_steps, accept_params, confirm="ok")` | `accept_params` is exactly the task's parameters, and `reviewed_steps` is every step of the program, in order. Confirming the parameters recompiles the bundle first, so the review is of the program that will run. |
| Canary | `canary(principal_id, **params)` | The review is for this digest, and every write step has an independent read-back through the attached verifier. The run must end `VERIFIED` with zero model calls. |
| Admit | `admit(principal_id, confirm="ok")` | A review and a passing canary exist for this digest, no effect is a placeholder, and the program passes its policy. |

### Independent read-back

A write step is any step `run_gate.is_consequential` flags. Each effect on a
write step is labeled:

- `independent_readback`: the contract is not an on-screen read, and the
  verifier reads it outside the acting session (a system-of-record read, or
  a separate signed-in session).
- `screen_readback`: the contract or the verifier's read goes through the
  acting screen. This does not count.
- `placeholder` or `unverified`: nothing to read, or a verifier with no
  declared tier.

A canary does not run while any write step lacks an `independent_readback`,
so nothing is written to the target app that can't be checked.

### The canary

The canary runs the reviewed program once against the target app and really
writes to it. Use a test record or a sandbox copy of the app. A canary that
fails goes to the exception inbox. The agent is never called after a halt.

### The admission manifest

`admit` writes `admission.json` into the bundle:

- `admitted_by_principal_id` and `admitted_at` (UTC)
- `reviewed_by_principal_id`, `reviewed_at` and the reviewed step ids
- the canary's principal, time, target origin, outcome and model call count
- the read-back label for every write step
- the bundle digest, task, parameters, policy, and the model key's source
  and fingerprint (never the key)
- `signature`: Ed25519 over the rest, with the local admission key

The key lives in `$OPENADAPT_JIT_KEY_DIR` or `~/.openadapt/jit`, outside the
cache directory, so someone who can edit a bundle can't re-sign it. Runs
verify against that key's public half.

## The customer's model key

The recording run is the only step that calls a model, and your agent loop
makes those calls with your key. `load_customer_model_key(provider)` reads
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, or a JSON key file you name with
`config_path`. Nothing else is tried. If your key is missing, it raises
`MissingModelKey` and the recording does not start. OpenAdapt holds no model
key to fall back to. The draft records where the key came from and a
fingerprint.

## Before you record

Before the browser opens, Flow shows this warning (to stderr by default, or
to your `on_warning` callback):

> Before you record: the recording keeps every screen the agent sees and
> every value it types, and the trace may contain sensitive data such as
> patient or customer records, names, and account numbers. Use a sandbox or
> test environment and test data when you can.

The draft keeps the text it showed.
