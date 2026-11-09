# Governed run authorization

`openadapt-flow run` evaluates deployment admission before it delegates to the
shared deterministic `Replayer`. Admission and execution must be one contract:
an admitted identity requirement cannot become a warning at replay, and an
explicitly approved unverified write cannot disappear before the action.

## Design

A successful run gate creates an ephemeral `GovernedRunAuthorization` bound to:

- the sealed bundle content digest;
- the exact effective parameter and worklist digest;
- the policy that admitted the run;
- every step that must receive an affirmative live identity verdict;
- the exact unresolved effect-contract hashes approved without an independent
  verifier;
- a unique authorization id and an honest approval-source label.

The replayer recomputes in-memory semantics and verifies sealed assets
immediately before touching the backend. A bundle, input, step, or
effect-contract mismatch halts before action, and a fresh authorization is
single-use within the process. Durable pause/resume persists the exact
authorization and frozen worklists, then revalidates them as a continuation.
Plaintext sealed assets are retained as a verified in-memory snapshot; every
target, predicate, identity, and postcondition consumer also detects later disk
replacement and halts rather than reading substituted bytes.
For required identity steps, `unreadable` and `abstain` halt even when the
action is reversible. Program exception edges cannot catch these governed
safety halts and convert them into success. An approved GUI write may execute
without a system-of-record verifier, but a non-vacuous screen postcondition is
required and the result is recorded as
`effect_approved_unverified=true`, never `effect_verified=true`.

Durable ledgers retain approved-but-unverified effect hashes separately from
independently confirmed effects. Both prevent duplicate re-execution, but only
confirmed effects are reported or revalidated as confirmed.

Direct API writes cannot use this fallback. Without a verifier, an API response
is not an independent effect oracle and there is no GUI postcondition floor, so
admission refuses.

The local `--approve-unverified-writes` flag proves only that the local
invocation explicitly accepted the risk. It does **not** authenticate a human.
A hosted control plane should set `approval_source` to its authenticated,
run-scoped approval reference.

## Options considered

1. **Remove the approval fallback.** Smallest and safest, but it makes
   consequential legacy workflows unusable when the application exposes no
   independent read API.
2. **Pass a boolean bypass into replay.** Fast, but reusable and not bound to a
   bundle, step, or effect contract. A changed workflow could inherit approval,
   and the audit trail could not establish what was accepted.
3. **Bind a narrow capability to the admitted workflow.** Slightly more code,
   but preserves the existing fallback without weakening other writes. This is
   the implemented option.
4. **Persist a signed approval and pause at each write.** Stronger identity and
   non-repudiation for remote operation, but adds key management, expiry,
   revocation, and resume UX. The current capability shape is the migration
   target for that hosted layer.

## Basis

- [NIST SP 800-53 Rev. 5](https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final)
  separates access enforcement from audit accountability. The implementation
  likewise enforces an exact capability and records the event source, affected
  steps, outcome, and contract hashes.
- [NIST AI RMF 1.0](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-ai-rmf-10)
  emphasizes documented human-AI roles, measurement, and governance over the
  system lifecycle. The local source label avoids overstating authentication,
  while the hosted design has a clear place for an authenticated reference.
- [NASA's formal runtime-assurance framework](https://ntrs.nasa.gov/citations/20230017350)
  and the [SEI Simplex architecture](https://www.sei.cmu.edu/library/an-architectural-description-of-the-simplex-architecture/)
  motivate a trusted runtime decision layer that retains control when an
  unverified component cannot establish a required property. Here, a missing
  identity verdict selects halt; approval is a separately bounded operating
  mode rather than a silent downgrade.

## Evidence

Run the focused policy-handoff probe:

```bash
python -m openadapt_flow.validation.governed_run \
  --out benchmark/governed_run
```

The committed artifact uses three deterministic trials per condition and
reports correct action, silent wrong action, safe halt, and over-halt. It is a
synthetic authorization-semantic test, not evidence of application or OCR
reliability.
