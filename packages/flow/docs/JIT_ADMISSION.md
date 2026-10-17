# Admitting a compiled CUA run

A bundle compiled from a computer-use agent run starts as a `DRAFT`. It
becomes `ADMITTED` only when a named person takes it through the steps
below. It never goes back: a changed program is a new recording and a new
draft. The
code is in [`openadapt_flow/jit/lifecycle.py`](../openadapt_flow/jit/lifecycle.py).
For the wrapper itself, see [JIT.md](JIT.md).

```
DRAFT --confirm_params--> --review--> --canary--> --admit--> ADMITTED
  \--reject--> REJECTED
```

## What a draft can't do

- Run unattended. `run()` returns `needs_human_admit` and executes nothing.
  The governed runner refuses a draft unless a named principal attends it,
  and the only attended run is the canary. The bundle carries a
  `jit_draft.json` marker, and `openadapt-flow replay` and `run` refuse a
  marked bundle without a valid admission, including a copy of it.
- Approve itself. A principal named `agent`, `model`, `bot`, or starting
  with `agent:`, `model:`, `bot:`, `llm:`, `service:` and similar can't
  confirm, review, attend, admit or reject. Neither can the identity that
  made the recording: its `recorded_by`, its dialect, or the agent's own
  `principal_id`.

Flow checks identities, not people. It records the principal it is given and
refuses the ones it knows are not people. The `openadapt-flow jit` commands
also need an interactive terminal and a typed `ok`. The Python methods don't
check for a terminal, so code that can call them can admit. Your own sign-in
or approval tool decides who may run that code.

## The steps

Every record below is pinned to the bundle's content digest. If the bundle
changes, earlier reviews and canaries no longer count.

| Step | Call | Refuses unless |
| --- | --- | --- |
| Confirm parameters | `confirm_params(principal_id, accept_params, confirm="ok")` | `accept_params` is exactly the task's parameters, and no review exists yet. It recompiles the bundle and returns the new step ids. Skip it when the task has no parameters. |
| Review | `review(principal_id, reviewed_steps, confirm="ok")` | The parameters were confirmed for this digest, and `reviewed_steps` is every step of the program, in order. |
| Canary | `canary(principal_id, **params)` | The review is for this digest, and every write step has an independent read-back through the attached verifier. The run must end `VERIFIED` with zero model calls. |
| Admit | `admit(principal_id, confirm="ok")` | A review and a passing canary exist for this digest, no effect is a placeholder, and the program passes its policy. With `require_distinct_reviewer_and_admitter=True` on `compile_agent` (off by default) or `--require-distinct`, the reviewer can't also admit. |

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

`admit` writes `admission.json` into the bundle:

- `admitted_by_principal_id` and `admitted_at` (UTC)
- `reviewed_by_principal_id`, `reviewed_at` and the reviewed step ids
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
