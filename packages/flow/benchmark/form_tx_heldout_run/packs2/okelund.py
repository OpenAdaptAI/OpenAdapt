"""Okelund: personal auto quote in a frameset (data only).

Written from the ok-dev intake, the dev-case demonstration and a dev-case
walk-through with the exploration probe; no portal source, scored intake or
answer key was read. What the dev case showed:

- The quote form lives in the ``/an/app`` frame of a frameset; when a
  session ends, that frame shows a sign-in page (``/an/login``).
- Form labels sit in table cells the surface does not link to the controls,
  so every binding is anchored by the control's ``name``.
- Typing the ZIP fills the city and the read-only state; the ZIP is bound
  before the city, so a city the portal fills that disagrees with the intake
  stops for a person.
- Make and model are read-only on the form and are set by a Vehicle Lookup
  window: make and "model contains" are entered, Search lists matching
  vehicles, and each row has its own Select button. With more than one
  matching row the Select button is ambiguous, so the run stops (the dev
  case lists two rows).
- Coverage selects come pre-set by the portal; they are overwritten from the
  intake.
- After Submit for Rating the frame shows "Quote Rated" with every stored
  value as a table; that page is read back by label. The quote number is
  shown without a "label: value" form, so it is not a required output.

Dev-case smoke on the re-pinned engine: the dev case stops before submit at
the lookup window, because its search lists two vehicles and each row's
button is labelled "Select": choosing the row that matches the intake is not
expressible, so the commit anchor is ambiguous (a stop, never a guess). With a
test-only change that picked the first row (not part of this pack), the dev
case ended verified with a receipt, 25 fields passed, so the rest of the pack
works; cases whose lookup lists one vehicle can go through.
"""

from __future__ import annotations

from openadapt_flow.form_tx.types import (
    ControlKind,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormPack,
    OutputExtractionSpec,
    PopupSubroutineSpec,
    PrefillPolicy,
    SummaryCheckSpec,
    UIProfileSpec,
    WizardScreenSpec,
)

CASE_PREFIX = "ok"

TEXT = FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE)
FOLD = FieldCodecSpec(kind=FormCodecKind.CASEFOLD)
INT = FieldCodecSpec(kind=FormCodecKind.INTEGER)
DATE_MDY = FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="MDY")
MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")
BOOL = FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)

SUMMARY_SECTION = (r"^Quote Rated$",)


def _bind(
    key: str,
    name: str,
    kind: ControlKind = ControlKind.TEXT,
    codec: FieldCodecSpec = TEXT,
    *,
    portal_default: bool = False,
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"ok_{name}",
        dossier_key=key,
        anchor=FieldAnchorSpec(name_or_id_patterns=(f"^{name}$",)),
        control_kind=kind,
        codec=codec,
        prefill_policy=(
            PrefillPolicy.OVERWRITE_WITH_DOSSIER
            if portal_default
            else PrefillPolicy.HALT_ON_CONFLICT
        ),
    )


def _shown(key: str, label: str, codec: FieldCodecSpec = TEXT) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"ok_rb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",), section_patterns=SUMMARY_SECTION
        ),
        codec=codec,
    )


def _screen(screen_id: str, landmark: str) -> WizardScreenSpec:
    return WizardScreenSpec(
        screen_id=screen_id,
        landmark_selector=landmark,
        next_button_anchor=FieldAnchorSpec(label_patterns=(r"^Save & Next >$",)),
    )


PROFILE = UIProfileSpec(
    profile_id="okelund",
    frame_path_patterns=(r"/an/app", r"/an/login"),
    max_settle_ms=15000,
)

VEHICLE_LOOKUP = PopupSubroutineSpec(
    subroutine_id="ok_vehicle_lookup",
    trigger_anchor=FieldAnchorSpec(label_patterns=(r"^Vehicle Lookup\.\.\.$",)),
    bindings=(
        _bind("vehicle.make", "make", ControlKind.SELECT, FOLD),
        _bind("vehicle.model", "model", ControlKind.TEXT, TEXT),
    ),
    compute_anchors=(FieldAnchorSpec(label_patterns=(r"^Search$",)),),
    commit_anchor=FieldAnchorSpec(
        label_patterns=(r"^Select$",), section_patterns=(r"^Vehicle Lookup$",)
    ),
    parent_return_anchor=FieldAnchorSpec(name_or_id_patterns=(r"^model$",)),
    return_output_key="vehicle.model",
    return_codec=FOLD,
)

PACK = FormPack(
    pack_id="okelund-auto-v1",
    domain_schema="personal_auto_quote/v1",
    ui_profile_id="okelund",
    bindings=(
        # 1. Named Insured
        _bind("applicant.first_name", "first_name"),
        _bind("applicant.last_name", "last_name"),
        _bind("applicant.date_of_birth", "dob", ControlKind.TEXT, DATE_MDY),
        _bind("applicant.marital_status", "marital_status", ControlKind.SELECT, FOLD),
        _bind("applicant.email", "email"),
        _bind("garaging.address_line1", "street"),
        _bind("garaging.zip", "zip"),
        _bind("garaging.city", "city", ControlKind.TEXT, FOLD),
        # 2. Driver
        _bind("driver.license_state", "license_state", ControlKind.SELECT, FOLD),
        _bind("driver.years_licensed", "years_licensed", ControlKind.TEXT, INT),
        _bind("driver.accidents_3yr", "accidents", ControlKind.SELECT, INT),
        _bind("driver.violations_3yr", "violations", ControlKind.SELECT, INT),
        # 3. Vehicle (make and model come from the lookup window)
        _bind("vehicle.year", "vehicle_year", ControlKind.SELECT, INT),
        _bind("vehicle.primary_use", "use", ControlKind.SELECT, FOLD),
        _bind("vehicle.annual_miles", "annual_miles", ControlKind.TEXT, INT),
        _bind("vehicle.ownership", "ownership", ControlKind.SELECT, FOLD),
        _bind(
            "vehicle.aftermarket_equipment",
            "aftermarket",
            ControlKind.RADIO_GROUP,
            BOOL,
        ),
        # 4. Coverages (selects the portal pre-sets)
        _bind("coverage.bi_limits", "bi_limits", ControlKind.SELECT, TEXT),
        _bind(
            "coverage.collision_deductible",
            "coll_ded",
            ControlKind.SELECT,
            MONEY,
            portal_default=True,
        ),
        _bind(
            "coverage.comprehensive_deductible",
            "comp_ded",
            ControlKind.SELECT,
            MONEY,
            portal_default=True,
        ),
        _bind(
            "coverage.rental_reimbursement",
            "rental",
            ControlKind.SELECT,
            FOLD,
            portal_default=True,
        ),
        _bind("coverage.effective_date", "eff_date", ControlKind.TEXT, DATE_MDY),
    ),
    popup_subroutines=(VEHICLE_LOOKUP,),
    screens=(
        WizardScreenSpec(
            screen_id="ok_review",
            landmark_selector="input[type='submit'][value='Submit for Rating']",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(
                label_patterns=(r"^Submit for Rating$",)
            ),
        ),
        _screen("ok_insured", "input[name='first_name']"),
        _screen("ok_driver", "select[name='license_state']"),
        _screen("ok_vehicle", "select[name='vehicle_year']"),
        _screen("ok_coverages", "select[name='bi_limits']"),
    ),
    summary_check=SummaryCheckSpec(
        field_readbacks=(
            _shown("applicant.first_name", "First Name"),
            _shown("applicant.last_name", "Last Name"),
            _shown(
                "applicant.date_of_birth", r"Date of Birth \(MM/DD/YYYY\)", DATE_MDY
            ),
            _shown("applicant.marital_status", "Marital Status", FOLD),
            _shown("applicant.email", "E-Mail Address"),
            _shown("garaging.address_line1", "Garaging Street"),
            _shown("garaging.city", "Garaging City", FOLD),
            _shown("garaging.state", "Garaging State", FOLD),
            _shown("garaging.zip", "Garaging ZIP"),
            _shown("driver.license_state", "License State", FOLD),
            _shown("driver.years_licensed", "Years Licensed", INT),
            _shown("driver.accidents_3yr", r"At-Fault Accidents \(3 Yrs\)", INT),
            _shown("driver.violations_3yr", r"Moving Violations \(3 Yrs\)", INT),
            _shown("vehicle.year", "Model Year", INT),
            _shown("vehicle.make", "Make", FOLD),
            _shown("vehicle.model", "Model", FOLD),
            _shown("vehicle.primary_use", "Primary Use", FOLD),
            _shown("vehicle.annual_miles", "Annual Miles", INT),
            _shown("vehicle.ownership", "Ownership", FOLD),
            _shown(
                "vehicle.aftermarket_equipment",
                r"Any custom or aftermarket equipment \(wheels, audio, lift kits\)\?",
                BOOL,
            ),
            _shown("coverage.bi_limits", "Bodily Injury Limits"),
            _shown("coverage.collision_deductible", "Collision Deductible", MONEY),
            _shown(
                "coverage.comprehensive_deductible", "Comprehensive Deductible", MONEY
            ),
            _shown("coverage.rental_reimbursement", "Rental Reimbursement", FOLD),
            _shown(
                "coverage.effective_date",
                r"Policy Effective Date \(MM/DD/YYYY\)",
                DATE_MDY,
            ),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="quote.number",
                anchor=FieldAnchorSpec(label_patterns=(r"^Quote Number$",)),
                required=False,
            ),
        ),
    ),
)
