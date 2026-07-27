# arXiv and artifact checklist

This is the submission gate referenced by `paper/sections/07_reproducibility.tex`.
Items marked **[x]** are discharged with the evidence recorded inline. Items
marked **[ ]** are the genuinely founder-dependent decisions; they are collected
at the top of `paper/README.md` under "Founder decisions".

Re-run `python paper/check_artifacts.py` after any edit to the paper or to a
benchmark artifact. It is a transcription-fidelity gate, not a validity gate.

## Paper metadata

- [x] Final title approved: *Compile Once, Govern Every Repair: Deterministic
      Replay for Repeated GUI Work* (workshop condensation: *A Green Screen Is
      Not a Saved Record*). Set in `paper/main.tex` `\title` and in
      `pdftitle`; the two are asserted identical to the `hypersetup` value.
- [ ] Author names, order, ORCIDs, affiliations, corresponding author. **FOUNDER
      DECISION.** Byline is currently `Richard Abrich, OpenAdapt.AI (MLDSAI
      Inc.), richard@openadapt.ai` in `paper/main.tex:36`.
- [ ] arXiv primary category, cross-lists, and endorsement. **FOUNDER
      DECISION.** Recommendation and justification are in `paper/README.md`.
- [ ] arXiv licence selection. **FOUNDER DECISION.** Recommendation in
      `paper/README.md`.
- [ ] Funding, conflict-of-interest, and model/provider disclosures. **FOUNDER
      DECISION.** The provider disclosure content is already factual and in the
      paper (`04_methodology.tex`: `claude-sonnet-5`, `computer_20251124`,
      `computer-use-2025-11-24`); what remains is the funding/COI statement and
      whether it is a footnote or an unnumbered section.
- [x] Third-party product names and screenshots reviewed. The paper contains no
      screenshots. Third-party names appearing in the text are OpenEMR (public
      demonstration instance), Citrix / ICA / HDX, Microsoft Windows UI
      Automation, Apple macOS / TextEdit, Parallels, Aardwolf, Playwright, and
      Anthropic model identifiers — each used nominatively to identify the
      environment or dependency actually measured, with no logo, mark, or
      endorsement implied. No customer or partner is named anywhere in the
      paper; see the workspace rule against customer/personal names in public
      artifacts.

## Release identity

- [x] Immutable Git commit: `d5ce14fff52f4bf5a0897280c0141b6526d469ae`
      (`OpenAdaptAI/openadapt-flow`, branch `main`). This is the base commit of
      the paper branch; every benchmark artifact cited by
      `check_artifacts.py` is read from this tree.
- [x] Release tag: `v1.24.0` (`openadapt-flow` 1.24.0, the release current at
      this commit; `pyproject.toml` `version = "1.24.0"`).
- [x] Wheel/sdist digests archived (verified against the PyPI JSON API, not
      copied from a changelog):
      - `openadapt_flow-1.24.0-py3-none-any.whl`
        SHA-256 `170fdac154794292c99dc6eea6486e7a2c3fdf321bcd87976d924bccd3db4aef`
      - `openadapt_flow-1.24.0.tar.gz`
        SHA-256 `2d4702e5ccbdfed0f78063ca510dc82894a2833edbed71d77edffbd0ffebd67d`
      - Declared licence `MIT`; `requires_python >=3.10,<3.13`; neither archive
        is yanked.
- [x] Environment captured per experiment, recorded inside each artifact rather
      than in prose:
      - Comparative studies (`benchmark/openemr/results.json`,
        `benchmark/results.json`): platform `macOS-15.7.3-arm64-arm-64bit`,
        model `claude-sonnet-5`, tool `computer_20251124`, beta header
        `computer-use-2025-11-24`, generated `2026-07-08`. `check_artifacts.py`
        asserts both arms share all four fields and that the paper discloses
        each one verbatim.
      - End-to-end effect study (`benchmark/effect_e2e/results.json`): platform
        `macOS-15.7.3-arm64-arm-64bit`, generated `2026-07-21`,
        `model_calls: 0`, `n_per_scenario: 9`.
      - Windows UIA: Windows 11 ARM VM, named snapshot, matrix key
        `20260717-candidate-56759c8-v2`.
      - Native macOS: macOS 15.7.3 arm64 host, TextEdit, batch
        `textedit_counted_3plus1_b1b61a5_20260717`.
      - Network RDP: Aardwolf 0.2.14 into a 1280x800 Parallels Windows 11
        snapshot, artifact `results_82a658a_20260718.sanitized.json`.
      - Citrix stand-in (`benchmark/citrix_ica_hdx/results.json`): fully
        in-process, no Docker/network/Playwright, generated `2026-07-26`.
- [x] Evaluation scripts run from a clean checkout: `python
      paper/check_artifacts.py` and `make -C paper` were run from a fresh
      worktree created from `origin/main` with no build directory present.
      The deterministic harnesses (`benchmark/effect_e2e/run.py`,
      `benchmark/fault_model/run.py`, `benchmark/lending_fault_model/`,
      `benchmark/ap_invoice/run.py`, `benchmark/o2c_recon/run.py`,
      `benchmark/citrix_ica_hdx/run_ica_hdx_qualification.py`) are localhost-only
      and make no model calls. The comparative and substrate studies are not
      clean-checkout reproducible and are labelled Field, not CI-reproducible.

## Task and oracle

- [x] Each reported study states environment, run count, and external oracle.
      `paper/sections/07_reproducibility.tex` Table `tab:evidence` is the index;
      `paper/sections/04_methodology.tex` gives the per-study protocol.
- [x] Oracle definitions, stated once per study, each independent of the path
      that carried the write:

      | Study | Oracle |
      |---|---|
      | End-to-end effect (`effect_e2e`) | Benchmark judge opens the on-disk SQLite system of record on its own read-only connection, over every table, fingerprints canonical typed schema and row content before/after, and classifies without seeing the write's HTTP success flag |
      | Transactional fault model (`fault_model`) | Database state at the persistence boundary, not the UI report |
      | Lending second domain (`lending_fault_model`) | Separate read-only connection to the MockLoan SQLite ledger; business tables discovered from SQLite's catalog; benchmark-local canonical typed row classification |
      | AP invoice (`ap_invoice`) | Direct per-table delta audit across two SQLite systems of record plus the OUTBOX maildir read from disk |
      | O2C reconciliation (`o2c_recon`) | Direct per-table delta audit across two SQLite systems of record plus re-reading the results CSV from disk |
      | OpenEMR comparison | Task-level success check, identical for both arms |
      | MockMed comparison | Task-level success check, identical for both arms |
      | Public-web breadth | External goal oracle, one run per application |
      | Identity ladder | Intended-entity ground truth over adversarial homonyms |
      | Windows UIA | Independent SQLite query against the application's row state |
      | Native macOS | Exact expected file bytes |
      | Network RDP | Guest-tools file readback, a channel independent of the RDP display |
      | Citrix stand-in | In-process out-of-band store read directly; never the on-screen banner |

- [x] Comparative conditions use the same task oracle and the same vision-only
      Playwright backend; asserted by `check_artifacts.py` against the source
      results files.
- [x] At least three trials per condition for every comparative or qualification
      claim: comparative arms are n=20/10 and n=100/20; substrate rows are 3/3;
      end-to-end effect is 9 per scenario per arm; lending is 3 per task per arm;
      multi-application is 3 per scenario per arm. The two exceptions are stated
      as single observations in the text itself: the drift-repair illustration
      (n=1 per arm) and the public-web corpus (n=1 per application).
- [x] Natural drift and injected drift labelled separately. All reported drift is
      injected (theme re-render, fault injection); no natural-drift study is
      claimed, and `06_limitations.tex` states that no published study covers
      weeks of natural drift.
- [x] Fixture, field, and descriptive evidence labelled. Four labels are defined
      in `07_reproducibility.tex` and applied per row in `tab:evidence`.

## Metrics

- [x] Task success and latency reported with run counts (`tab:comparison`, and
      per-trial RDP latencies bound by `check_artifacts.py`).
- [x] Model calls and recorded API cost reported. Compiled arms record zero model
      calls (`comparison.json` `model_calls_compiled: 0`, asserted); agent costs
      are derived from recorded token usage and the list-price schedule stored in
      each raw result, and `04_methodology.tex` says so.
- [x] Authoring and maintenance effort reported separately from runtime cost:
      `06_limitations.tex` states explicitly that the cost values exclude
      recording, compilation, operator review, exception handling,
      infrastructure, support, and maintenance.
- [x] Silent incorrect success reported against an external oracle, in every
      study that has one.
- [x] Safe halt, over-halt, and false abort reported. False aborts are reported
      as a constant 9 of 90 across all three end-to-end arms; over-halt is
      reported for the identity ladder (including the 100% pixel-only figure),
      the lending study, and the multi-application benchmarks. Recovery time is
      reported only for the drift-repair illustration and is labelled n=1.
- [x] Per-task outcomes published; aggregation does not hide failures. The
      per-scenario end-to-end table (`tab:e2eclasses`) and the
      per-scenario multi-application table (`tab:multiapp`) publish every cell,
      including the cells where an arm slips, and `check_artifacts.py` binds each
      published cell to its artifact.

## Safety and privacy

- [x] Identity-armed coverage: `03_governance.tex` states that identity
      guarantees apply only to armed steps, that real bundles can contain unarmed
      clicks, and that coverage is reported per workflow.
- [x] Effect declarations and verifier configuration disclosed, including the
      concession that effects are authored per deployment and the compiler does
      not infer them.
- [x] System-of-record oracle distinguished from same-screen confirmation, in the
      main text (`03_governance.tex`) and again in `06_limitations.tex`, with the
      pixel-only-substrate unavailability case stated explicitly.
- [x] Optional model rescue and its false-rescue risk disclosed
      (`06_limitations.tex`).
- [x] Raw artifacts reviewed for credentials, PHI, PII, and licensing. Every
      released system-of-record fixture is synthetic (MockMed, MockLoan, the
      AP-invoice and O2C fixtures, the Citrix stand-in); the RDP artifact cited is
      the `.sanitized.json` derivative; the macOS artifact is a hash-bound offline
      adjudication. The public-web study releases only its bounded aggregate.
      Stated in `07_reproducibility.tex` under "Data availability and ethics".
- [x] Shared artifacts are sanitized derivatives with manifest and approved hash;
      the mechanism is described in `03_governance.tex` and the boundary is
      restated in `07_reproducibility.tex`.
- [x] No copied AGPL benchmark material is referenced by, or shipped with, the
      paper package. The paper mentions no openIMIS reference-environment file,
      and the arXiv tarball stages only `.tex` and `.bbl`.

## Reproduction

- [x] `python paper/check_artifacts.py` passes from a clean worktree.
- [x] `make -C paper` builds both PDFs with zero LaTeX warnings: no overfull or
      underfull boxes, no undefined citations, no undefined references.
      `paper/build/main.pdf` is 21 pages; `paper/workshop/build/main.pdf` is 6
      pages.
- [x] `sh paper/make_arxiv_tarball.sh` produces a submission tarball and
      independently re-compiles the staged tree, failing loudly on any undefined
      citation or reference.
- [ ] CI paper workflow passes on the final release commit. **Re-verify on the
      exact submission commit** — `.github/workflows/paper.yml` runs the gate on
      every change under `paper/`; the current branch's run is the evidence, but
      a later engine merge must not be assumed to preserve it.
- [x] Raw JSON and aggregate tables agree: `check_artifacts.py` asserts the
      comparison artifact matches `benchmark/openemr/results.json` and
      `benchmark/results.json` field-by-field for every arm, so a hand-edited
      aggregate cannot pass.
- [x] OpenEMR shared-demo caveat retained in `04_methodology.tex`,
      `06_limitations.tex`, and `07_reproducibility.tex`.
- [x] The retired, circular in-process `silent_wrong_action` study appears in no
      paper prose. `check_artifacts.py` still pins its constants so the retired
      artifact cannot be silently edited, and separately fails the build if
      `50 of 90`, `50/90`, `silently accepted 50`, or `silent_wrong_action`
      appears in any paper source file.

## Evidence still missing for broad claims

These are deliberately open. Each is stated as a limitation in the paper rather
than hedged away, and none is a founder decision — each requires new evidence.

- [ ] Longitudinal trials over weeks or months of natural interface drift.
- [ ] A representative enterprise workflow sample rather than authored fixtures.
- [ ] Real Citrix ICA/HDX validation. The reported Citrix campaign is a
      deterministic synthetic stand-in; `benchmark/citrix_ica_hdx/results.json`
      records `is_real_ica_hdx: false`, `ica_hdx_accepted: false`, and
      `status_manifest.json` reports `real_protocol_environment_evidence:
      pending`.
- [ ] Multi-environment Windows evidence (one VM and one workflow today).
- [ ] Production-scale hosted isolation and recovery evidence.
- [ ] Independent replication by a third party, and any external deployment at
      all: **N = 0 external organizations have deployed this system.** This is
      stated first and without hedging in `06_limitations.tex`, and
      `check_artifacts.py` fails the build if that disclosure is removed from
      either the abstract or the limitations section.
- [ ] A study that exercises real GUI perception *and* effect verification on the
      same runs. Today the browser-and-OCR fault study carries only a screen-only
      arm, and the effect-verified studies stub the observation backend and
      vision stack. The paper says so in three places rather than presenting one
      study as though it covered both.
- [ ] A third-party system scored on EffectBench. Both reference baselines are
      OpenAdapt's own arms, so fair cross-system ranking is a design property,
      not a demonstrated one.
