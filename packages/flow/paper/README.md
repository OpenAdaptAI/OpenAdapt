# OpenAdapt arXiv paper

This directory contains the source for the OpenAdapt technical paper. It is a
submission draft, not a submitted paper. The byline is currently
Richard Abrich, OpenAdapt (MLDSAI Inc.); the final author list and order are
confirmed as a submission gate below.

## Submission blockers

- Confirm the final author list, order, ORCIDs, and affiliations (byline
  currently set to Richard Abrich, OpenAdapt / MLDSAI Inc.).
- Choose an arXiv category and complete any required endorsement.
- Record the commit and release artifact used for the final evaluation.
- Re-run every promoted experiment from the release candidate and archive the
  raw outputs.
- Add repeated longitudinal and cross-environment trials before making claims
  beyond the bounded studies currently reported.
- Complete a disclosure review for screenshots, logs, and third-party app data.

## Build

Requirements: Python 3.10+, `latexmk`, and a TeX distribution with `booktabs`,
`microtype`, `hyperref`, `amssymb`, `tikz`, and `pgfplots` (Debian/Ubuntu:
`texlive-latex-extra texlive-pictures texlive-science`).

```bash
python paper/check_artifacts.py
make -C paper
```

`make -C paper` builds two PDFs from the same gate-checked constants:

- `paper/build/main.pdf` — the full technical report (canonical artifact).
- `paper/workshop/build/main.pdf` — an ~8-page workshop condensation
  (`paper/workshop/main.tex`) reframed around the silent-wrong-effect finding.
  It shares `references.bib` via a byte-identical copy (a regular file, not a
  symlink, so the sdist packages cleanly) and the same benchmark constants;
  `check_artifacts.py` binds both and asserts the two bib files stay identical.
  Retarget its document class when a specific workshop venue is chosen.

`make -C paper clean` removes generated files.

## Evidence contract

`check_artifacts.py` binds each headline number to a released raw result or a
bounded aggregate summary. It does not re-run the applications or imply that
all underlying evaluation rows are public. Grown corpora, deployment-derived
tuning, target-specific recipes, and raw private evaluation rows stay outside
the public release. The reproducibility section and `ARTIFACT_CHECKLIST.md`
distinguish source-backed, CI-reproducible, field, fixture, and one-run
descriptive evidence.
