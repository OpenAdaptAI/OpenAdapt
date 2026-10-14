"""Gold write specs for ExtraDup.

Each spec is one CREATE. Mutants that change cardinality or add a field
are gold FAIL. Duplicate-CREATE is killed by ``|new(M)| = |spec(M)|``,
not by field-value inclusion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class WriteSpec:
    """One intended CREATE against a system of record.

    ``expected_new`` is ``|spec(M)|``. ``allowed_fields`` is every key a
    legitimate write may persist, including store-assigned identity. A field
    outside this set is Extra-Field.

    ``identity_fields`` names the keys that say WHICH record the write must
    land on. They are the contract's ``oracle_identity``. Every other spec
    field is content. Separating the two is what lets an oracle answer "the
    right content in the wrong chart", which a content check cannot.

    ``decoy_identity`` is a different record in the same collection. The
    ``wrong_record`` operator writes the correct content there.
    """

    env: str
    collection: str
    fields: Mapping[str, str]
    expected_new: int
    allowed_fields: frozenset[str]
    extra_field: str
    extra_value: str
    omit_field: str
    identity_fields: frozenset[str]
    decoy_identity: Mapping[str, str]


# MockMed triage-save, the same synthetic encounter the Flow fault server
# persists at GET /api/db. All values are fake.
MOCKMED_GOLD = WriteSpec(
    env="mockmed",
    collection="encounters",
    fields={
        "patient_id": "p1",
        "type": "Triage",
        "note": "Follow-up in 2 weeks; BP recheck.",
    },
    expected_new=1,
    allowed_fields=frozenset(
        {"id", "patient_id", "type", "note", "source", "key"}
    ),
    extra_field="priority",
    extra_value="stat",
    omit_field="note",
    identity_fields=frozenset({"patient_id"}),
    decoy_identity={"patient_id": "p0"},
)


# OpenEMR-shaped local patient CREATE. Mirrors the ``patient_data`` row
# the pinned openemr_local fixture writes; this kit does not start Docker.
OPENEMR_GOLD = WriteSpec(
    env="openemr_local",
    collection="patient_data",
    fields={
        "pubpid": "MRN-0042",
        "fname": "Taylor",
        "lname": "Duplicate",
    },
    expected_new=1,
    allowed_fields=frozenset(
        {"id", "pid", "pubpid", "fname", "lname", "source"}
    ),
    extra_field="occupation",
    extra_value="Hardware",
    omit_field="lname",
    identity_fields=frozenset({"pubpid"}),
    decoy_identity={"pubpid": "MRN-0000"},
)


def identity_of(spec: WriteSpec) -> dict[str, str]:
    """The record the write must land on: ``oracle_identity`` for this spec."""
    missing = set(spec.identity_fields) - set(spec.fields)
    if missing:
        raise KeyError(
            f"spec {spec.env}/{spec.collection} names identity field(s) "
            f"{sorted(missing)} it does not carry"
        )
    return {key: spec.fields[key] for key in sorted(spec.identity_fields)}


def decoy_of(spec: WriteSpec) -> dict[str, str]:
    """A different record in the same collection. Never the spec's own."""
    decoy = {key: spec.decoy_identity[key] for key in sorted(spec.identity_fields)}
    if decoy == identity_of(spec):
        raise ValueError(
            f"decoy identity {decoy} equals the spec identity; the wrong-record "
            "operator would be a no-op"
        )
    return decoy


GOLD_SPECS: tuple[WriteSpec, ...] = (MOCKMED_GOLD, OPENEMR_GOLD)
