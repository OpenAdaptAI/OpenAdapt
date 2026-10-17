# Capability, qualification, and machine-checked claims

## Two admissions answer two different questions

A release admission and a workflow admission answer different questions. The
release admission covers the exact product release. The workflow admission
covers one exact sealed bundle, application, environment, runtime, input,
identity, effect, and policy contract.

Flow enters Production only through an active signed, expiring, and revocable
release admission. If the admission is missing, expired, revoked, or bound to a
different release, Flow is **not actively admitted**. Read the
[current admission-derived status](https://openadapt.ai/status.json) and the
[capability and qualification matrix](PRODUCT_STATUS.md).

## What is qualified today

The browser path runs record, compile, policy-check, deterministic
replay, refusal, and report generation in CI. Windows UIA, native macOS, native
Linux, and RDP each have retained 3/3 accepted task evidence with independent
effects or oracles. Citrix has a dedicated exact-window backend and a retained
3+3 code-readiness record; an exact ICA/HDX deployment receives its own counted
qualification record rather than inheriting RDP or stand-in evidence. Each new
third-party application is similarly qualified against its controls and effect
oracle. The workflow-program IR adds parameters, branches, loops, effect
verification, and governed recovery on the same runtime. `DESIGN.md` has the
module contracts;
[`design/WORKFLOW_PROGRAM_IR.md`](design/WORKFLOW_PROGRAM_IR.md)
describes the program IR, and [`L1_INTEGRATION.md`](L1_INTEGRATION.md)
covers feeding layered clinical-data platforms.

The integrated status of the engine, browser, desktop, remote-display, safety,
GUI, hosted, and deployment surfaces is published in
[`PRODUCT_STATUS.md`](PRODUCT_STATUS.md). Security reviewers should
start with [`ENTERPRISE_ARCHITECTURE.md`](ENTERPRISE_ARCHITECTURE.md),
which maps screenshot/credential flows, cryptographic guarantees, hosted
boundaries, and unmet controls.

## Machine-checked claims

Product claims are enforced by CI. Every registered claim is tiered and mapped
to the specific tests and benchmark artifacts that back it in
[`../claims.yaml`](../claims.yaml). CI runs `scripts/validate_claims.py`, which
**fails the build whenever a claim's tier outranks its strongest evidence** and
regenerates [`VERIFICATION.md`](VERIFICATION.md), the claim-by-claim
verification report, from the registry, so the adjectives in the README cannot
quietly rot.
