"""Triage of verified saves for human review.

This package orders a list's verified saves for a person and proposes one
batch approval for the clean ones. It never approves, resumes or certifies
anything, and nothing on the run path imports it. See
``docs/DECISION_TRIAGE.md``.

Modules:

- :mod:`.minimize`: values to match booleans; the provider boundary check.
- :mod:`.features`: deterministic features for one save.
- :mod:`.rules`: the v0 clean rule, which needs no labels.
- :mod:`.provider`: the provider protocol and its request and answer types.
- :mod:`.feature_model`: the local logistic-regression provider.
- :mod:`.calibration`: calibration, thresholds and the pinned artifact.
- :mod:`.labels`: review decisions as labels.
- :mod:`.triage`: the plan.
- :mod:`.review`: a person's answers to a plan.
"""
