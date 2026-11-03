"""A baseline read back from JSON keeps each record's identity.

``verify_run`` stores the pre-action baseline with ``model_dump(mode="json")``.
Postgres, MSSQL and Oracle drivers return UUID, Decimal and datetime values,
which come back from JSON as strings. If identity compared the raw values,
every row that already existed would count as new, and ``count_new_only``
would confirm a write that never happened.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow.runtime.effects._common import new_records_only
from openadapt_flow.runtime.effects.effect import EffectState, stable_id

_values = st.one_of(
    st.uuids(),
    st.decimals(allow_nan=False, allow_infinity=False, places=2),
    st.datetimes(timezones=st.just(timezone.utc)),
    st.datetimes(),
    st.dates(),
    st.integers(),
    st.text(max_size=8),
)


def _round_trip(state: EffectState) -> EffectState:
    return EffectState.model_validate(state.model_dump(mode="json"))


@settings(max_examples=300, deadline=None)
@given(
    record=st.dictionaries(
        st.sampled_from(["id", "patient", "amount", "created_at"]),
        _values,
        min_size=1,
    )
)
def test_identity_survives_the_json_baseline(record: dict) -> None:
    before = _round_trip(EffectState(substrate="sql", reachable=True, records=[record]))
    assert stable_id(before.records[0]) == stable_id(record)
    assert new_records_only([record], before) == []


def test_an_unchanged_uuid_table_has_no_new_rows() -> None:
    row = {
        "id": uuid.UUID("6f0f5a8e-3b0a-4c39-9a51-0d3b2d0c8a11"),
        "patient_id": "P1",
        "amount": Decimal("125.00"),
        "visit_date": date(2026, 10, 20),
        "created_at": datetime(2026, 10, 20, 9, 30, tzinfo=timezone.utc),
    }
    before = _round_trip(EffectState(substrate="sql", reachable=True, records=[row]))
    assert new_records_only([dict(row)], before) == []
    assert new_records_only([dict(row, id=uuid.uuid4())], before) != []


def test_bytes_without_a_json_form_still_have_an_identity() -> None:
    assert stable_id({"blob": b"\xff\xfe"}) == stable_id({"blob": b"\xff\xfe"})
