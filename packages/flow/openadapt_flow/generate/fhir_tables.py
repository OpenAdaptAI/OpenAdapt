"""Reviewed FHIR R4 knowledge the ensure-record generator applies.

Nothing here is generated or learned from one application. Each table is
standard-level knowledge (the FHIR R4 specification, ISO/USPS code lists)
that a person reviewed once; the generator only decides *where* in a
demonstrated record a table applies. A table entry never removes a check:
it says how a check compares, or names a value a correct record may hold.
"""

from __future__ import annotations

#: List elements are told apart by these keys, in this order. A
#: ContactPoint by ``system`` (a Patient keeps its phone and its email in one
#: ``telecom`` list); a HumanName, Address or Identifier by ``use`` or
#: ``system``.
DISCRIMINATORS: tuple[str, ...] = ("system", "use")

#: ContactPoint ``value`` comparison by ``system``. A phone number keeps its
#: digits whatever the punctuation; an email address is compared casefolded,
#: as mail systems treat it.
CONTACT_COMPARE: dict[str, str] = {
    "phone": "digits",
    "fax": "digits",
    "sms": "digits",
    "pager": "digits",
    "email": "casefold",
}

#: USPS state and territory codes, name -> code. Applied to an Address
#: ``state`` element when every demonstrated record stores one of the codes:
#: a task names the state, the record holds its code. The map applies to the
#: task's side only, so a record that stores the name still reads as wrong.
US_STATES: dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME",
    "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "American Samoa": "AS", "Guam": "GU", "Northern Mariana Islands": "MP",
    "Puerto Rico": "PR", "U.S. Virgin Islands": "VI",
}  # fmt: skip

#: Elements whose value, when set, equals what the specification says their
#: absence means (``meaningWhenMissing``), so setting it changes nothing a
#: reader of the record sees. A FHIR client commonly sends ``active: true`` on
#: a create. Only the stated value is allowed; ``active: false`` stays a
#: wrong field.
DEFAULT_MEANING: dict[str, dict[str, object]] = {
    # Patient.active: "generally assumed to be active if no data is provided"
    "Patient": {"active": True},
    "Practitioner": {"active": True},
    "Organization": {"active": True},
}

#: Reviewed identity templates, by resource type: each rule is a set of
#: element paths that together make "the same one". A rule applies when the
#: demonstrations bind every one of its paths to a task parameter. Patient is
#: ``patient-identity/1`` (#1516's record check): first name, last name and
#: birth date, or the email address. A type with no template gets a
#: proposed rule that a person must confirm (see ``ensure_contract``).
IDENTITY_TEMPLATES: dict[str, list[list[str]]] = {
    "Patient": [
        ["name.given", "name.family", "birthDate"],
        ["telecom[system=email].value"],
    ],
}

#: Search parameter types a candidate search may use for an identity value.
SEARCHABLE_TYPES: frozenset[str] = frozenset(
    {"string", "token", "reference", "date", "uri", "number"}
)
