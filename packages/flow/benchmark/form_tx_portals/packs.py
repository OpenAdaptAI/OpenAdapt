"""Declarative ``FormPack`` definitions and realistic workloads for benchmark portals.

Data-only specifications: zero executable per-portal logic in the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from openadapt_flow.form_tx.types import (
    ControlKind,
    DossierEntity,
    DossierFact,
    EntityTableSpec,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormPack,
    InputDossier,
    ModalRuleSpec,
    OutputExtractionSpec,
    PopupSubroutineSpec,
    PrefillPolicy,
    ProvenanceKind,
    SummaryCheckSpec,
    WizardScreenSpec,
)


def _fact(
    key: str,
    value: Any,
    *,
    provenance: ProvenanceKind = ProvenanceKind.INTAKE_DOCUMENT,
    source_id: str = "dossier_src_01",
    confidence: float = 1.0,
) -> DossierFact:
    return DossierFact(
        key=key,
        value=value,
        raw_value=str(value),
        provenance=provenance,
        source_id=source_id,
        confidence=confidence,
    )


def build_underwriting_wizard_pack() -> FormPack:
    """Data-only ``FormPack`` for ``StatefulUnderwritingPortal``."""
    return FormPack(
        pack_id="property_underwriting_wizard_v1",
        domain_schema="property_quote",
        ui_profile_id="server_ppr_wizard",
        bindings=(
            FieldBindingSpec(
                binding_id="b_fname",
                dossier_key="applicant.first_name",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Applicant First Name",),
                    name_or_id_patterns=(r"\bfirst_name\b",),
                ),
                codec=FieldCodecSpec(kind=FormCodecKind.TITLE_CASE),
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="b_lname",
                dossier_key="applicant.last_name",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Applicant Last Name",),
                    name_or_id_patterns=(r"\blast_name\b",),
                ),
                codec=FieldCodecSpec(kind=FormCodecKind.TITLE_CASE),
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="b_dob",
                dossier_key="applicant.dob",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Date of Birth",),
                    name_or_id_patterns=(r"\bdob\b",),
                ),
                control_kind=ControlKind.DATE,
                codec=FieldCodecSpec(kind=FormCodecKind.ISO_DATE),
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="b_phone",
                dossier_key="applicant.phone",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Primary Phone",),
                    name_or_id_patterns=(r"\bphone\b",),
                ),
                codec=FieldCodecSpec(kind=FormCodecKind.PHONE_DIGITS),
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="b_marital",
                dossier_key="applicant.marital_status",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Marital Status",),
                    name_or_id_patterns=(r"\bmarital_status\b",),
                ),
                control_kind=ControlKind.SELECT,
                codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="b_claims",
                dossier_key="applicant.prior_claims_count",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Prior Losses",),
                    name_or_id_patterns=(r"\bprior_claims_count\b",),
                ),
                codec=FieldCodecSpec(kind=FormCodecKind.INTEGER),
                prefill_policy=PrefillPolicy.HALT_ON_CONFLICT,
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="b_street",
                dossier_key="dwelling.street_address",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Property Street Address",),
                    name_or_id_patterns=(r"\bstreet_address\b",),
                ),
                codec=FieldCodecSpec(kind=FormCodecKind.UPPER),
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="b_year_built",
                dossier_key="dwelling.year_built",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Year Built",),
                    name_or_id_patterns=(r"\byear_built\b",),
                ),
                codec=FieldCodecSpec(kind=FormCodecKind.INTEGER),
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="b_roof_year",
                dossier_key="dwelling.roof_year",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Roof Year Installed",),
                    name_or_id_patterns=(r"\broof_year\b",),
                ),
                codec=FieldCodecSpec(kind=FormCodecKind.INTEGER),
                triggers_re_render=True,
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="b_roof_update",
                dossier_key="dwelling.roof_update_type",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Roof Update Type",),
                    name_or_id_patterns=(r"\broof_update_type\b",),
                ),
                control_kind=ControlKind.SELECT,
                codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
            ),
            FieldBindingSpec(
                binding_id="b_has_pool",
                dossier_key="dwelling.has_pool",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Swimming Pool on Premises",),
                    name_or_id_patterns=(r"\bhas_pool\b",),
                ),
                control_kind=ControlKind.SELECT,
                codec=FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO),
                triggers_re_render=True,
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="b_pool_fenced",
                dossier_key="dwelling.pool_fenced",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Pool Fenced with Self-Latching Gate",),
                    name_or_id_patterns=(r"\bpool_fenced\b",),
                ),
                control_kind=ControlKind.SELECT,
                codec=FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO),
            ),
            # NOTE: `has_trampoline` is intentionally NOT bound in v1 pack so
            # conditional emergence tests genuine epistemic abstention + learning!
            FieldBindingSpec(
                binding_id="b_deductible",
                dossier_key="coverage.deductible",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^All-Peril Deductible",),
                    name_or_id_patterns=(r"\bdeductible\b",),
                ),
                control_kind=ControlKind.SELECT,
                codec=FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS),
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="b_liability",
                dossier_key="coverage.liability_limit",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Personal Liability Limit",),
                    name_or_id_patterns=(r"\bliability_limit\b",),
                ),
                control_kind=ControlKind.SELECT,
                codec=FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS),
                required_in_pack=True,
            ),
        ),
        entity_tables=(
            EntityTableSpec(
                table_id="household_members_table",
                entity_kind="household_member",
                row_selector="#household-table tbody tr[data-entity-row]",
                column_selectors={
                    "full_name": "[data-col='full_name']",
                    "relationship": "[data-col='relationship']",
                    "dob": "[data-col='dob']",
                },
                key_fields=("full_name", "dob"),
                field_codecs={
                    "full_name": FieldCodecSpec(kind=FormCodecKind.TITLE_CASE),
                    "dob": FieldCodecSpec(kind=FormCodecKind.ISO_DATE),
                },
                dismiss_button_selector="button.dismiss-hh",
            ),
        ),
        popup_subroutines=(
            PopupSubroutineSpec(
                subroutine_id="replacement_cost_estimator",
                trigger_anchor=FieldAnchorSpec(
                    css_selector="#btn-open-estimator",
                    label_patterns=(r"Launch Replacement Cost Estimator",),
                ),
                popup_url_pattern=r"/estimator/popup",
                popup_title_pattern=r"Replacement Cost Estimator",
                bindings=(
                    FieldBindingSpec(
                        binding_id="est_b_sqft",
                        dossier_key="estimator.living_area_sqft",
                        anchor=FieldAnchorSpec(
                            label_patterns=(r"^Living Area",),
                            name_or_id_patterns=(r"\bliving_area_sqft\b",),
                        ),
                        codec=FieldCodecSpec(kind=FormCodecKind.INTEGER),
                        required_in_pack=True,
                    ),
                    FieldBindingSpec(
                        binding_id="est_b_stories",
                        dossier_key="estimator.stories",
                        anchor=FieldAnchorSpec(
                            label_patterns=(r"^Number of Stories",),
                            name_or_id_patterns=(r"\bstories\b",),
                        ),
                        control_kind=ControlKind.SELECT,
                        codec=FieldCodecSpec(
                            kind=FormCodecKind.ENUM_MAP,
                            enum_aliases={
                                "1": ("1", "1 Story"),
                                "2": ("2", "2 Stories"),
                            },
                        ),
                        required_in_pack=True,
                    ),
                    FieldBindingSpec(
                        binding_id="est_b_grade",
                        dossier_key="estimator.finish_grade",
                        anchor=FieldAnchorSpec(
                            label_patterns=(r"^Interior Finish Grade",),
                            name_or_id_patterns=(r"\best_grade_btn\b",),
                            role="combobox",
                        ),
                        control_kind=ControlKind.PORTAL_COMBOBOX,
                        codec=FieldCodecSpec(
                            kind=FormCodecKind.ENUM_MAP,
                            enum_aliases={
                                "Standard": ("Standard", "Standard Finish"),
                                "Custom": ("Custom", "Custom Finish"),
                                "Luxury": ("Luxury", "Luxury Finish"),
                            },
                        ),
                        required_in_pack=True,
                    ),
                ),
                commit_anchor=FieldAnchorSpec(
                    css_selector="#btn-calc-apply",
                    label_patterns=(r"Calculate & Apply",),
                ),
                parent_return_anchor=FieldAnchorSpec(
                    css_selector="#replacement_cost_display",
                    label_patterns=(r"^Estimated Replacement Cost",),
                ),
                return_output_key="estimated_replacement_cost_cents",
                return_codec=FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS),
            ),
        ),
        screens=(
            WizardScreenSpec(
                screen_id="applicant",
                landmark_selector="body[data-screen-id='applicant']",
                next_button_anchor=FieldAnchorSpec(
                    css_selector="#btn-applicant-next",
                    label_patterns=(r"^Next: Property Details",),
                ),
            ),
            WizardScreenSpec(
                screen_id="property",
                landmark_selector="body[data-screen-id='property']",
                next_button_anchor=FieldAnchorSpec(
                    css_selector="#btn-property-next",
                    label_patterns=(r"^Next: Coverage",),
                ),
            ),
            WizardScreenSpec(
                screen_id="coverage",
                landmark_selector="body[data-screen-id='coverage']",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(
                    css_selector="#btn-issue-quote",
                    label_patterns=(r"^Generate Official Quote",),
                ),
            ),
        ),
        summary_check=SummaryCheckSpec(
            summary_landmark_selector="body[data-screen-id='quote_summary']",
            field_readbacks=(
                FieldBindingSpec(
                    binding_id="rb_fname",
                    dossier_key="applicant.first_name",
                    anchor=FieldAnchorSpec(css_selector="#sum_fname"),
                    control_kind=ControlKind.READONLY_OUTPUT,
                    codec=FieldCodecSpec(kind=FormCodecKind.TITLE_CASE),
                ),
                FieldBindingSpec(
                    binding_id="rb_lname",
                    dossier_key="applicant.last_name",
                    anchor=FieldAnchorSpec(css_selector="#sum_lname"),
                    control_kind=ControlKind.READONLY_OUTPUT,
                    codec=FieldCodecSpec(kind=FormCodecKind.TITLE_CASE),
                ),
                FieldBindingSpec(
                    binding_id="rb_dob",
                    dossier_key="applicant.dob",
                    anchor=FieldAnchorSpec(css_selector="#sum_dob"),
                    control_kind=ControlKind.READONLY_OUTPUT,
                    codec=FieldCodecSpec(kind=FormCodecKind.ISO_DATE),
                ),
                FieldBindingSpec(
                    binding_id="rb_phone",
                    dossier_key="applicant.phone",
                    anchor=FieldAnchorSpec(css_selector="#sum_phone"),
                    control_kind=ControlKind.READONLY_OUTPUT,
                    codec=FieldCodecSpec(kind=FormCodecKind.PHONE_DIGITS),
                ),
                FieldBindingSpec(
                    binding_id="rb_street",
                    dossier_key="dwelling.street_address",
                    anchor=FieldAnchorSpec(css_selector="#sum_street"),
                    control_kind=ControlKind.READONLY_OUTPUT,
                    codec=FieldCodecSpec(kind=FormCodecKind.UPPER),
                ),
                FieldBindingSpec(
                    binding_id="rb_pool",
                    dossier_key="dwelling.has_pool",
                    anchor=FieldAnchorSpec(css_selector="#sum_pool"),
                    control_kind=ControlKind.READONLY_OUTPUT,
                    codec=FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO),
                ),
                FieldBindingSpec(
                    binding_id="rb_claims",
                    dossier_key="applicant.prior_claims_count",
                    anchor=FieldAnchorSpec(css_selector="#sum_claims"),
                    control_kind=ControlKind.READONLY_OUTPUT,
                    codec=FieldCodecSpec(kind=FormCodecKind.INTEGER),
                ),
                FieldBindingSpec(
                    binding_id="rb_ded",
                    dossier_key="coverage.deductible",
                    anchor=FieldAnchorSpec(css_selector="#sum_ded"),
                    control_kind=ControlKind.READONLY_OUTPUT,
                    codec=FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS),
                ),
            ),
            outputs=(
                OutputExtractionSpec(
                    output_key="quote_number",
                    anchor=FieldAnchorSpec(css_selector="#sum_quote_no"),
                    codec=FieldCodecSpec(kind=FormCodecKind.EXACT),
                ),
                OutputExtractionSpec(
                    output_key="annual_premium_cents",
                    anchor=FieldAnchorSpec(css_selector="#sum_premium"),
                    codec=FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS),
                ),
                OutputExtractionSpec(
                    output_key="replacement_cost_cents",
                    anchor=FieldAnchorSpec(css_selector="#sum_rc"),
                    codec=FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS),
                ),
            ),
        ),
        max_commit_mutations=1,
    )


def build_hospital_hms_pack() -> FormPack:
    """Data-only ``FormPack`` for held-out ``LegacyHospitalQueuePortal``."""
    return FormPack(
        pack_id="hospital_outpatient_registration_v1",
        domain_schema="patient_registration",
        ui_profile_id="legacy_postback_portal",
        bindings=(
            FieldBindingSpec(
                binding_id="hms_b_name",
                dossier_key="patient.full_name",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Patient Full Name",),
                    name_or_id_patterns=(r"\bpatient_name\b",),
                ),
                codec=FieldCodecSpec(kind=FormCodecKind.TITLE_CASE),
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="hms_b_dob",
                dossier_key="patient.dob",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Date of Birth",),
                    name_or_id_patterns=(r"\bdob\b",),
                ),
                control_kind=ControlKind.DATE,
                codec=FieldCodecSpec(kind=FormCodecKind.ISO_DATE),
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="hms_b_sex",
                dossier_key="patient.sex",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Biological Sex",),
                    name_or_id_patterns=(r"\bsex\b",),
                ),
                control_kind=ControlKind.SELECT,
                codec=FieldCodecSpec(kind=FormCodecKind.UPPER),
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="hms_b_mobile",
                dossier_key="patient.mobile",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Mobile Number",),
                    name_or_id_patterns=(r"\bmobile\b",),
                ),
                codec=FieldCodecSpec(kind=FormCodecKind.PHONE_DIGITS),
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="hms_b_visit",
                dossier_key="visit.visit_type",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Visit Category",),
                    name_or_id_patterns=(r"\bvisit_type\b",),
                ),
                control_kind=ControlKind.SELECT,
                codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="hms_b_dept",
                dossier_key="visit.department",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Clinical Department",),
                    name_or_id_patterns=(r"\bdepartment\b",),
                ),
                control_kind=ControlKind.SELECT,
                codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
                triggers_re_render=True,
                required_in_pack=True,
            ),
            FieldBindingSpec(
                binding_id="hms_b_doc",
                dossier_key="visit.consulting_doctor",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Consulting Doctor",),
                    name_or_id_patterns=(r"\bdoc_combo_btn\b",),
                    role="combobox",
                ),
                control_kind=ControlKind.PORTAL_COMBOBOX,
                codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
                required_in_pack=True,
            ),
        ),
        modal_rules=(
            ModalRuleSpec(
                modal_id="duplicate_phone_modal",
                container_selector="dialog#dup-phone-modal[open]",
                message_selector="#dup-modal-msg",
                confirm_selector="#btn-confirm-dup",
                cancel_selector="#btn-cancel-dup",
                policy="halt_for_human",
            ),
        ),
        screens=(
            WizardScreenSpec(
                screen_id="hms_registration",
                landmark_selector="body[data-screen-id='hms_registration']",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(
                    css_selector="#btn-hms-submit",
                    label_patterns=(r"^Register Patient",),
                ),
            ),
        ),
        summary_check=SummaryCheckSpec(
            summary_landmark_selector="body[data-screen-id='hms_search']",
            field_readbacks=(
                FieldBindingSpec(
                    binding_id="hms_rb_name",
                    dossier_key="patient.full_name",
                    anchor=FieldAnchorSpec(css_selector="#rd_name"),
                    control_kind=ControlKind.READONLY_OUTPUT,
                    codec=FieldCodecSpec(kind=FormCodecKind.TITLE_CASE),
                ),
                FieldBindingSpec(
                    binding_id="hms_rb_dob",
                    dossier_key="patient.dob",
                    anchor=FieldAnchorSpec(css_selector="#rd_dob"),
                    control_kind=ControlKind.READONLY_OUTPUT,
                    codec=FieldCodecSpec(kind=FormCodecKind.ISO_DATE),
                ),
                FieldBindingSpec(
                    binding_id="hms_rb_sex",
                    dossier_key="patient.sex",
                    anchor=FieldAnchorSpec(css_selector="#rd_sex"),
                    control_kind=ControlKind.READONLY_OUTPUT,
                    codec=FieldCodecSpec(kind=FormCodecKind.UPPER),
                ),
                FieldBindingSpec(
                    binding_id="hms_rb_mobile",
                    dossier_key="patient.mobile",
                    anchor=FieldAnchorSpec(css_selector="#rd_mobile"),
                    control_kind=ControlKind.READONLY_OUTPUT,
                    codec=FieldCodecSpec(kind=FormCodecKind.PHONE_DIGITS),
                ),
                FieldBindingSpec(
                    binding_id="hms_rb_dept",
                    dossier_key="visit.department",
                    anchor=FieldAnchorSpec(css_selector="#rd_dept"),
                    control_kind=ControlKind.READONLY_OUTPUT,
                    codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
                ),
                FieldBindingSpec(
                    binding_id="hms_rb_doc",
                    dossier_key="visit.consulting_doctor",
                    anchor=FieldAnchorSpec(css_selector="#rd_doc"),
                    control_kind=ControlKind.READONLY_OUTPUT,
                    codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
                ),
            ),
            outputs=(
                OutputExtractionSpec(
                    output_key="uhid",
                    anchor=FieldAnchorSpec(css_selector="#rd_uhid"),
                    codec=FieldCodecSpec(kind=FormCodecKind.EXACT),
                ),
            ),
        ),
        max_commit_mutations=2,
    )


@dataclass(frozen=True)
class UnderwritingScenario:
    scenario_id: str
    description: str
    dossier: InputDossier
    inject_unlisted_spouse: bool = False
    prefill_prior_claims: Optional[str] = None
    ground_truth_trampoline: Optional[str] = None
    ground_truth_spouse_valid: bool = False
    expect_validation_block: bool = False


def build_underwriting_scenarios() -> tuple[UnderwritingScenario, ...]:
    """Representative scenarios spanning clean, conditional, prefill-conflict, and rule-blocked quotes."""

    def _make_dossier(
        sid: str,
        *,
        fname: str,
        lname: str,
        dob: str,
        phone: str,
        marital: str,
        claims: str,
        street: str,
        yr_built: str,
        roof_yr: str,
        roof_upd: Optional[str],
        pool: str,
        pool_fenced: Optional[str],
        trampoline: Optional[str],
        sqft: str,
        stories: str,
        grade: str,
        ded: str,
        liab: str,
    ) -> InputDossier:
        facts = {
            "applicant.first_name": _fact("applicant.first_name", fname),
            "applicant.last_name": _fact("applicant.last_name", lname),
            "applicant.dob": _fact("applicant.dob", dob),
            "applicant.phone": _fact("applicant.phone", phone),
            "applicant.marital_status": _fact("applicant.marital_status", marital),
            "applicant.prior_claims_count": _fact("applicant.prior_claims_count", claims),
            "dwelling.street_address": _fact("dwelling.street_address", street),
            "dwelling.year_built": _fact("dwelling.year_built", yr_built),
            "dwelling.roof_year": _fact("dwelling.roof_year", roof_yr),
            "dwelling.has_pool": _fact("dwelling.has_pool", pool),
            "estimator.living_area_sqft": _fact("estimator.living_area_sqft", sqft),
            "estimator.stories": _fact("estimator.stories", stories),
            "estimator.finish_grade": _fact("estimator.finish_grade", grade),
            "coverage.deductible": _fact("coverage.deductible", ded),
            "coverage.liability_limit": _fact("coverage.liability_limit", liab),
        }
        if roof_upd is not None:
            facts["dwelling.roof_update_type"] = _fact("dwelling.roof_update_type", roof_upd)
        if pool_fenced is not None:
            facts["dwelling.pool_fenced"] = _fact("dwelling.pool_fenced", pool_fenced)
        if trampoline is not None:
            facts["dwelling.has_trampoline"] = _fact("dwelling.has_trampoline", trampoline)

        primary_entity = DossierEntity(
            entity_kind="household_member",
            entity_id="primary_insured",
            fields={
                "full_name": _fact("full_name", f"{fname} {lname}"),
                "relationship": _fact("relationship", "Primary Insured"),
                "dob": _fact("dob", dob),
            },
        )
        return InputDossier(
            dossier_id=f"dos_{sid}",
            request_id=f"req_{sid}",
            facts=facts,
            entities=(primary_entity,),
        )

    return (
        UnderwritingScenario(
            scenario_id="uw_01_clean_modern_home",
            description="Standard modern home, no pool, recent roof, 100% grounded autonomous quote",
            dossier=_make_dossier(
                "uw_01",
                fname="claire",
                lname="bennett",
                dob="1988-07-14",
                phone="614-555-0148",
                marital="Single",
                claims="0",
                street="742 Evergreen Ter",
                yr_built="2018",
                roof_yr="2018",
                roof_upd=None,
                pool="No",
                pool_fenced=None,
                trampoline=None,
                sqft="2200",
                stories="2",
                grade="Custom",
                ded="2500",
                liab="500000",
            ),
        ),
        UnderwritingScenario(
            scenario_id="uw_02_older_roof_conditional_grounded",
            description="2004 roof triggers PPR conditional Roof Update field (grounded in dossier)",
            dossier=_make_dossier(
                "uw_02",
                fname="marcus",
                lname="vance",
                dob="1979-11-02",
                phone="(614) 555-0192",
                marital="Married",
                claims="0",
                street="19 Oak Hollow Rd",
                yr_built="1998",
                roof_yr="2004",
                roof_upd="Full Replacement",
                pool="No",
                pool_fenced=None,
                trampoline=None,
                sqft="2600",
                stories="2",
                grade="Standard",
                ded="2500",
                liab="300000",
            ),
        ),
        UnderwritingScenario(
            scenario_id="uw_03_unlisted_spouse_prefill",
            description="Portal injects unlisted spouse 'Jordan Avery' not in intake dossier",
            dossier=_make_dossier(
                "uw_03",
                fname="elena",
                lname="rostova",
                dob="1985-03-22",
                phone="6145550177",
                marital="Single",
                claims="0",
                street="88 Summit Ave",
                yr_built="2016",
                roof_yr="2016",
                roof_upd=None,
                pool="No",
                pool_fenced=None,
                trampoline=None,
                sqft="1950",
                stories="1",
                grade="Custom",
                ded="1000",
                liab="300000",
            ),
            inject_unlisted_spouse=True,
            ground_truth_spouse_valid=False,
        ),
        UnderwritingScenario(
            scenario_id="uw_04_pool_spawns_unmapped_trampoline_1",
            description="Pool=Yes triggers PPR spawning unmapped 'Trampoline' question (actual=Yes!)",
            dossier=_make_dossier(
                "uw_04",
                fname="devon",
                lname="brooks",
                dob="1982-09-10",
                phone="614-555-0115",
                marital="Married",
                claims="0",
                street="410 Pinecrest Dr",
                yr_built="2015",
                roof_yr="2015",
                roof_upd=None,
                pool="Yes",
                pool_fenced="Yes",
                trampoline="Yes",  # In dossier, but v1 pack lacks binding until taught!
                sqft="3100",
                stories="2",
                grade="Luxury",
                ded="5000",
                liab="500000",
            ),
            ground_truth_trampoline="Yes",
        ),
        UnderwritingScenario(
            scenario_id="uw_05_pool_spawns_unmapped_trampoline_2",
            description="Second pool quote teaching consistent trampoline mapping for RuleAccumulator",
            dossier=_make_dossier(
                "uw_05",
                fname="hannah",
                lname="kim",
                dob="1990-01-29",
                phone="614-555-0163",
                marital="Single",
                claims="0",
                street="52 Meadowbrook Ln",
                yr_built="2019",
                roof_yr="2019",
                roof_upd=None,
                pool="Yes",
                pool_fenced="Yes",
                trampoline="No",
                sqft="2800",
                stories="2",
                grade="Custom",
                ded="2500",
                liab="500000",
            ),
            ground_truth_trampoline="No",
        ),
        UnderwritingScenario(
            scenario_id="uw_06_pool_zero_halt_after_learned_rule",
            description="Third pool quote after admitting induced trampoline binding -> 0 halts!",
            dossier=_make_dossier(
                "uw_06",
                fname="samuel",
                lname="ortiz",
                dob="1976-06-05",
                phone="614-555-0188",
                marital="Married",
                claims="1",
                street="905 Lakeshore Blvd",
                yr_built="2014",
                roof_yr="2014",
                roof_upd=None,
                pool="Yes",
                pool_fenced="Yes",
                trampoline="Yes",
                sqft="3400",
                stories="2",
                grade="Luxury",
                ded="5000",
                liab="500000",
            ),
            ground_truth_trampoline="Yes",
        ),
        UnderwritingScenario(
            scenario_id="uw_07_prefill_prior_claims_conflict",
            description="Portal prefills Prior Losses = 2 when intake dossier says 0",
            dossier=_make_dossier(
                "uw_07",
                fname="nora",
                lname="patel",
                dob="1992-12-11",
                phone="614-555-0131",
                marital="Single",
                claims="0",
                street="301 Cedar Ct",
                yr_built="2020",
                roof_yr="2020",
                roof_upd=None,
                pool="No",
                pool_fenced=None,
                trampoline=None,
                sqft="1800",
                stories="1",
                grade="Standard",
                ded="1000",
                liab="300000",
            ),
            prefill_prior_claims="2",
        ),
    )


def build_hms_queue_dossiers() -> tuple[InputDossier, ...]:
    """Queue jobs for held-out ``LegacyHospitalQueuePortal`` including duplicate-phone modal."""
    return (
        InputDossier(
            dossier_id="hms_job_01",
            request_id="kiosk_req_1001",
            facts={
                "patient.full_name": _fact(
                    "patient.full_name",
                    "anita desai",
                    provenance=ProvenanceKind.UPSTREAM_QUEUE,
                    source_id="kiosk_scan_1001",
                ),
                "patient.dob": _fact(
                    "patient.dob",
                    "1991-05-18",
                    provenance=ProvenanceKind.UPSTREAM_QUEUE,
                    source_id="kiosk_scan_1001",
                ),
                "patient.sex": _fact(
                    "patient.sex",
                    "Female",
                    provenance=ProvenanceKind.UPSTREAM_QUEUE,
                    source_id="kiosk_scan_1001",
                ),
                "patient.mobile": _fact(
                    "patient.mobile",
                    "98111 22334",
                    provenance=ProvenanceKind.UPSTREAM_QUEUE,
                    source_id="kiosk_scan_1001",
                ),
                "visit.visit_type": _fact(
                    "visit.visit_type",
                    "New Registration",
                    provenance=ProvenanceKind.UPSTREAM_QUEUE,
                    source_id="kiosk_scan_1001",
                ),
                "visit.department": _fact(
                    "visit.department",
                    "Cardiology",
                    provenance=ProvenanceKind.UPSTREAM_QUEUE,
                    source_id="kiosk_scan_1001",
                ),
                "visit.consulting_doctor": _fact(
                    "visit.consulting_doctor",
                    "Dr. Kavita Rao",
                    provenance=ProvenanceKind.UPSTREAM_QUEUE,
                    source_id="kiosk_scan_1001",
                ),
            },
        ),
        InputDossier(
            dossier_id="hms_job_02_family_phone_collision",
            request_id="kiosk_req_1002",
            facts={
                "patient.full_name": _fact(
                    "patient.full_name",
                    "meera nair",
                    provenance=ProvenanceKind.UPSTREAM_QUEUE,
                    source_id="voice_agent_1002",
                ),
                "patient.dob": _fact(
                    "patient.dob",
                    "2016-08-24",
                    provenance=ProvenanceKind.UPSTREAM_QUEUE,
                    source_id="voice_agent_1002",
                ),
                "patient.sex": _fact(
                    "patient.sex",
                    "Female",
                    provenance=ProvenanceKind.UPSTREAM_QUEUE,
                    source_id="voice_agent_1002",
                ),
                # Collides with existing_phone_records ("9876543210") -> triggers <dialog open>!
                "patient.mobile": _fact(
                    "patient.mobile",
                    "98765-43210",
                    provenance=ProvenanceKind.UPSTREAM_QUEUE,
                    source_id="voice_agent_1002",
                ),
                "visit.visit_type": _fact(
                    "visit.visit_type",
                    "New Registration",
                    provenance=ProvenanceKind.UPSTREAM_QUEUE,
                    source_id="voice_agent_1002",
                ),
                "visit.department": _fact(
                    "visit.department",
                    "Pediatrics",
                    provenance=ProvenanceKind.UPSTREAM_QUEUE,
                    source_id="voice_agent_1002",
                ),
                "visit.consulting_doctor": _fact(
                    "visit.consulting_doctor",
                    "Dr. Ananya Sharma",
                    provenance=ProvenanceKind.UPSTREAM_QUEUE,
                    source_id="voice_agent_1002",
                ),
            },
        ),
    )
