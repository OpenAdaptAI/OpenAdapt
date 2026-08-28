# OpenAdapt arXiv paper

Source for the OpenAdapt technical paper and its workshop condensation. This is
a submission draft, not a submitted paper.

## Which paper is this?

Six papers build out of two source trees, and every one of them lands in a
file named `main.pdf`. Name the profile and the output path, never just
"the paper".

**This tree, `openadapt-flow/paper/`, holds two of them:**

| Paper | Source | Built to |
|---|---|---|
| "Compile Once, Govern Every Repair" — the SYSTEMS paper about the engine: compilation, deterministic replay, governed repair. | `paper/main.tex` | `paper/build/main.pdf` |
| "A Green Screen Is Not a Saved Record" — a workshop condensation of the above. **Superseded; do not submit it.** Last touched 2026-07-27, and it is not anonymous. "Measuring the Checkers" took the venue it was aimed at. | `paper/workshop/main.tex` | `paper/workshop/build/main.pdf` |

**`openadapt-attest-bench` `paper/` (private) holds the other four**, the
MEASUREMENT lineage: "Measuring the Checkers" in a workshop and an arXiv
profile, "Admissible by Construction", and the extended "Silent Wrong
Actions in Computer-Use Agents". That tree is canonical for all four; its
`paper/README.md` opens with the map. The live workshop submission is its
`paper/workshop/main.tex`, not anything here and not its `paper/main.tex`.

**`openadapt-internal` `docs/workshop-draft-verify-agents-2026-08-25/`
(private) is a superseded frozen copy**, not a source. Its files carry a
`SUPERSEDED-` prefix. Do not build or upload from it.

The systems paper and the measurement papers must not be merged: different
claims, different audiences. The measurement lineage uses this engine as its
instrument and cites it. This tree does not cite the measurement lineage back;
`references.bib` holds none of it. Say "cites" in one direction only until
that changes.

`PAPERS.md` in the workspace root carries the portfolio view: who reads each
paper, where it is published, and whether it may go out yet.

Everything mechanically checkable is done: `python paper/check_artifacts.py`
passes, `make -C paper` builds both PDFs with zero LaTeX warnings, and
`sh paper/make_arxiv_tarball.sh` produces a verified submission tarball.
`paper/ARTIFACT_CHECKLIST.md` records the evidence item by item.

---

## Founder decisions (the only things left before submission)

These five require a human decision and cannot be discharged by an agent. Every
other checklist item is either closed with evidence or is an open *evidence*
gap that requires new experiments, not a decision.

1. **Authorship — DECIDED 2026-08-13.** Solo: `Richard Abrich, OpenAdapt.AI
   (MLDSAI Inc.), richard@openadapt.ai`, ORCID `0000-0002-9556-4491` (now in
   `main.tex`). Revisit a co-author only for the ICML merge, and only for a
   substantive contributor.
2. **arXiv primary category, cross-lists, and endorsement.** Recommendation and
   justification below. arXiv requires endorsement for a first submission to a
   category; check your account before the deadline you care about, because
   obtaining endorsement is not instant.
3. **arXiv licence — DECIDED 2026-08-13.** CC BY 4.0, per the
   recommendation below.
4. **Funding and conflict-of-interest statement — DECIDED 2026-08-13.**
   Added to `main.tex`: page-1 footnote plus an unnumbered section before the
   bibliography. Original note: the paper previously had none.
   The author develops the evaluated system; `06_limitations.tex` already says
   so in the threats-to-validity paragraph, but a formal COI/funding statement
   is a separate, venue-facing decision (footnote on page 1 vs. an unnumbered
   section before the bibliography).
5. **Venue intent — DECIDED 2026-08-13.** Target: NeurIPS 2026 workshop
   "Who Verifies the Agents?" (verify-agents-workshop.github.io; verified
   non-archival, dual-submission welcome, 4-9 pages, deadline 29 Aug 2026
   AoE, notification 29 Sep, Sydney 11-12 Dec; in-person travel approved).
   The condensation leads with the pre-registered SWAR measurement
   (openadapt-attest-bench) with the engine as apparatus. Retarget
   `paper/workshop/main.tex` to the workshop's required style when the
   condensation is rewritten; the full report stays arXiv-formatted.

### Recommended arXiv categories

- **Primary: `cs.SE` (Software Engineering).** The contribution is a systems and
  evaluation one: a runtime that treats an out-of-band system-of-record read as a
  first-class execution gate, and a reliability metric (silent incorrect success
  jointly with over-halt) for automation. Its intellectual lineage is the
  test-oracle problem, runtime verification, the end-to-end argument, and
  transactional idempotency hazards. Both adversarial reviews independently
  concluded the paper has little novel machine learning, so a machine-learning
  primary would put it in front of the wrong reviewers.
- **Cross-list `cs.PL` (Programming Languages).** Justified, not decorative: the
  compiler performs inductive program synthesis from demonstrations with explicit
  quarantine of underdetermined induction, and the paper argues in Related Work
  and again in the Conclusion that a sound out-of-band effect oracle removes the
  binding constraint on generate-and-check for programs whose correctness is
  defined by an effect on the world. That is an argument aimed squarely at the
  synthesis community, and it is positioned against PROSE/FlashMeta,
  version-space algebra, CEGIS, SyGuS, and WebRobot.
- **Cross-list `cs.HC` (Human-Computer Interaction).** The demonstration-recording
  loop, the one-shot operator confirm for inferred parameters, the interactive
  disambiguation of underdetermined induction, and the human-only gates in the
  repair promotion lifecycle are HCI contributions, and the PBD lineage
  (SUGILITE, Rousillon, DiLogics) lives here.
- **Optional cross-list `cs.AI`.** Only if you want the computer-use-agent
  audience to see it. The paper measures against a computer-use agent baseline
  and argues that task-success benchmarks mismeasure reliability, which is a
  message for that community — but the paper contributes no model or training
  method, so this is reach, not fit.

### Recommended licence

**CC BY 4.0.** The engine is MIT and the argument depends on being quotable and
reusable; a non-commercial or no-derivatives licence would conflict with the
open-core positioning and would prevent the failure taxonomy and metric
definitions from being adopted by others, which is the stated goal. Avoid
`arXiv.org perpetual, non-exclusive license` if you want third parties to be
able to reuse the taxonomy directly.

---

## Build

Requirements: Python 3.10+, `latexmk`, and a TeX distribution with `booktabs`,
`microtype`, `hyperref`, `amssymb`, `array`, `tikz`, and `pgfplots`
(Debian/Ubuntu: `texlive-latex-extra texlive-pictures texlive-science`).

```bash
python paper/check_artifacts.py
make -C paper
```

`make -C paper` gate-checks every headline constant and then builds two PDFs
from the same constants:

- `paper/build/main.pdf` — the full technical report (canonical artifact), 21
  pages including the bibliography.
- `paper/workshop/build/main.pdf` — the workshop condensation
  (`paper/workshop/main.tex`), 6 pages, reframed around the silent-wrong-effect
  finding. It shares `references.bib` via a byte-identical copy (a regular file,
  not a symlink, so the sdist packages cleanly) and the same benchmark
  constants; `check_artifacts.py` binds both and asserts the two bib files stay
  identical.

A clean checkout builds both with **zero LaTeX warnings** — no overfull or
underfull boxes, no undefined citations, no undefined references. Treat any new
warning as a defect.

`make -C paper clean` removes generated files (`build/`, `workshop/build/`,
`dist/`).

## arXiv submission

```bash
make -C paper arxiv            # -> paper/dist/arxiv-main.tar.gz
make -C paper arxiv-workshop   # -> paper/dist/arxiv-workshop.tar.gz
```

`paper/make_arxiv_tarball.sh` is fail-loud. It gate-checks the constants, does a
clean build to produce `main.bbl` (arXiv does not reliably run `bibtex`, so the
`.bbl` is shipped and `references.bib` is not), stages only the files the
document actually `\input`s, recompiles that staged tree standalone in a scratch
directory, refuses to emit a tarball if any citation or reference is undefined,
strips auxiliary files, and prints the tarball's SHA-256 and the source commit.

Submission steps, in order:

1. Close the five founder decisions above. Set the byline and `pdfauthor` in
   `paper/main.tex` and `paper/workshop/main.tex` before building.
2. Confirm the paper CI workflow is green on the exact commit you intend to
   submit (`.github/workflows/paper.yml`). Do not assume a green run from an
   earlier commit still holds.
3. `make -C paper clean && make -C paper arxiv`. Record the printed SHA-256 and
   source commit in `paper/ARTIFACT_CHECKLIST.md` under "Release identity".
4. At <https://arxiv.org/submit>, start a new submission and upload
   `paper/dist/arxiv-main.tar.gz`. Do **not** upload a PDF; arXiv compiles the
   source.
5. Select the primary category and cross-lists (recommendation above). If you
   lack endorsement for the primary category, request it before proceeding — a
   submission held for endorsement is not queued.
6. Choose the licence (recommendation above).
7. Paste the abstract. It must match `paper/main.tex` exactly, minus LaTeX
   markup: arXiv's abstract field is plain text, so replace `\emph{...}` with the
   bare words and `---` with an em dash. Do not paraphrase; the abstract carries
   machine-checked constants.
8. Verify arXiv's generated PDF page-for-page against `paper/build/main.pdf`
   before announcing. arXiv's TeX Live version may differ from yours; the tarball
   is deliberately package-light (`booktabs`, `microtype`, `hyperref`, `amssymb`, `array`,
   `tikz`, `pgfplots`, `xcolor`, `lmodern`, `geometry`) to
   reduce that risk, and `pgfplots` is pinned with `\pgfplotsset{compat=1.13}`.
9. After announcement, record the arXiv identifier in
   `paper/ARTIFACT_CHECKLIST.md` and cite it from the repository README.

## Evidence contract

`check_artifacts.py` binds each headline number to a released raw result or a
bounded aggregate summary. It does not re-run the applications and does not imply
that all underlying evaluation rows are public. Grown corpora,
deployment-derived tuning, target-specific recipes, and raw private evaluation
rows stay outside the public release. The reproducibility section and
`ARTIFACT_CHECKLIST.md` distinguish source-backed, CI-reproducible, field,
fixture, and one-run descriptive evidence.

The check establishes **transcription fidelity only** — that the prose faithfully
cites the released evidence. It cannot distinguish a sound measurement from an
unsound one, and the paper says so in its own contributions list. It additionally
enforces two negative constraints: the retired, circular in-process
`silent_wrong_action` result may not appear in any paper prose, and the
N=0-external-deployments and Citrix-stand-in disclosures may not be removed.

## Review record

`REVIEW_ADVERSARIAL.md` and `REVIEW_ADVERSARIAL_2.md` are the two adversarial
peer reviews the current draft was hardened against. They are retained
deliberately: they are the record of what was wrong and what was fixed, and they
are review-only deliverables that have not been submitted anywhere.
