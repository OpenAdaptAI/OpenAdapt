"""A summary row that shows several values ("Name (DOB)") as an effect location.

Before this change a pack could not read such a row as two values: the keys
ended ``not_checked``, or a pack checked a derived string built from both.
Now each key gets its own verdict. Synthetic snapshots only.
"""

from __future__ import annotations

from typing import Any

import pytest

from openadapt_flow.form_tx.judge import _row_verdicts
from openadapt_flow.form_tx.row_location import row_readback_results
from openadapt_flow.form_tx.types import (
    DossierFact,
    FieldAnchorSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormPack,
    FormSnapshot,
    ObservedTextPair,
    ProvenanceKind,
    ReadbackFieldVerdict,
    ReadbackStatus,
    SummaryCheckSpec,
    SummaryRowPartSpec,
    SummaryRowReadbackSpec,
)

ROW = SummaryRowReadbackSpec(
    anchor=FieldAnchorSpec(label_patterns=("^Travellers$",)),
    pattern=r"(?P<n>.+) \((?P<d>[^)]*)\)",
    parts=(
        SummaryRowPartSpec(group="n", dossier_key="t_name"),
        SummaryRowPartSpec(
            group="d",
            dossier_key="t_dob",
            codec=FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="MDY"),
        ),
    ),
)
INTAKE = {"t_name": "Ada Byron", "t_dob": "01/31/1990"}


def _snap(*pairs: tuple[str, str]) -> FormSnapshot:
    return FormSnapshot(
        url_origin_and_path="http://portal.test/done",
        text_pairs=tuple(ObservedTextPair(label=a, value=b) for a, b in pairs),
    )


def _facts(values: dict[str, Any]) -> dict[str, DossierFact]:
    return {
        k: DossierFact(
            key=k,
            value=v,
            provenance=ProvenanceKind.INTAKE_DOCUMENT,
            source_id="intake",
        )
        for k, v in values.items()
    }


def _statuses(shown: FormSnapshot, intake: dict[str, Any] = INTAKE) -> dict[str, str]:
    got = row_readback_results((ROW,), shown, _facts(intake), run_id="r")
    return {k: v[0][0] for k, v in got.items()}


def test_each_value_in_the_row_is_checked() -> None:
    assert _statuses(_snap(("Travellers", "Ada Byron (01/31/1990)"))) == {
        "t_name": "held",
        "t_dob": "held",
    }


def test_a_wrong_date_refutes_only_the_date() -> None:
    assert _statuses(_snap(("Travellers", "Ada Byron (01/30/1990)"))) == {
        "t_name": "held",
        "t_dob": "refuted",
    }


def test_a_wrong_name_refutes_only_the_name() -> None:
    assert _statuses(_snap(("Travellers", "Ada (01/31/1990)"))) == {
        "t_name": "refuted",
        "t_dob": "held",
    }


@pytest.mark.parametrize(
    "shown",
    [
        _snap(),
        _snap(("Travellers", "Ada Byron, 01/31/1990")),
        _snap(("Travellers", "Ada Byron (01/31/1990)"), ("Travellers", "x (1/1/2000)")),
    ],
    ids=["not shown", "another format", "shown twice"],
)
def test_what_cannot_be_read_is_not_checked(shown: FormSnapshot) -> None:
    assert set(_statuses(shown).values()) == {"not_checked"}


def test_a_part_the_codec_cannot_read_is_not_checked_alone() -> None:
    # No month names are given, so a spelled month is not read; the name is.
    got = _statuses(_snap(("Travellers", "Ada Byron (31 January 1990)")))
    assert got == {"t_name": "held", "t_dob": "not_checked"}


def test_an_intake_value_the_codec_cannot_read_is_not_checked() -> None:
    got = _statuses(
        _snap(("Travellers", "Ada Byron (01/31/1990)")), {**INTAKE, "t_dob": "soon"}
    )
    assert got == {"t_name": "held", "t_dob": "not_checked"}


def test_an_unported_codec_leaves_the_row_not_checked() -> None:
    row = ROW.model_copy(
        update={
            "parts": (
                SummaryRowPartSpec(
                    group="n",
                    dossier_key="t_name",
                    codec=FieldCodecSpec(kind=FormCodecKind.TITLE_CASE),
                ),
                ROW.parts[1],
            )
        }
    )
    got = row_readback_results(
        (row,),
        _snap(("Travellers", "Ada Byron (01/31/1990)")),
        _facts(INTAKE),
        run_id="r",
    )
    assert {k: v[0][0] for k, v in got.items()} == {
        "t_name": "not_checked",
        "t_dob": "not_checked",
    }


def test_a_shown_value_for_a_key_with_no_intake_value_is_refuted() -> None:
    # Like a field read-back: the summary must not show a value the run never
    # had (a portal prefill).
    got = _statuses(
        _snap(("Travellers", "Ada Byron (01/31/1990)")), {"t_name": "Ada Byron"}
    )
    assert got == {"t_name": "held", "t_dob": "refuted"}
    empty = _statuses(_snap(("Travellers", "Ada Byron ()")), {"t_name": "Ada Byron"})
    assert empty == {"t_name": "held"}


def _verdict_args() -> tuple[Any, Any]:
    def nc(key: str, prov: ProvenanceKind, reason: str) -> ReadbackFieldVerdict:
        return ReadbackFieldVerdict(
            dossier_key=key,
            codec_kind=FormCodecKind.EXACT,
            passed=False,
            status=ReadbackStatus.NOT_CHECKED,
            provenance=prov,
            reason=reason,
        )

    def failed(
        key: str, kind: FormCodecKind, prov: ProvenanceKind, reason: str
    ) -> ReadbackFieldVerdict:
        return ReadbackFieldVerdict(
            dossier_key=key,
            codec_kind=kind,
            passed=False,
            status=ReadbackStatus.FAILED,
            provenance=prov,
            reason=reason,
        )

    return nc, failed


def test_the_judge_turns_results_into_form_tx_verdicts() -> None:
    nc, failed = _verdict_args()
    verdicts = _row_verdicts(
        (ROW,),
        _snap(("Travellers", "Ada Byron (01/30/1990)")),
        _facts(INTAKE),
        "r",
        nc,
        failed,
    )
    by_key = {v.dossier_key: v for v in verdicts}
    assert by_key["t_name"].status == ReadbackStatus.PASSED
    assert by_key["t_dob"].status == ReadbackStatus.FAILED
    assert by_key["t_dob"].codec_kind == FormCodecKind.ISO_DATE
    assert _row_verdicts((), _snap(), {}, "r", nc, failed) == []


def test_a_pack_without_rows_dumps_and_hashes_as_before() -> None:
    from openadapt_flow.form_tx.types import canonical_sha256

    check = SummaryCheckSpec()
    assert "row_readbacks" not in check.model_dump(mode="json")
    pack = FormPack(
        pack_id="p", domain_schema="d", ui_profile_id="u", summary_check=check
    )
    dumped = pack.model_dump(mode="json")
    assert "row_readbacks" not in dumped["summary_check"]
    assert pack.digest() == canonical_sha256(dumped)
    with_rows = SummaryCheckSpec(row_readbacks=(ROW,))
    assert with_rows.model_dump(mode="json")["row_readbacks"]
    assert (
        SummaryCheckSpec.model_validate(with_rows.model_dump(mode="json")) == with_rows
    )


def test_a_bad_row_pattern_is_refused() -> None:
    with pytest.raises(ValueError):
        ROW.model_copy().model_validate({**ROW.model_dump(), "pattern": "("})
