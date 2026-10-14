"""ExtraDup: this trajectory is wrong; does your checker notice?

Public kit against MockMed and an OpenEMR-shaped local store. Does not
vendor AppWorld evaluation.py, does not unseal HOLDOUT, does not patch
WorkArena.
"""

from openadapt_evals.extradup.checkers import (
    content_only_check,
    identity_check,
    records_under,
)
from openadapt_evals.extradup.gold import (
    MOCKMED_GOLD,
    OPENEMR_GOLD,
    WriteSpec,
    decoy_of,
    identity_of,
)
from openadapt_evals.extradup.mutations import (
    ALL_OPERATORS,
    EVAL_ONLY_OPERATORS,
    OPERATORS,
    apply,
)
from openadapt_evals.extradup.seal import REFUSED, VERIFIED, seal_verdict
from openadapt_evals.extradup.suite import (
    SUITE_LABEL,
    SUITE_NAME,
    CellReport,
    check_invariants,
    run_cell,
)

__all__ = [
    "ALL_OPERATORS",
    "EVAL_ONLY_OPERATORS",
    "CellReport",
    "MOCKMED_GOLD",
    "OPENEMR_GOLD",
    "OPERATORS",
    "REFUSED",
    "SUITE_LABEL",
    "SUITE_NAME",
    "VERIFIED",
    "WriteSpec",
    "apply",
    "check_invariants",
    "content_only_check",
    "decoy_of",
    "identity_check",
    "identity_of",
    "records_under",
    "run_cell",
    "seal_verdict",
]
