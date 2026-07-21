# arXiv and artifact checklist

## Paper metadata

- [ ] Final title approved.
- [ ] Author names, order, ORCIDs, affiliations, and corresponding author approved.
- [ ] arXiv category and license selected.
- [ ] Funding, conflict-of-interest, and model/provider disclosures completed.
- [ ] Third-party product names and screenshots reviewed.

## Release identity

- [ ] Paper records an immutable Git commit and release tag.
- [ ] Wheel/container digests are archived.
- [ ] OS, browser, Python, dependencies, hardware, model, and provider API
      versions are captured per experiment.
- [ ] Evaluation scripts run from a clean checkout.

## Task and oracle

- [ ] Each task states environment, initial state, action budget, parameters,
      external oracle, and teardown.
- [ ] Comparative conditions use the same task oracle.
- [ ] Each comparative task has at least three trials per condition.
- [ ] Natural drift and injected drift are labeled separately.
- [ ] Fixture, analog, public-demo, and real-application evidence are labeled.

## Metrics

- [ ] Task success and latency reported with run counts.
- [ ] Model calls, tokens, and recorded API cost reported.
- [ ] Authoring and maintenance effort reported separately from runtime cost.
- [ ] Silent incorrect success reported against an external oracle.
- [ ] Safe halt, over-halt, false abort, and recovery time reported.
- [ ] Per-task outcomes are published; aggregation does not hide failures.

## Safety and privacy

- [ ] Identity-armed coverage reported for each consequential workflow.
- [ ] Effect declarations and verifier configuration disclosed.
- [ ] System-of-record oracle distinguished from same-screen confirmation.
- [ ] Optional model rescue and its false-rescue risk disclosed.
- [ ] Raw artifacts reviewed for credentials, PHI, PII, and licensing.
- [ ] Shared artifacts are sanitized derivatives with manifest and approved hash.

## Reproduction

- [ ] `python paper/check_artifacts.py` passes.
- [ ] `make -C paper` builds without errors.
- [ ] CI paper workflow passes on the release commit.
- [ ] Raw JSON/JSONL and aggregate tables agree.
- [ ] OpenEMR shared-demo caveat is retained.
- [ ] Known limits link resolves to the release-pinned document.

## Evidence still missing for broad claims

- [ ] Longitudinal trials over weeks or months.
- [ ] Representative enterprise workflow sample.
- [ ] Real Citrix/ICA/HDX validation.
- [ ] Multi-environment Windows evidence.
- [ ] Production-scale hosted isolation and recovery evidence.
- [ ] Independent replication.
