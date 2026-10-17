# Qualification terms

This page defines the words `openadapt-flow qualify` uses. Other pages link
here instead of repeating them.

## Qualify

Qualifying a workflow means running the compiled bundle against the normal
case and a set of deliberate faults before it may run unattended on a real
system. It passes only when:

- the normal case completes, and the write is confirmed by reading it back
  from the system of record (the application's API or database), not from the
  screen;
- every fault case halts before anything is written.

The faults are:

| Case | What changes | Must |
|---|---|---|
| `representative` | nothing | complete, verified by read-back |
| `fault-ambiguity` | a second copy of the save button appears | halt |
| `fault-wrong-identity` | the save button is relabeled to a different action | halt |
| `fault-stale-identity` | the screen goes blank at the save | halt |
| `fault-weak-effect` | the write check is swapped for one that only reads the screen | halt |
| `fault-missing-effect` | the write check is removed | halt |

Each case runs several times (three by default). After each trial an
independent oracle reads the system of record and checks that the normal case
wrote exactly one correct record and that no fault case wrote anything.

**When you need it:** before a workflow makes consequential or irreversible
writes (creating a patient, submitting a claim, sending a payment) without a
person watching. It is optional for read-only tasks.

**What you get:** a signed report (`qualification/report.json`) and a bundle
whose `workflow.json` carries the qualification project and its signed case
results. With certification (below), the runner admits that bundle for
unattended runs.

**What it does not prove:** anything beyond the environment it was qualified
in. The report records the application version, browser, viewport and
runtime. A different application version, a changed screen, or a different
system of record needs a new qualification. A pass also does not show the
workflow handles inputs or screens that the cases did not exercise.

## Certify

Certifying adds one step to qualifying: the result is checked against a
certification policy and signed under it. `openadapt-flow qualify ...
--certify clinical-write` refuses to certify unless all six cases ran and
passed and the bundle satisfies every rule of the policy. Nothing is marked
certified when it refuses; the refusal reasons are in
`qualification/verdict.txt`.

What is signed is `qualification/result.json`
(`openadapt.qualification-result/v1`, RFC-0002): the coverage report without
free text, the digest of the read-back contract it was judged against, and
one read-back verdict per trial. Its digest goes into the existing
qualification admission envelope; there is no separate signing scheme.

The keys `qualify` signs with are local test keys. Case results are signed
with runner and fault-driver keys made for one campaign and held in memory.
The admission envelope is signed with Flow's well-known local-dev
qualification key, which every production trust map refuses. None of them is
a production trust root; a production deployment admits results signed by its
own authority (see [PRODUCTION.md](PRODUCTION.md)).

## Certification policy

A certification policy is a named set of rules a bundle must meet before it is
certified. The rules live in a YAML file and are enforced by
`openadapt_flow/policy.py`. They cover:

- **identity**: which steps must check that the target on screen is the right
  one before acting (`require_identity_for`, `prohibit_unarmed_clicks`);
- **effect verification**: which steps must confirm their write in the system
  of record, not only on screen (`require_system_effects_for`,
  `require_effects_for_irreversible`, `require_effect_verification_for`), and
  which need an idempotency key so a retry cannot write twice
  (`require_idempotency_key_for`);
- **screen checks**: which steps must also check the screen afterwards
  (`require_screen_postconditions_for`, `prohibit_vacuous_postconditions`,
  `max_unverified_steps`);
- **operator review**: effect bindings the compiler could not derive must be
  confirmed by a person (`prohibit_unconfirmed_effect_bindings`), and steps
  found on thin evidence need sign-off
  (`require_human_approval_below_confidence`).

Which faults must halt, the minimum effect tier and the trial count are not
policy rules today. `qualify` always requires all six cases for
certification, sets the minimum effect tier to the tier of the read-back you
configure (a REST or SQL read-back is the strongest, "independent system"),
and runs `campaign.trials` trials per case (three by default).

### Built-in policies

| Policy | Requires | Use it for |
|---|---|---|
| `clinical-write` | identity on every navigation and write step; no unchecked clicks; a system-of-record check and an idempotency key on every write; screen checks on write, save, submit, create, update, delete, confirm and send steps; no step without a check; no unconfirmed effect bindings; sign-off for steps found with confidence below 0.8 | unattended writes to a system of record: EHR, claims, billing, anything where a wrong or duplicate write is costly |
| `permissive` | only that write steps carry some check of their outcome | development, CI smoke tests, read-only tasks |

The `qualify propose` and `qualify from-demo` commands also take a
`--policy-pack` that bundles a policy with a minimum effect tier and signer:

| Pack | Policy | Minimum effect tier | Signer |
|---|---|---|---|
| `community` | `permissive` | persisted-state reacquisition | local dev signer allowed |
| `cloud` | `clinical-write` | independent session | production signer |
| `regulated` | `clinical-write` | independent system | production signer |

### How to choose

Use `clinical-write` whenever the workflow writes to a system of record and
will run without a person approving each write. Use `permissive` while you are
building the workflow, or for tasks that only read.

### Your own policy

You can write your own. `--certify` takes a built-in name or a path to a YAML
file with the same keys; unknown keys are rejected. Start from a copy of
`openadapt_flow/policies/clinical-write.yaml`, and tighten rather than loosen
it for production writes.

See also: [Qualify a workflow](QUALIFY_A_WORKFLOW.md),
[QUALIFICATION_PROJECT.md](QUALIFICATION_PROJECT.md).
