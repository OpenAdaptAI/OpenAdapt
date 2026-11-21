"""Velloway personal umbrella quote: pack written from the dev case only.

Seen in the dev-case demonstration (``demos/velloway.jsonl``): a sign-in
page, a start page with an optional account number, one long application
page (dates split into Day / Month / Year, an occupation type-ahead, Yes/No
and limit radio groups, an "Add a watercraft" button), a "Check your
answers" page with a confirmation checkbox and "Request quote", and a final
page listing "What we have on file" as a definition list.
"""

from __future__ import annotations

from typing import Any

from openadapt_flow.form_tx.types import (
    ControlKind,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormPack,
    PrefillPolicy,
    SummaryCheckSpec,
    UIProfileSpec,
    WizardScreenSpec,
)

CASE_PREFIX = "vw"

TEXT = FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE)
EXACT = FieldCodecSpec(kind=FormCodecKind.CASEFOLD)
INT = FieldCodecSpec(kind=FormCodecKind.INTEGER)
BOOL = FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)
MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")

_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)  # fmt: skip


_PLACEHOLDERS = FieldBindingSpec.model_fields["placeholder_values"].default


def _lab(
    key: str,
    label: str,
    kind: ControlKind = ControlKind.TEXT,
    codec: FieldCodecSpec = TEXT,
    *,
    section: str | None = None,
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"b_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",),
            section_patterns=(f"^{section}$",) if section else (),
        ),
        control_kind=kind,
        codec=codec,
        prefill_policy=PrefillPolicy.HALT_ON_CONFLICT,
        # Pack workaround: this portal's selects start on a "Choose" option,
        # which the frozen engine's placeholder list does not include.
        placeholder_values=(*_PLACEHOLDERS, "Choose"),
    )


def _shown(key: str, label: str, codec: FieldCodecSpec = TEXT) -> FieldBindingSpec:
    """A read-back of the final page's "What we have on file" list."""
    return FieldBindingSpec(
        binding_id=f"r_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",),
            section_patterns=("^What we have on file$",),
        ),
        codec=codec,
    )


PROFILE = UIProfileSpec(
    profile_id="heldout2_velloway",
    auth_wall_selectors=('input[type="password"]',),
)

_NONPROFIT = (
    r"Does anyone in your household serve as a director or officer of a "
    r"nonprofit organization\?"
)

PACK = FormPack(
    pack_id="heldout2.velloway.umbrella.v1",
    domain_schema="personal_lines.umbrella",
    ui_profile_id=PROFILE.profile_id,
    bindings=(
        _lab("applicant.first_name", "First name"),
        _lab("applicant.last_name", "Last name"),
        _lab("applicant.dob_day", "Day", codec=INT, section="Date of birth"),
        _lab("applicant.dob_month", "Month", codec=INT, section="Date of birth"),
        _lab("applicant.dob_year", "Year", codec=INT, section="Date of birth"),
        _lab("applicant.email", "Email address"),
        _lab("applicant.phone", "Phone number"),
        _lab("applicant.occupation", "Occupation", ControlKind.PORTAL_COMBOBOX, EXACT),
        _lab("home.address_line1", "Address line 1"),
        _lab("home.city", "Town or city"),
        _lab("home.state", "State", ControlKind.SELECT, EXACT),
        _lab("home.zip", "ZIP code"),
        _lab(
            "underlying.home_liability",
            "Home liability limit",
            ControlKind.SELECT,
            MONEY,
        ),
        _lab("household.rental_properties", "Rental properties you own", codec=INT),
        _lab("household.drivers", "Licensed drivers in your household", codec=INT),
        _lab("household.youthful_drivers", r"How many of them are under 25\?", codec=INT),
        _lab(
            "household.vehicles", "Vehicles your household owns or leases", codec=INT
        ),
        _lab(
            "underlying.auto_liability",
            "Auto liability limits",
            ControlKind.SELECT,
            EXACT,
        ),
        _lab("household.nonprofit_board", _NONPROFIT, ControlKind.RADIO_GROUP, BOOL),
        _lab("coverage.umbrella_limit", "Umbrella limit", ControlKind.RADIO_GROUP, MONEY),
        _lab("coverage.start_day", "Day", codec=INT, section="Policy start date"),
        _lab("coverage.start_month", "Month", codec=INT, section="Policy start date"),
        _lab("coverage.start_year", "Year", codec=INT, section="Policy start date"),
        _lab(
            "flow.confirm_details",
            "I confirm the details above are correct",
            ControlKind.CHECKBOX,
            BOOL,
        ),
    ),
    screens=(
        WizardScreenSpec(
            screen_id="vw_check",
            landmark_selector="#confirm",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=(r"^Request quote$",)),
        ),
        WizardScreenSpec(
            screen_id="vw_apply",
            landmark_selector="#first_name",
            next_button_anchor=FieldAnchorSpec(
                label_patterns=(r"^Continue to summary$",)
            ),
        ),
        WizardScreenSpec(
            screen_id="vw_start",
            landmark_selector="#account",
            next_button_anchor=FieldAnchorSpec(label_patterns=(r"^Continue$",)),
        ),
    ),
    summary_check=SummaryCheckSpec(
        field_readbacks=(
            _shown("applicant.first_name", "First name"),
            _shown("applicant.last_name", "Last name"),
            _shown("applicant.dob_long", "Date of birth"),
            _shown("applicant.email", "Email address", EXACT),
            _shown("applicant.phone", "Phone number"),
            _shown("home.address_line1", "Address line 1", EXACT),
            _shown("home.city", "Town or city", EXACT),
            _shown("home.state", "State", EXACT),
            _shown("home.zip", "ZIP code"),
            _shown("applicant.occupation", "Occupation", EXACT),
            _shown("household.drivers", "Licensed drivers in your household", INT),
            _shown("household.youthful_drivers", r"How many of them are under 25\?", INT),
            _shown(
                "household.vehicles", "Vehicles your household owns or leases", INT
            ),
            _shown("household.watercraft_shown", "Watercraft", EXACT),
            _shown("underlying.auto_liability", "Auto liability limits", EXACT),
            _shown("underlying.home_liability", "Home liability limit", MONEY),
            _shown("household.rental_properties", "Rental properties you own", INT),
            _shown("household.nonprofit_board", _NONPROFIT, BOOL),
            _shown("coverage.umbrella_limit", "Umbrella limit", MONEY),
            _shown("coverage.start_long", "Policy start date"),
        ),
    ),
    max_commit_mutations=1,
)


def _long_date(iso: Any) -> str | None:
    try:
        y, m, d = (int(p) for p in str(iso).split("-"))
    except ValueError:
        return None
    return f"{d} {_MONTHS[m - 1]} {y}"


def derived_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Intake facts restated as the portal asks for and shows them."""
    out: dict[str, Any] = {}
    for prefix, key in (("applicant.dob", "applicant.date_of_birth"),
                        ("coverage.start", "coverage.start_date")):  # fmt: skip
        iso = raw.get(key)
        if not iso:
            continue
        try:
            y, m, d = (int(p) for p in str(iso).split("-"))
        except ValueError:
            continue
        out[f"{prefix}_day"] = d
        out[f"{prefix}_month"] = m
        out[f"{prefix}_year"] = y
        out[f"{prefix}_long"] = _long_date(iso)
    # The summary says "None" when there is no watercraft. With watercraft the
    # intake gives only a count, so the shown list has no expected value.
    if raw.get("household.watercraft_count") in (0, "0"):
        out["household.watercraft_shown"] = "None"
    # The check page's confirmation box: ticked by the person submitting.
    out["flow.confirm_details"] = True
    return out
