"""connector.status_from_report (C7): a report that says "check the record"
must not reach the control plane as a plain failure.

The cases come from the vocabulary, not from this file: every
``transaction_outcome`` row, with each ``execution_outcome`` the row allows,
at exit codes 0 and 1. When the precise outcome's plain result is
``check_the_record``, the connector status must map to that result too.
On main a report with ``transaction_outcome: RECONCILIATION_REQUIRED`` and
``execution_outcome: FAILED`` maps to ``failed`` ("Didn't finish"): the
precise outcome is never read. Owned by the connector status work
(FAIL_CLOSED.md follow-up 3, the class of #2300).
"""

from __future__ import annotations

from typing import Any

from openadapt_flow.connector.executor import status_from_report
from tests.failopen.harness import CodeCase, Seed, SiteSpec, register
from tests.failopen.sites._vocab import result_of, rows


def _cases() -> list[CodeCase]:
    out = []
    for tx, row in rows("transaction_outcome").items():
        for eo in row.get("execution_outcomes", []):
            for rc in (0, 1):
                must = row["result"]
                out.append(
                    CodeCase(
                        evidence={
                            "rc": rc,
                            "report": {
                                "execution_outcome": eo,
                                "transaction_outcome": tx,
                                "success": rc == 0 and tx == "VERIFIED",
                            },
                        },
                        may_have_changed=bool(row["may_have_changed"]),
                        forbidden=lambda status, must=must: (
                            must == "check_the_record"
                            and result_of("connector_status", status) != must
                        ),
                        label=f"{tx}/{eo} rc={rc}",
                    )
                )
    out.append(
        CodeCase(
            evidence={
                "rc": 0,
                "report": {"execution_outcome": "NOT_A_CODE", "success": True},
            },
            may_have_changed=True,
            forbidden=lambda status: False,
            label="unknown execution_outcome",
            unknown=True,
        )
    )
    return out


def _run(ev: dict[str, Any]) -> Any:
    return status_from_report(ev["rc"], ev["report"])


register(
    SiteSpec(
        id="connector.status_from_report",
        classes="C7",
        target="openadapt_flow.connector.executor:status_from_report",
        kind="owned",
        run=_run,
        is_pass=lambda out: out == "success",
        seeds=lambda: [
            Seed(
                {"rc": 0, "report": {"execution_outcome": "VERIFIED", "success": True}},
                True,
            ),
            Seed({"rc": 1, "report": {"execution_outcome": "FAILED"}}, False),
        ],
        codes=_cases,
    ),
    xfail=(
        "a report whose transaction_outcome says check the record maps to "
        "'failed'; needs the connector status from the vocabulary "
        "(FAIL_CLOSED.md follow-up 3, class of #2300)"
    ),
    known=[
        "M12 P7: seed 0: RECONCILIATION_REQUIRED/FAILED rc=0: may have changed, reported 'failed'",
        "M12 P7: seed 0: RECONCILIATION_REQUIRED/FAILED rc=1: may have changed, reported 'failed'",
    ],
)
