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
    OutputExtractionSpec,
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
MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")
NEGATED_SUFFIX = ".is_no"


def _radio(key: str, name: str) -> FieldBindingSpec:
    """A native Yes/No radio group as one field (an unchecked group reads empty)."""
    return FieldBindingSpec(
        binding_id=f"b_{name}",
        dossier_key=key,
        anchor=_name(name),
        control_kind=ControlKind.RADIO_GROUP,
        codec=BOOL,
        # Radios with no label element give options with an empty label; the
        # default placeholder list ("") would drop every option.
        placeholder_values=("-- Select --",),
    )


def _shown(
    key: str, label: str, codec: FieldCodecSpec = TEXT, *, section: str = ""
) -> FieldBindingSpec:
    """A read-back of a value shown as page text next to ``label``."""
    return FieldBindingSpec(
        binding_id=f"rb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",),
            section_patterns=(section,) if section else (),
        ),
        codec=codec,
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


LOSSES = FieldCodecSpec(
    kind=FormCodecKind.ENUM_MAP,
    enum_aliases={"0": (), "1": (), "2": (), "3": ("3 or more",)},
)

HARBORLINE_PACK = FormPack(
    pack_id="heldout.harborline.home.v2",
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
        _radio("property.has_pool", "pool"),
        _radio("property.pool_fenced", "pool_fenced"),
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
            # The estimator pre-sets its grade; it is overwritten from the intake.
            bindings=(
                _bind(
                    "property.construction_quality",
                    "quality",
                    ControlKind.SELECT,
                    EXACT,
                    portal_default=True,
                ),
            ),
            compute_anchors=(FieldAnchorSpec(label_patterns=(r"^Calculate$",)),),
            commit_anchor=FieldAnchorSpec(label_patterns=(r"^Use This Value$",)),
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
    # The confirmation page shows the stored values as table text.
    summary_check=SummaryCheckSpec(
        summary_landmark_selector="#appno",
        field_readbacks=(
            _shown("applicant.first_name", "First Name"),
            _shown("applicant.last_name", "Last Name"),
            _shown("applicant.date_of_birth", r"Date of Birth \(MM/DD/YYYY\)", DATE_MDY),
            _shown("applicant.phone", "Phone"),
            _shown("applicant.email", "E-mail"),
            _shown("history.prior_policy_number", r"Prior Harborline Policy No\. \(if any\)"),
            _shown("property.address_line1", "Street Address"),
            _shown("property.city", "City"),
            _shown("property.state", "State", EXACT),
            _shown("property.zip", "ZIP Code"),
            _shown("property.year_built", "Year Built", INT),
            _shown("property.square_feet", r"Total Living Area \(sq ft\)", INT),
            _shown("property.stories", "Number of Stories", EXACT),
            _shown("property.construction_class", "Construction Class", EXACT),
            _shown("property.roof_year", r"Roof Covering Last Replaced \(Year\)", INT),
            _shown("property.roof_material", "Roof Covering Material", EXACT),
            _shown("history.prior_claims_5yr", r"Number of Losses \(Last 5 Yrs\)", LOSSES),
            _shown("coverage.dwelling_a", r"Dwelling Coverage \(A\)", MONEY),
            _shown("coverage.effective_date", r"Policy Effective Date \(MM/DD/YYYY\)", DATE_MDY),
            _shown("coverage.deductible", "All Peril Deductible", MONEY),
            _shown("coverage.liability_limit", r"Personal Liability \(E\)", MONEY),
            _shown("property.has_pool", r"Swimming Pool on Premises\?", BOOL),
            _shown("property.pool_fenced", r"Is the pool enclosed by a locking fence\?", BOOL),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="quote.application_no",
                anchor=FieldAnchorSpec(css_selector="#appno"),
                required=False,
            ),
        ),
    ),
    max_commit_mutations=1,
)


# ---------------------------------------------------------------- cobalt
# A single-page app: the step is named by the progress bar, fields by their
# aria-labelledby text. Type-ahead comboboxes fetch suggestions only after
# text is typed. Its radio groups are ARIA radios (div role=radio) named only
# by their own text, which the surface reports with an empty label; each
# option is therefore bound by its position on the dev case's screens and
# set as a yes/no choice from a fact restated from the intake
# ("<key>==<option>"). A position that holds a different control on another
# case does not match, so the portal's own required-field error stops the run.
COBALT_PROFILE = UIProfileSpec(
    profile_id="heldout_spa_aria",
    error_banner_selectors=(".cr-error[role='alert']",),
    field_error_selectors=(".cr-error",),
    auth_wall_selectors=('input[type="password"]',),
    modal_dialog_selectors=('[role="dialog"][aria-modal="true"]',),
)

COBALT_RADIOS: dict[str, tuple[int, int, tuple[str, ...], str]] = {
    # dossier key: (step, field position in the step card, options, shown label)
    "applicant.marital_status": (1, 6, ("Single", "Married", "Divorced", "Widowed"), "Marital status"),
    "vehicle.primary_use": (3, 4, ("Commute", "Pleasure", "Business"), "Primary use"),
    "vehicle.ownership": (3, 8, ("Own", "Finance", "Lease"), "Ownership"),
    "coverage.bi_limits": (4, 1, ("25/50", "50/100", "100/300", "250/500"), "Bodily injury liability"),
}
CHOICE_SEP = "=="


def _cr_step(n: int, next_label: str) -> WizardScreenSpec:
    return WizardScreenSpec(
        screen_id=f"cr_step_{n}",
        landmark_selector=f".progress > div:nth-child({n}).on + div:not(.on)",
        next_button_anchor=FieldAnchorSpec(label_patterns=(f"^{next_label}$",)),
    )


def _label(key: str, label: str, kind: ControlKind = ControlKind.TEXT,
           codec: FieldCodecSpec = TEXT, *, portal_default: bool = False) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"b_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(label_patterns=(f"^{label}$",)),
        control_kind=kind,
        codec=codec,
        # The portal's selects start on "Select one", which has an empty value.
        placeholder_values=("", "Select one"),
        prefill_policy=(
            PrefillPolicy.OVERWRITE_WITH_DOSSIER
            if portal_default
            else PrefillPolicy.HALT_ON_CONFLICT
        ),
    )


def _cr_choice_bindings() -> tuple[FieldBindingSpec, ...]:
    out: list[FieldBindingSpec] = []
    for key, (_step, pos, options, _shown_label) in COBALT_RADIOS.items():
        for j, opt in enumerate(options, start=1):
            out.append(
                FieldBindingSpec(
                    binding_id=f"b_{key}_{j}",
                    dossier_key=f"{key}{CHOICE_SEP}{opt}",
                    anchor=FieldAnchorSpec(
                        css_selector=(
                            f"#app > section > div:nth-of-type({pos}) > "
                            f"div:nth-of-type(2) > div:nth-of-type({j})"
                        ),
                        role="radio",
                    ),
                    control_kind=ControlKind.CHECKBOX,
                    codec=BOOL,
                    # The option's text is its value, so the surface reads every
                    # option as non-empty; a checked state that disagrees with the
                    # intake is set from the intake.
                    prefill_policy=PrefillPolicy.OVERWRITE_WITH_DOSSIER,
                )
            )
    return tuple(out)


def _cr_choice_readbacks() -> tuple[FieldBindingSpec, ...]:
    out: list[FieldBindingSpec] = []
    for key, (_step, _pos, options, shown_label) in COBALT_RADIOS.items():
        for opt in options:
            others = tuple(o for o in options if o != opt)
            out.append(
                _shown(
                    f"{key}{CHOICE_SEP}{opt}",
                    shown_label,
                    FieldCodecSpec(
                        kind=FormCodecKind.ENUM_MAP,
                        enum_aliases={"true": (opt,), "false": others},
                    ),
                )
            )
    return tuple(out)


CR_STATES = {
    "AZ": "Arizona", "CA": "California", "CO": "Colorado", "ID": "Idaho", "IL": "Illinois",
    "MA": "Massachusetts", "MO": "Missouri", "NC": "North Carolina", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "OR": "Oregon", "PA": "Pennsylvania", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "WA": "Washington",
}
CR_STATE = FieldCodecSpec(
    kind=FormCodecKind.ENUM_MAP,
    enum_aliases={code: (f"{name} ({code})",) for code, name in CR_STATES.items()},
)
CR_DED = FieldCodecSpec(kind=FormCodecKind.ENUM_MAP, enum_aliases={
    "0": ("No coverage",), "250": ("$250",), "500": ("$500",), "1000": ("$1,000",),
})

COBALT_PACK = FormPack(
    pack_id="heldout.cobalt.auto.v2",
    domain_schema="personal_lines.auto",
    ui_profile_id=COBALT_PROFILE.profile_id,
    bindings=(
        _label("applicant.first_name", "First name"),
        _label("applicant.last_name", "Last name"),
        _label("applicant.date_of_birth", "Date of birth", ControlKind.DATE,
               FieldCodecSpec(kind=FormCodecKind.ISO_DATE)),
        _label("applicant.email", "Email address"),
        _label("applicant.license_state", "State where licensed",
               ControlKind.PORTAL_COMBOBOX, CR_STATE),
        _label("vehicle.year", "Model year", ControlKind.SELECT, INT),
        _label("vehicle.make", "Make", ControlKind.PORTAL_COMBOBOX, EXACT),
        _label("vehicle.model", "Model", ControlKind.PORTAL_COMBOBOX, EXACT),
        _label("vehicle.annual_miles", "Estimated annual mileage", codec=INT),
        _label("vehicle.commute_miles_one_way", r"One-way commute \(miles\)", codec=INT),
        _label("garaging.zip", "Garaging ZIP code"),
        _label("garaging.city_state", "Garaging city", ControlKind.PORTAL_COMBOBOX, EXACT),
        _label("vehicle.lienholder", "Lienholder or lessor name"),
        _label("coverage.collision_deductible", "Collision deductible", ControlKind.SELECT,
               INT, portal_default=True),
        _label("coverage.comprehensive_deductible", "Comprehensive deductible",
               ControlKind.SELECT, INT, portal_default=True),
        *_cr_choice_bindings(),
    ),
    screens=(
        WizardScreenSpec(
            screen_id="cr_review",
            landmark_selector=".progress > div:nth-child(5).on",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=(r"^Get my quote$",)),
        ),
        _cr_step(1, "Continue"),
        _cr_step(2, "Continue"),
        _cr_step(3, "Continue"),
        _cr_step(4, "Review quote"),
    ),
    summary_check=SummaryCheckSpec(
        summary_landmark_selector="#quote-id",
        field_readbacks=(
            _shown("applicant.first_name", "First name"),
            _shown("applicant.last_name", "Last name"),
            _shown("applicant.date_of_birth", "Date of birth", DATE_MDY),
            _shown("applicant.email", "Email address"),
            _shown("applicant.license_state", "State where licensed", CR_STATE),
            _shown("vehicle.year", "Model year", INT),
            _shown("vehicle.make", "Make", EXACT),
            _shown("vehicle.model", "Model", EXACT),
            _shown("vehicle.annual_miles", "Estimated annual mileage", INT),
            _shown("vehicle.commute_miles_one_way", r"One-way commute \(miles\)", INT),
            _shown("garaging.zip", "Garaging ZIP code"),
            _shown("garaging.city_state", "Garaging city", EXACT),
            _shown("vehicle.lienholder", "Lienholder or lessor name"),
            _shown("coverage.collision_deductible", "Collision deductible", CR_DED),
            _shown("coverage.comprehensive_deductible", "Comprehensive deductible", CR_DED),
            *_cr_choice_readbacks(),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="quote.id",
                anchor=FieldAnchorSpec(css_selector="#quote-id"),
                required=False,
            ),
        ),
    ),
    max_commit_mutations=1,
)


PACKS: dict[str, tuple[FormPack, UIProfileSpec]] = {
    "harborline": (HARBORLINE_PACK, HARBORLINE_PROFILE),
    "cobalt": (COBALT_PACK, COBALT_PROFILE),
}

def _lab(
    key: str,
    label: str,
    kind: ControlKind = ControlKind.TEXT,
    codec: FieldCodecSpec = TEXT,
    *,
    rerender: bool = False,
    portal_default: bool = False,
    **extra: Any,
) -> FieldBindingSpec:
    """A binding anchored on the control's visible label."""
    return FieldBindingSpec(
        binding_id=f"b_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(label_patterns=(f"^{label}$",)),
        control_kind=kind,
        codec=codec,
        triggers_re_render=rerender,
        prefill_policy=(
            PrefillPolicy.OVERWRITE_WITH_DOSSIER
            if portal_default
            else PrefillPolicy.HALT_ON_CONFLICT
        ),
        **extra,
    )


def _group(key: str, name_prefix: str, codec: FieldCodecSpec = EXACT, *,
           rerender: bool = False, portal_default: bool = False) -> FieldBindingSpec:
    """A native radio group whose name starts with ``name_prefix``."""
    return FieldBindingSpec(
        binding_id=f"b_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(name_or_id_patterns=(f"^{name_prefix}",)),
        control_kind=ControlKind.RADIO_GROUP,
        codec=codec,
        triggers_re_render=rerender,
        prefill_policy=(
            PrefillPolicy.OVERWRITE_WITH_DOSSIER
            if portal_default
            else PrefillPolicy.HALT_ON_CONFLICT
        ),
    )


# ---------------------------------------------------------------- pinecrest
# The quote app is a same-origin iframe at /app/ inside the /portal shell. It
# re-renders the whole step on every change (ids and names regenerate), so
# every binding re-snapshots after it writes. The rebuild-cost calculator is
# an in-page dialog: Calculate, then Apply.
PINECREST_PROFILE = UIProfileSpec(
    profile_id="heldout_iframe_rerender",
    error_banner_selectors=(".pc-err[role='alert']", "[role='alert']"),
    field_error_selectors=(".pc-err",),
    auth_wall_selectors=('input[type="password"]', "#pc-relogin"),
    frame_path_patterns=(r"/app/?",),
)


def _pc_step(n: int) -> WizardScreenSpec:
    return WizardScreenSpec(
        screen_id=f"pc_step_{n}",
        landmark_selector=f".tabs span:nth-child({n}).on",
        next_button_anchor=FieldAnchorSpec(label_patterns=(r"^Save and continue$",)),
    )


PC_INT = INT
PINECREST_PACK = FormPack(
    pack_id="heldout.pinecrest.home.v2",
    domain_schema="personal_lines.homeowners",
    ui_profile_id=PINECREST_PROFILE.profile_id,
    bindings=(
        _lab("property.address_line1", "Street address", rerender=True),
        _lab("property.city_state", "City", ControlKind.PORTAL_COMBOBOX, EXACT, rerender=True),
        _lab("property.zip", "ZIP code", rerender=True),
        _lab("property.year_built", "Year built", codec=PC_INT, rerender=True),
        _lab("property.square_feet", "Living area", codec=PC_INT, rerender=True),
        _lab("property.area_unit", "Area unit", ControlKind.SELECT, EXACT, rerender=True),
        _lab("property.construction_type", "Construction type", ControlKind.SELECT, EXACT, rerender=True),
        _group("property.stories", "stories", rerender=True),
        _lab("property.roof_year", "Year roof was installed", codec=PC_INT, rerender=True),
        _lab("property.roof_material", "Roof material", ControlKind.SELECT, EXACT, rerender=True),
        _lab("property.heating_type", "Primary heating", ControlKind.SELECT, EXACT, rerender=True),
        _group("property.wood_stove_professionally_installed", "stove_installed", rerender=True),
        _lab("history.prior_claims_5yr", "Claims in the last 5 years", codec=PC_INT, rerender=True),
        _lab("property.burglar_alarm", "Monitored burglar alarm", ControlKind.CHECKBOX, BOOL,
             rerender=True, portal_default=True),
        _group("coverage.deductible", "deductible", INT, rerender=True, portal_default=True),
        _lab("coverage.effective_date", "Policy start date", ControlKind.DATE,
             FieldCodecSpec(kind=FormCodecKind.ISO_DATE), rerender=True),
        FieldBindingSpec(
            binding_id="b_dwelling_launcher",
            dossier_key="coverage.dwelling_limit",
            anchor=FieldAnchorSpec(label_patterns=(r"^Calculate rebuild cost$",)),
            control_kind=ControlKind.POPUP_LAUNCHER,
        ),
    ),
    popup_subroutines=(
        PopupSubroutineSpec(
            subroutine_id="pc_rebuild",
            popup_mode="in_page",
            trigger_anchor=FieldAnchorSpec(label_patterns=(r"^Calculate rebuild cost$",)),
            bindings=(
                _lab("property.square_feet", r"Living area \(sq ft\)", codec=PC_INT),
                _lab("property.year_built", "Year built", codec=PC_INT),
                _lab("property.construction_type", "Construction type", ControlKind.SELECT, EXACT),
                _lab("property.finish_level", "Finish level", ControlKind.SELECT, EXACT),
            ),
            compute_anchors=(FieldAnchorSpec(label_patterns=(r"^Calculate$",)),),
            commit_anchor=FieldAnchorSpec(label_patterns=(r"^Apply to dwelling limit$",)),
            parent_return_anchor=FieldAnchorSpec(label_patterns=(r"^Dwelling limit$",)),
            return_output_key="coverage.dwelling_limit",
            return_codec=INT,
        ),
    ),
    screens=(
        WizardScreenSpec(
            screen_id="pc_review",
            landmark_selector="#pc-submit",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=(r"^Submit for quote$",)),
        ),
        _pc_step(1),
        _pc_step(2),
        _pc_step(3),
    ),
    summary_check=SummaryCheckSpec(
        summary_landmark_selector="#pc-ref",
        field_readbacks=(
            _shown("property.address_line1", "Street address"),
            _shown("property.city_state", "City", EXACT),
            _shown("property.zip", "ZIP code"),
            _shown("property.year_built", "Year built", INT),
            _shown("property.construction_type", "Construction type", EXACT),
            _shown("property.stories", "Stories", EXACT),
            _shown("property.roof_year", "Year roof was installed", INT),
            _shown("property.roof_material", "Roof material", EXACT),
            _shown("property.heating_type", "Primary heating", EXACT),
            _shown("property.wood_stove_professionally_installed",
                   r"Was the wood stove installed by a licensed professional\?", EXACT),
            _shown("history.prior_claims_5yr", "Claims in the last 5 years", INT),
            _shown("property.burglar_alarm", "Monitored burglar alarm", BOOL),
            _shown("coverage.dwelling_limit", "Dwelling limit", MONEY),
            _shown("coverage.deductible", "Deductible", MONEY),
            _shown("coverage.effective_date", "Policy start date", DATE_MDY),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="quote.reference",
                anchor=FieldAnchorSpec(css_selector="#pc-ref"),
                required=False,
            ),
        ),
    ),
    max_commit_mutations=1,
)


# ---------------------------------------------------------------- tidewater
# Server-rendered pages whose fields are custom elements with open shadow
# roots. Next posts a hidden form; a session expiry redirects to /signin.
TIDEWATER_PROFILE = UIProfileSpec(
    profile_id="heldout_shadow_postback",
    error_banner_selectors=("p.bad[role='alert']",),
    field_error_selectors=(".err",),
    auth_wall_selectors=('input[type="password"]',),
)


def _tw_step(n: int) -> WizardScreenSpec:
    return WizardScreenSpec(
        screen_id=f"tw_step_{n}",
        landmark_selector=f"form#tg-post[action='/qq/{n}']",
        next_button_anchor=FieldAnchorSpec(label_patterns=(r"^Next$",)),
    )


TW_LOSSES = FieldCodecSpec(
    kind=FormCodecKind.ENUM_MAP,
    enum_aliases={"0": ("None",), "1": (), "2": (), "3": ("3 or more",)},
)

TIDEWATER_PACK = FormPack(
    pack_id="heldout.tidewater.bundle.v2",
    domain_schema="personal_lines.bundle",
    ui_profile_id=TIDEWATER_PROFILE.profile_id,
    bindings=(
        _lab("applicant.first_name", "First name"),
        _lab("applicant.last_name", "Last name"),
        _lab("applicant.date_of_birth", r"Date of birth \(MM/DD/YYYY\)", ControlKind.DATE, DATE_MDY),
        _lab("applicant.email", "Email"),
        _lab("property.address_line1", "Street address"),
        _lab("property.address_line2", "Apt / unit"),
        _lab("property.city", "City"),
        _lab("property.state", "State", ControlKind.SELECT, EXACT),
        _lab("property.zip", "ZIP"),
        _lab("property.year_built", "Year built", codec=INT),
        _lab("property.square_feet", "Square footage", codec=INT),
        _lab("property.roof_year", r"Roof updated \(year\)", codec=INT),
        _lab("property.occupancy", "Occupancy", ControlKind.SELECT, EXACT),
        _lab("history.prior_claims_3yr", "Losses in the last 3 years", ControlKind.SELECT, INT),
        _lab("vehicle.year", "Vehicle year", ControlKind.SELECT, INT),
        _lab("vehicle.make", "Make", ControlKind.PORTAL_COMBOBOX, EXACT),
        _lab("vehicle.model", "Model", ControlKind.PORTAL_COMBOBOX, EXACT),
        _lab("household.driver_count", "Drivers in household", ControlKind.SELECT, INT),
    ),
    screens=(
        WizardScreenSpec(
            screen_id="tw_review",
            landmark_selector="form[action='/qq/review']",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=(r"^Submit quote request$",)),
        ),
        _tw_step(1),
        _tw_step(2),
        _tw_step(3),
    ),
    summary_check=SummaryCheckSpec(
        summary_landmark_selector="#quote-no",
        field_readbacks=(
            _shown("applicant.first_name", "First name"),
            _shown("applicant.last_name", "Last name"),
            _shown("applicant.date_of_birth", r"Date of birth \(MM/DD/YYYY\)", DATE_MDY),
            _shown("applicant.email", "Email"),
            _shown("property.address_line1", "Street address", EXACT),
            _shown("property.address_line2", "Apt / unit", EXACT),
            _shown("property.city", "City", EXACT),
            _shown("property.state", "State", EXACT),
            _shown("property.zip", "ZIP"),
            _shown("property.year_built", "Year built", INT),
            _shown("property.square_feet", "Square footage", INT),
            _shown("property.roof_year", r"Roof updated \(year\)", INT),
            _shown("property.occupancy", "Occupancy", EXACT),
            _shown("history.prior_claims_3yr", "Losses in the last 3 years", TW_LOSSES),
            _shown("vehicle.year", "Vehicle year", INT),
            _shown("vehicle.make", "Make", EXACT),
            _shown("vehicle.model", "Model", EXACT),
            _shown("household.driver_count", "Drivers in household", INT),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="quote.number",
                anchor=FieldAnchorSpec(css_selector="#quote-no"),
                required=False,
            ),
        ),
    ),
    max_commit_mutations=1,
)


PACKS["pinecrest"] = (PINECREST_PACK, PINECREST_PROFILE)
PACKS["tidewater"] = (TIDEWATER_PACK, TIDEWATER_PROFILE)


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
    derived = _derived_facts(intake["facts"])
    for key, value in derived.items():
        if key not in facts:
            facts[key] = DossierFact(
                key=key,
                value=value,
                provenance=ProvenanceKind.INTAKE_DOCUMENT,
                source_id=f"intake:{intake['case_id']}",
                confidence=1.0,
            )
    return InputDossier(dossier_id=f"intake:{intake['case_id']}", request_id=request_id, facts=facts)


def _derived_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Facts restated from the intake in the form a portal shows them.

    A city type-ahead lists "City, ST"; a living area typed as square feet
    is in the "sq ft" unit. Nothing here adds information the intake lacks.
    """
    out: dict[str, Any] = {}
    if raw.get("property.city") and raw.get("property.state"):
        out["property.city_state"] = f"{raw['property.city']}, {raw['property.state']}"
    if raw.get("garaging.city") and raw.get("garaging.state"):
        out["garaging.city_state"] = f"{raw['garaging.city']}, {raw['garaging.state']}"
    for key, (_s, _p, options, _l) in COBALT_RADIOS.items():
        if raw.get(key) not in (None, ""):
            picked = str(raw[key]).casefold()
            for opt in options:
                out[f"{key}{CHOICE_SEP}{opt}"] = opt.casefold() == picked
    if raw.get("property.square_feet") not in (None, ""):
        out["property.area_unit"] = "sq ft"
    return out


def _intake_value(value: Any) -> Any:
    return value


# The first run's post-hoc pack P1 (estimator grade overwritten from the
# intake) is part of the v2 pack; the name is kept for run.py --posthoc.
HARBORLINE_PACK_POSTHOC_P1 = HARBORLINE_PACK
