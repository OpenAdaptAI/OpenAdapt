"""Form packs for the held-out quote portals, written from each dev case.

Every pack here is data. Each was written by hand by the form-layer author
from the portal's dev case and its source; ``induce_form_pack`` could not be
used because it needs labelled controls, and the harborline table layout has
no label elements. See RESULTS.md for what each pack could and could not
express.
"""

from __future__ import annotations

from typing import Any

from openadapt_flow.form_tx.types import (
    ControlKind,
    DossierFact,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormPack,
    InputDossier,
    PopupSubroutineSpec,
    PrefillPolicy,
    ProvenanceKind,
    SummaryCheckSpec,
    UIProfileSpec,
    WizardScreenSpec,
)

TEXT = FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE)
EXACT = FieldCodecSpec(kind=FormCodecKind.CASEFOLD)
INT = FieldCodecSpec(kind=FormCodecKind.INTEGER)
DATE_MDY = FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="MDY")


def _name(name: str) -> FieldAnchorSpec:
    return FieldAnchorSpec(name_or_id_patterns=(f"^{name}$",))


def _bind(
    key: str,
    name: str,
    kind: ControlKind = ControlKind.TEXT,
    codec: FieldCodecSpec = TEXT,
    *,
    required: bool = False,
    portal_default: bool = False,
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"b_{name}",
        dossier_key=key,
        anchor=_name(name),
        control_kind=kind,
        codec=codec,
        # A select the portal always pre-sets to a default (seen on the dev
        # case) is overwritten from the intake; every other prefilled value
        # that disagrees with the intake stops for a person.
        prefill_policy=(
            PrefillPolicy.OVERWRITE_WITH_DOSSIER
            if portal_default
            else PrefillPolicy.HALT_ON_CONFLICT
        ),
        required_in_pack=required,
    )


BOOL = FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)
NEGATED_SUFFIX = ".is_no"


def _yes_no_radios(key: str, name: str) -> tuple[FieldBindingSpec, FieldBindingSpec]:
    return tuple(  # type: ignore[return-value]
        FieldBindingSpec(
            binding_id=f"b_{name}_{value}",
            dossier_key=dossier_key,
            anchor=FieldAnchorSpec(css_selector=f'input[name="{name}"][value="{value}"]'),
            control_kind=ControlKind.CHECKBOX,
            codec=BOOL,
            # The frozen surface reads an unchecked native radio as holding its
            # value attribute, so every radio looks prefilled and would stop.
            # Seen on the dev case; these radios are set from the intake.
            prefill_policy=PrefillPolicy.OVERWRITE_WITH_DOSSIER,
        )
        for value, dossier_key in (("Y", key), ("N", key + NEGATED_SUFFIX))
    )


# ---------------------------------------------------------------- harborline
HARBORLINE_PROFILE = UIProfileSpec(
    profile_id="heldout_table_postback",
    error_banner_selectors=("p.err",),
    field_error_selectors=("span.err",),
    auth_wall_selectors=('input[type="password"]',),
)


def _hl_step(n: int) -> WizardScreenSpec:
    return WizardScreenSpec(
        screen_id=f"hl_step_{n}",
        landmark_selector=f"form[action='/quote/step/{n}']",
        next_button_anchor=FieldAnchorSpec(label_patterns=(r"^Next",)),
    )


HARBORLINE_PACK = FormPack(
    pack_id="heldout.harborline.home",
    domain_schema="personal_lines.homeowners",
    ui_profile_id=HARBORLINE_PROFILE.profile_id,
    bindings=(
        _bind("applicant.first_name", "first_name"),
        _bind("applicant.last_name", "last_name"),
        _bind("applicant.date_of_birth", "dob", ControlKind.DATE, DATE_MDY),
        _bind("applicant.phone", "phone"),
        _bind("applicant.email", "email"),
        _bind("history.prior_policy_number", "prior_policy"),
        _bind("property.address_line1", "street"),
        _bind("property.city", "city"),
        _bind("property.state", "state", ControlKind.SELECT, EXACT),
        _bind("property.zip", "zip"),
        _bind("property.year_built", "year_built", codec=INT),
        _bind("property.square_feet", "sqft", codec=INT),
        _bind("property.stories", "stories", ControlKind.SELECT, EXACT, portal_default=True),
        # Asked only for older homes; not in any intake, so it stops for a person.
        _bind(
            "property.construction_class",
            "construction_class",
            ControlKind.SELECT,
            EXACT,
            required=True,
        ),
        _bind("property.roof_year", "roof_year", codec=INT),
        _bind("property.roof_material", "roof_material", ControlKind.SELECT, EXACT),
        _bind("history.prior_claims_5yr", "losses_5yr", ControlKind.SELECT, INT),
        _bind("coverage.effective_date", "eff_date", ControlKind.DATE, DATE_MDY),
        _bind("coverage.deductible", "deductible", ControlKind.SELECT, INT, portal_default=True),
        _bind("coverage.liability_limit", "liability", ControlKind.SELECT, INT, portal_default=True),
        # Yes/No radio pairs: one checkbox-style binding per radio, the "No"
        # radio bound to the negation of the intake fact (see dossier_for).
        *_yes_no_radios("property.has_pool", "pool"),
        *_yes_no_radios("property.pool_fenced", "pool_fenced"),
        FieldBindingSpec(
            binding_id="b_cov_a_launcher",
            dossier_key="coverage.dwelling_a",
            anchor=FieldAnchorSpec(label_patterns=(r"^Estimate RC",)),
            control_kind=ControlKind.POPUP_LAUNCHER,
        ),
    ),
    popup_subroutines=(
        PopupSubroutineSpec(
            subroutine_id="hl_rce",
            trigger_anchor=FieldAnchorSpec(label_patterns=(r"^Estimate RC",)),
            popup_url_pattern="/tools/rce",
            bindings=(
                _bind("property.construction_quality", "quality", ControlKind.SELECT, EXACT),
            ),
            commit_anchor=FieldAnchorSpec(label_patterns=(r"^Calculate",)),
            parent_return_anchor=_name("cov_a"),
            return_output_key="coverage.dwelling_a",
            return_codec=INT,
        ),
    ),
    # The review page also holds a Back form that posts to step 4, so it is
    # listed first: the first matching landmark names the screen.
    screens=(
        WizardScreenSpec(
            screen_id="hl_review",
            landmark_selector="form[action='/quote/submit']",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(
                label_patterns=(r"^Submit Application",)
            ),
        ),
        _hl_step(1),
        _hl_step(2),
        _hl_step(3),
        _hl_step(4),
    ),
    # The confirmation page shows the stored values as plain table text, not
    # as form controls, so the frozen surface cannot read any of them back.
    # The contract names the page; every field ends not_checked.
    summary_check=SummaryCheckSpec(summary_landmark_selector="#appno"),
    max_commit_mutations=1,
)


# ---------------------------------------------------------------- cobalt
# A single-page app: the step is named by the progress bar, fields by their
# aria-labelledby text. Type-ahead comboboxes fetch suggestions only after
# text is typed; the frozen surface opens a listbox by clicking, so the pack
# can bind them but the layer may not be able to choose an option.
COBALT_PROFILE = UIProfileSpec(
    profile_id="heldout_spa_aria",
    error_banner_selectors=(".cr-error[role='alert']",),
    auth_wall_selectors=('input[type="password"]',),
    modal_dialog_selectors=('[role="dialog"][aria-modal="true"]',),
)


def _cr_step(n: int, next_label: str) -> WizardScreenSpec:
    return WizardScreenSpec(
        screen_id=f"cr_step_{n}",
        landmark_selector=f".progress > div:nth-child({n}).on + div:not(.on)",
        next_button_anchor=FieldAnchorSpec(label_patterns=(f"^{next_label}$",)),
    )


def _label(key: str, label: str, kind: ControlKind = ControlKind.TEXT,
           codec: FieldCodecSpec = TEXT) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"b_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(label_patterns=(f"^{label}$",)),
        control_kind=kind,
        codec=codec,
    )


COBALT_PACK = FormPack(
    pack_id="heldout.cobalt.auto",
    domain_schema="personal_lines.auto",
    ui_profile_id=COBALT_PROFILE.profile_id,
    bindings=(
        _label("applicant.first_name", "First name"),
        _label("applicant.last_name", "Last name"),
        _label("applicant.date_of_birth", "Date of birth", ControlKind.DATE,
               FieldCodecSpec(kind=FormCodecKind.ISO_DATE)),
        _label("applicant.email", "Email address"),
        _label("applicant.license_state", "State where licensed",
               ControlKind.PORTAL_COMBOBOX, EXACT),
        _label("vehicle.year", "Model year", ControlKind.SELECT, INT),
        _label("vehicle.make", "Make", ControlKind.PORTAL_COMBOBOX, EXACT),
        _label("vehicle.model", "Model", ControlKind.PORTAL_COMBOBOX, EXACT),
        _label("vehicle.annual_miles", "Estimated annual mileage", codec=INT),
        _label("vehicle.commute_miles_one_way", "One-way commute \\(miles\\)", codec=INT),
        _label("garaging.zip", "Garaging ZIP code"),
        _label("garaging.city", "Garaging city", ControlKind.PORTAL_COMBOBOX, EXACT),
        _label("vehicle.lienholder", "Lienholder or lessor name"),
    ),
    screens=(
        WizardScreenSpec(
            screen_id="cr_review",
            landmark_selector=".progress > div:nth-child(5).on",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=(r"^(Submit|Buy|Get)",)),
        ),
        _cr_step(1, "Continue"),
        _cr_step(2, "Continue"),
        _cr_step(3, "Continue"),
        _cr_step(4, "Review quote"),
    ),
    summary_check=SummaryCheckSpec(),
    max_commit_mutations=1,
)


PACKS: dict[str, tuple[FormPack, UIProfileSpec]] = {
    "harborline": (HARBORLINE_PACK, HARBORLINE_PROFILE),
    "cobalt": (COBALT_PACK, COBALT_PROFILE),
}

# Portals whose forms the frozen surface cannot see at all (probe.py):
# pinecrest renders the form in an iframe (0 controls on the page), tidewater
# in shadow-DOM custom elements (only a bare Next button). They run with an
# empty pack so the layer's own outcome is what gets recorded.
for _pid, _landmark in (("pinecrest", None), ("tidewater", None)):
    PACKS[_pid] = (
        FormPack(
            pack_id=f"heldout.{_pid}.unreadable",
            domain_schema="personal_lines.homeowners",
            ui_profile_id="heldout_default",
            summary_check=SummaryCheckSpec(),
        ),
        UIProfileSpec(profile_id="heldout_default"),
    )


def dossier_for(intake: dict[str, Any], request_id: str) -> InputDossier:
    facts = {
        key: DossierFact(
            key=key,
            value=_intake_value(value),
            provenance=ProvenanceKind.INTAKE_DOCUMENT,
            source_id=f"intake:{intake['case_id']}",
            confidence=1.0,
        )
        for key, value in intake["facts"].items()
        if value is not None and value != ""
    }
    for key, fact in list(facts.items()):
        if isinstance(fact.value, bool):
            neg = key + NEGATED_SUFFIX
            facts[neg] = fact.model_copy(update={"key": neg, "value": not fact.value})
    return InputDossier(dossier_id=f"intake:{intake['case_id']}", request_id=request_id, facts=facts)


def _intake_value(value: Any) -> Any:
    return value


# ------------------------------------------------------- post-hoc (labelled)
# Written after the scored run, from what it showed; never replaces the
# frozen row. P1: the estimator's Quality Grade select is pre-set to
# "Standard", so it is overwritten from the intake like the other defaults.
HARBORLINE_PACK_POSTHOC_P1 = HARBORLINE_PACK.model_copy(
    update={
        "pack_id": "heldout.harborline.home.posthoc_p1",
        "popup_subroutines": (
            HARBORLINE_PACK.popup_subroutines[0].model_copy(
                update={
                    "bindings": (
                        _bind(
                            "property.construction_quality",
                            "quality",
                            ControlKind.SELECT,
                            EXACT,
                            portal_default=True,
                        ),
                    )
                }
            ),
        ),
    }
)
