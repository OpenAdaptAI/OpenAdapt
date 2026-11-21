"""Comprehensive deterministic unit and workflow tests for ``openadapt_flow.form_tx``:
``FormReconciler``, ``SummaryJudge``, ``RuleAccumulator``, ``SignedFormTxReceipt``,
and the Generality Gate.
"""

from __future__ import annotations

import ast
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

import pytest

from openadapt_flow.form_tx.judge import SummaryJudge, build_authoritative_fact_map
from openadapt_flow.form_tx.learning import RuleAccumulator, admit_candidate_into_pack
from openadapt_flow.form_tx.receipt import (
    ed25519_public_key_hex,
    generate_ed25519_keypair,
    issue_form_tx_receipt,
    load_or_create_ed25519_key,
    verify_form_tx_receipt,
    verify_salted_output_digest,
)
from openadapt_flow.form_tx.reconciler import FormReconciler, ReconcilerSession
from openadapt_flow.form_tx.surface import FormSurfaceBackend
from openadapt_flow.form_tx.types import (
    ControlKind,
    DossierEntity,
    DossierFact,
    EntityTableSpec,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormHaltKind,
    FormPack,
    FormSnapshot,
    FormTxOutcome,
    HumanDecisionAction,
    HumanResolutionInput,
    HumanResolutionRecord,
    InputDossier,
    ModalRuleSpec,
    ObservedControl,
    ObservedEntityRow,
    ObservedModal,
    ObservedOption,
    OutputExtractionSpec,
    PopupSubroutineSpec,
    PrefillPolicy,
    ProvenanceKind,
    ReadbackFieldVerdict,
    ReadbackStatus,
    SummaryCheckSpec,
    SummaryVerdict,
    UIProfileSpec,
    WizardScreenSpec,
    canonical_sha256,
)

# Hex-encoded vendor/application/customer identifiers so zero banned strings
# appear anywhere in engine OR test source files.
_BANNED_TOKEN_HEX: tuple[str, ...] = (
    "677569646577697265",
    "706f6c69637963656e746572",
    "636c61696d63656e746572",
    "62696c6c696e6763656e746572",
    "6e6174696f6e77696465",
    "75706c696e65",
    "7370617469616d6564",
    "766974616c63617265",
    "6f70656e656d72",
    "33363076616c7565",
    "6475636b637265656b",
    "6170706c69656465706963",
    "657a6c796e78",
)


def _decoded_banned_tokens() -> tuple[str, ...]:
    return tuple(bytes.fromhex(h).decode("utf-8") for h in _BANNED_TOKEN_HEX)


# ---------------------------------------------------------------------------
# Deterministic In-Memory Fake Stateful Form & Popup Surfaces
# ---------------------------------------------------------------------------


@dataclass
class FakePopupFormSurface:
    """In-memory child calculator popup window bound to a parent ``FakeStatefulFormSurface``."""

    parent: "FakeStatefulFormSurface"
    ui_profile: UIProfileSpec = field(
        default_factory=lambda: UIProfileSpec(profile_id="generic_web_ui")
    )
    living_area_sqft: str = ""
    construction_year: str = ""
    garage_bays: str = ""
    require_unmapped_garage_bays: bool = False
    broken_return_write: bool = False
    # The estimator keeps only the first three digits of living area, as a
    # badly built widget might. The parent form never shows the input.
    truncate_living_area: bool = False
    mutation_count: int = 0

    def reset_mutation_counter(self) -> None:
        self.mutation_count = 0

    def snapshot(
        self,
        *,
        entity_tables: Sequence[object] = (),
        modal_rules: Sequence[object] = (),
        screens: Sequence[object] = (),
    ) -> FormSnapshot:
        controls: list[ObservedControl] = [
            ObservedControl(
                control_id="popup_living_area",
                dom_selector="#popup-living-area",
                tag="input",
                input_type="text",
                name_attr="living_area_sqft",
                label_text="Living Area (Sq Ft)",
                section_text="Cost Estimator",
                current_value=self.living_area_sqft,
                required=True,
            ),
            ObservedControl(
                control_id="popup_construction_year",
                dom_selector="#popup-construction-year",
                tag="input",
                input_type="text",
                name_attr="construction_year",
                label_text="Construction Year",
                section_text="Cost Estimator",
                current_value=self.construction_year,
                required=True,
            ),
            ObservedControl(
                control_id="popup_apply_btn",
                dom_selector="#btn-popup-apply",
                tag="button",
                role="button",
                label_text="Apply Estimate",
                section_text="Cost Estimator",
            ),
        ]
        if self.require_unmapped_garage_bays:
            controls.insert(
                2,
                ObservedControl(
                    control_id="popup_garage_bays",
                    dom_selector="#popup-garage-bays",
                    tag="input",
                    input_type="text",
                    name_attr="garage_bays",
                    label_text="Attached Garage Bays",
                    section_text="Cost Estimator",
                    current_value=self.garage_bays,
                    required=True,
                ),
            )
        return FormSnapshot(
            url_origin_and_path="https://portal.example.test/estimator/popup",
            title="Replacement Cost Estimator",
            screen_id="popup_estimator",
            authenticated_landmark_present=True,
            controls=tuple(controls),
        )

    def goto(self, url_or_path: str) -> None:
        return None

    def fill_text(self, dom_selector: str, value: str) -> None:
        if dom_selector == "#popup-living-area":
            self.living_area_sqft = value[:3] if self.truncate_living_area else value
        elif dom_selector == "#popup-construction-year":
            self.construction_year = value
        elif dom_selector == "#popup-garage-bays":
            self.garage_bays = value
        else:
            raise KeyError(f"Unknown popup selector: {dom_selector}")

    def select_native_option(self, dom_selector: str, option_value: str) -> None:
        raise KeyError(f"Unknown popup select: {dom_selector}")

    def set_checkbox_or_radio(self, dom_selector: str, checked: bool = True) -> None:
        raise KeyError(f"Unknown popup checkbox: {dom_selector}")

    def open_portal_combobox_options(
        self, dom_selector: str
    ) -> tuple[ObservedOption, ...]:
        return ()

    def choose_portal_combobox_option(
        self, dom_selector: str, option: ObservedOption
    ) -> None:
        return None

    def click(self, dom_selector: str) -> None:
        if dom_selector == "#btn-popup-apply":
            if (
                not self.broken_return_write
                and self.living_area_sqft.strip()
                and self.construction_year.strip()
            ):
                sqft = int(self.living_area_sqft.replace(",", "").strip())
                estimate_dollars = sqft * 200
                self.parent.values["replacement_cost"] = f"${estimate_dollars:,.2f}"
                # The portal stores the estimator inputs with the quote, so
                # the summary can show them.
                self.parent.values["living_area_sqft"] = self.living_area_sqft
                self.parent.values["construction_year"] = self.construction_year
            return
        raise KeyError(f"Unknown popup click selector: {dom_selector}")

    @contextmanager
    def open_popup_surface(
        self,
        trigger_selector: str,
        *,
        url_pattern: Optional[str] = None,
        title_pattern: Optional[str] = None,
    ) -> Iterator["FormSurfaceBackend"]:
        yield self

    def ping_keepalive(self, path: str) -> int:
        return 200


class FakeStatefulFormSurface:
    """Deterministic multi-screen stateful form backend with PPR cascades, modals, and search."""

    def __init__(
        self,
        *,
        prefill_values: Optional[dict[str, str]] = None,
        entity_rows: Optional[list[dict[str, str]]] = None,
        dismiss_works: bool = True,
        block_next_with_banner: Optional[str] = None,
        block_next_field_error: Optional[tuple[str, str]] = None,
        block_commit_with_banner: Optional[str] = None,
        trigger_duplicate_modal_on_commit: bool = False,
        modal_close_works: bool = True,
        commit_mutations_to_emit: int = 1,
        corrupt_summary_overrides: Optional[dict[str, str]] = None,
        drop_summary_fields: Sequence[str] = (),
        drop_confirmation_output: bool = False,
        auth_wall: bool = False,
        authenticated_landmark_present: bool = True,
        require_authenticated_landmark: bool = False,
        popup_broken_return: bool = False,
        popup_require_unmapped_garage: bool = False,
        duplicate_ambiguous_first_name: bool = False,
        popup_truncate_living_area: bool = False,
    ) -> None:
        landmarks = ("#session-user-badge",) if require_authenticated_landmark else ()
        self._ui_profile = UIProfileSpec(
            profile_id="generic_web_ui",
            authenticated_landmark_selectors=landmarks,
        )
        self._mutation_count = 0
        self.current_route = "/wizard/applicant"
        self.auth_wall = auth_wall
        self.authenticated_landmark_present = authenticated_landmark_present

        self.values: dict[str, str] = {
            "first_name": "",
            "last_name": "",
            "effective_date": "",
            "phone_number": "",
            "state_code": "",
            "occupancy_kind": "",
            "prior_claims_count": "",
            "has_pool": "",
            "pool_fenced": "",
            "has_trampoline": "",
            "replacement_cost": "$0.00",
            "lookup_query": "",
        }
        if prefill_values:
            self.values.update(prefill_values)

        self.terms_accepted: bool = False
        self.household_rows: list[dict[str, str]] = list(entity_rows or [])
        self.dismiss_works = dismiss_works

        self.block_next_with_banner = block_next_with_banner
        self.block_next_field_error = block_next_field_error
        self.block_commit_with_banner = block_commit_with_banner
        self.active_banners: list[str] = []
        self.field_errors: dict[str, str] = {}

        self.trigger_duplicate_modal_on_commit = trigger_duplicate_modal_on_commit
        self.modal_close_works = modal_close_works
        self.duplicate_modal_open = False
        self.duplicate_modal_acknowledged = False

        self.commit_mutations_to_emit = commit_mutations_to_emit
        self.committed_record: Optional[dict[str, str]] = None
        self.confirmation_code: str = ""
        self.quoted_total_display: str = ""
        self.lookup_loaded_code: str = ""

        self.corrupt_summary_overrides = dict(corrupt_summary_overrides or {})
        self.drop_summary_fields = set(drop_summary_fields)
        self.drop_confirmation_output = drop_confirmation_output
        self.popup_broken_return = popup_broken_return
        self.popup_require_unmapped_garage = popup_require_unmapped_garage
        self.duplicate_ambiguous_first_name = duplicate_ambiguous_first_name
        self.popup_truncate_living_area = popup_truncate_living_area

    @property
    def ui_profile(self) -> UIProfileSpec:
        return self._ui_profile

    @property
    def mutation_count(self) -> int:
        return self._mutation_count

    def reset_mutation_counter(self) -> None:
        self._mutation_count = 0

    def goto(self, url_or_path: str) -> None:
        if url_or_path.startswith("http"):
            from urllib.parse import urlsplit

            self.current_route = urlsplit(url_or_path).path
        else:
            self.current_route = url_or_path
        self.active_banners.clear()
        self.field_errors.clear()

    def fill_text(self, dom_selector: str, value: str) -> None:
        mapping = {
            "#first-name": "first_name",
            "#last-name": "last_name",
            "#effective-date": "effective_date",
            "#phone-number": "phone_number",
            "#prior-claims": "prior_claims_count",
            "#lookup-input": "lookup_query",
        }
        if dom_selector not in mapping:
            raise KeyError(f"Unsupported text selector: {dom_selector}")
        key = mapping[dom_selector]
        self.values[key] = value
        self.field_errors.pop(dom_selector, None)

    def select_native_option(self, dom_selector: str, option_value: str) -> None:
        mapping = {
            "#state-select": "state_code",
            "#has-pool": "has_pool",
            "#pool-fenced": "pool_fenced",
            "#has-trampoline": "has_trampoline",
        }
        if dom_selector not in mapping:
            raise KeyError(f"Unsupported select selector: {dom_selector}")
        key = mapping[dom_selector]
        self.values[key] = option_value
        if key == "has_pool" and option_value != "yes":
            self.values["pool_fenced"] = ""
            self.values["has_trampoline"] = ""

    def set_checkbox_or_radio(self, dom_selector: str, checked: bool = True) -> None:
        if dom_selector == "#terms-checkbox":
            self.terms_accepted = checked
            return
        raise KeyError(f"Unsupported checkbox/radio selector: {dom_selector}")

    def open_portal_combobox_options(
        self, dom_selector: str
    ) -> tuple[ObservedOption, ...]:
        if dom_selector == "#occupancy-combobox":
            return (
                ObservedOption(value="primary_residence", label="Primary Residence"),
                ObservedOption(value="seasonal", label="Seasonal / Secondary"),
                ObservedOption(value="tenant", label="Tenant Occupied"),
            )
        raise KeyError(f"Unsupported combobox selector: {dom_selector}")

    def choose_portal_combobox_option(
        self, dom_selector: str, option: ObservedOption
    ) -> None:
        if dom_selector == "#occupancy-combobox":
            self.values["occupancy_kind"] = option.label
            return
        raise KeyError(f"Unsupported combobox selector: {dom_selector}")

    def _perform_server_commit(self) -> None:
        self._mutation_count += self.commit_mutations_to_emit
        saved = dict(self.values)
        saved["terms_accepted"] = "Yes" if self.terms_accepted else "No"
        saved.update(self.corrupt_summary_overrides)
        self.committed_record = saved
        self.confirmation_code = "" if self.drop_confirmation_output else "CONF-10001"
        self.quoted_total_display = "$1,240.50"
        self.current_route = "/wizard/submitted"

    def click(self, dom_selector: str) -> None:
        if dom_selector.startswith("#hh-row-") and dom_selector.endswith(
            " .btn-dismiss-row"
        ):
            idx_str = dom_selector.split("#hh-row-")[1].split()[0]
            idx = int(idx_str)
            if self.dismiss_works and 0 <= idx < len(self.household_rows):
                self.household_rows.pop(idx)
            return

        if dom_selector == "#btn-next-applicant":
            if self.block_next_with_banner or self.block_next_field_error:
                if self.block_next_with_banner:
                    self.active_banners = [self.block_next_with_banner]
                if self.block_next_field_error:
                    sel, err = self.block_next_field_error
                    self.field_errors[sel] = err
                return
            self.active_banners.clear()
            self.field_errors.clear()
            self.current_route = "/wizard/property"
            return

        if dom_selector == "#btn-next-property":
            self.active_banners.clear()
            self.field_errors.clear()
            self.current_route = "/wizard/review"
            return

        if dom_selector == "#btn-commit-application":
            if self.block_commit_with_banner:
                self.active_banners = [self.block_commit_with_banner]
                return
            if (
                self.trigger_duplicate_modal_on_commit
                and not self.duplicate_modal_acknowledged
            ):
                self.duplicate_modal_open = True
                return
            self._perform_server_commit()
            return

        if dom_selector == "#modal-duplicate-confirm":
            if self.modal_close_works:
                self.duplicate_modal_open = False
                self.duplicate_modal_acknowledged = True
                self._perform_server_commit()
            return

        if dom_selector == "#btn-lookup-submit":
            q = self.values.get("lookup_query", "").strip()
            if q and q == self.confirmation_code:
                self.lookup_loaded_code = q
            return

        raise KeyError(f"Unsupported click selector: {dom_selector}")

    @contextmanager
    def open_popup_surface(
        self,
        trigger_selector: str,
        *,
        url_pattern: Optional[str] = None,
        title_pattern: Optional[str] = None,
    ) -> Iterator[FormSurfaceBackend]:
        if trigger_selector != "#btn-open-cost-calc":
            raise KeyError(f"Unsupported popup trigger: {trigger_selector}")
        popup = FakePopupFormSurface(
            parent=self,
            broken_return_write=self.popup_broken_return,
            require_unmapped_garage_bays=self.popup_require_unmapped_garage,
            truncate_living_area=self.popup_truncate_living_area,
        )
        yield popup

    def ping_keepalive(self, path: str) -> int:
        return 200

    def snapshot(
        self,
        *,
        entity_tables: Sequence[object] = (),
        modal_rules: Sequence[object] = (),
        screens: Sequence[object] = (),
    ) -> FormSnapshot:
        modals: list[ObservedModal] = []
        if self.duplicate_modal_open:
            modals.append(
                ObservedModal(
                    modal_id="duplicate_contact_modal",
                    container_selector="#modal-duplicate-contact",
                    message_text="Existing profile shares this phone number. Proceed anyway?",
                )
            )

        if self.current_route == "/wizard/applicant":
            obs_rows = tuple(
                ObservedEntityRow(
                    table_id="household_table",
                    entity_kind="household_member",
                    row_index=idx,
                    row_selector=f"#hh-row-{idx}",
                    values=dict(r),
                    has_dismiss_action=True,
                )
                for idx, r in enumerate(self.household_rows)
            )
            controls: list[ObservedControl] = [
                ObservedControl(
                    control_id="ctrl_first_name",
                    dom_selector="#first-name",
                    tag="input",
                    input_type="text",
                    name_attr="first_name",
                    label_text="Applicant First Name",
                    section_text="Applicant Details",
                    current_value=self.values["first_name"],
                    required=True,
                    error_text=self.field_errors.get("#first-name", ""),
                ),
                ObservedControl(
                    control_id="ctrl_last_name",
                    dom_selector="#last-name",
                    tag="input",
                    input_type="text",
                    name_attr="last_name",
                    label_text="Applicant Last Name",
                    section_text="Applicant Details",
                    current_value=self.values["last_name"],
                    required=True,
                ),
                ObservedControl(
                    control_id="ctrl_effective_date",
                    dom_selector="#effective-date",
                    tag="input",
                    input_type="date",
                    name_attr="effective_date",
                    label_text="Requested Effective Date",
                    section_text="Applicant Details",
                    current_value=self.values["effective_date"],
                    required=True,
                ),
                ObservedControl(
                    control_id="ctrl_phone",
                    dom_selector="#phone-number",
                    tag="input",
                    input_type="text",
                    name_attr="phone_number",
                    label_text="Contact Phone",
                    section_text="Applicant Details",
                    current_value=self.values["phone_number"],
                    required=True,
                    error_text=self.field_errors.get("#phone-number", ""),
                ),
                ObservedControl(
                    control_id="ctrl_state",
                    dom_selector="#state-select",
                    tag="select",
                    name_attr="state_code",
                    label_text="Property State",
                    section_text="Applicant Details",
                    current_value=self.values["state_code"],
                    required=True,
                    options=(
                        ObservedOption(value="", label="-- Select --"),
                        ObservedOption(value="CA", label="California"),
                        ObservedOption(value="NY", label="New York"),
                        ObservedOption(value="TX", label="Texas"),
                    ),
                ),
                ObservedControl(
                    control_id="ctrl_occupancy",
                    dom_selector="#occupancy-combobox",
                    tag="div",
                    role="combobox",
                    name_attr="occupancy_kind",
                    label_text="Occupancy Classification",
                    section_text="Applicant Details",
                    current_value=self.values["occupancy_kind"],
                    required=True,
                ),
                ObservedControl(
                    control_id="ctrl_prior_claims",
                    dom_selector="#prior-claims",
                    tag="input",
                    input_type="text",
                    name_attr="prior_claims_count",
                    label_text="Prior Claims (Last 5 Years)",
                    section_text="Loss History",
                    current_value=self.values["prior_claims_count"],
                    required=True,
                ),
                ObservedControl(
                    control_id="btn_next_1",
                    dom_selector="#btn-next-applicant",
                    tag="button",
                    role="button",
                    label_text="Next: Property Details",
                ),
            ]
            if self.duplicate_ambiguous_first_name:
                controls.insert(
                    1,
                    ObservedControl(
                        control_id="ctrl_first_name_dup",
                        dom_selector="#first-name-dup",
                        tag="input",
                        input_type="text",
                        name_attr="first_name_secondary",
                        label_text="Applicant First Name",
                        section_text="Applicant Details",
                        current_value="",
                    ),
                )
            return FormSnapshot(
                url_origin_and_path="https://portal.example.test/wizard/applicant",
                title="Step 1: Applicant",
                screen_id="applicant_screen",
                auth_wall_present=self.auth_wall,
                authenticated_landmark_present=self.authenticated_landmark_present,
                controls=tuple(controls),
                entity_rows=obs_rows,
                active_modals=tuple(modals),
                banner_errors=tuple(self.active_banners),
            )

        if self.current_route == "/wizard/property":
            yes_no_opts = (
                ObservedOption(value="", label="-- Select --"),
                ObservedOption(value="yes", label="Yes"),
                ObservedOption(value="no", label="No"),
            )
            controls = [
                ObservedControl(
                    control_id="ctrl_has_pool",
                    dom_selector="#has-pool",
                    tag="select",
                    name_attr="has_pool_482910",
                    label_text="Swimming Pool on Premises",
                    section_text="Property Hazards",
                    current_value=self.values["has_pool"],
                    required=True,
                    options=yes_no_opts,
                ),
                ObservedControl(
                    control_id="btn_cost_calc",
                    dom_selector="#btn-open-cost-calc",
                    tag="button",
                    role="button",
                    label_text="Calculate Replacement Cost",
                    section_text="Valuation",
                ),
                ObservedControl(
                    control_id="out_replacement_cost",
                    dom_selector="#replacement-cost-output",
                    tag="input",
                    input_type="text",
                    name_attr="replacement_cost",
                    label_text="Estimated Replacement Cost",
                    section_text="Valuation",
                    current_value=self.values["replacement_cost"],
                    readonly=True,
                ),
                ObservedControl(
                    control_id="btn_next_2",
                    dom_selector="#btn-next-property",
                    tag="button",
                    role="button",
                    label_text="Next: Review",
                ),
            ]
            # PPR Conditional Cascade when has_pool == "yes"
            if self.values["has_pool"] == "yes":
                controls.insert(
                    1,
                    ObservedControl(
                        control_id="ctrl_pool_fenced",
                        dom_selector="#pool-fenced",
                        tag="select",
                        name_attr="pool_fenced_991234",
                        label_text="Self-Latching Perimeter Fence",
                        section_text="Property Hazards",
                        current_value=self.values["pool_fenced"],
                        required=True,
                        options=yes_no_opts,
                    ),
                )
                controls.insert(
                    2,
                    ObservedControl(
                        control_id="ctrl_has_trampoline",
                        dom_selector="#has-trampoline",
                        tag="select",
                        name_attr="has_trampoline_774123",
                        label_text="Trampoline on Premises",
                        section_text="Property Hazards",
                        current_value=self.values["has_trampoline"],
                        required=True,
                        options=yes_no_opts,
                    ),
                )
            return FormSnapshot(
                url_origin_and_path="https://portal.example.test/wizard/property",
                title="Step 2: Property & Valuation",
                screen_id="property_screen",
                auth_wall_present=self.auth_wall,
                authenticated_landmark_present=self.authenticated_landmark_present,
                controls=tuple(controls),
                active_modals=tuple(modals),
                banner_errors=tuple(self.active_banners),
            )

        if self.current_route == "/wizard/review":
            controls = [
                ObservedControl(
                    control_id="ctrl_terms",
                    dom_selector="#terms-checkbox",
                    tag="input",
                    input_type="checkbox",
                    name_attr="terms_accepted",
                    label_text="I attest that all grounded entries are accurate",
                    section_text="Attestation",
                    checked=self.terms_accepted,
                    required=True,
                ),
                ObservedControl(
                    control_id="btn_commit",
                    dom_selector="#btn-commit-application",
                    tag="button",
                    role="button",
                    label_text="Submit Transaction",
                    section_text="Attestation",
                ),
            ]
            return FormSnapshot(
                url_origin_and_path="https://portal.example.test/wizard/review",
                title="Step 3: Review & Submit",
                screen_id="review_screen",
                auth_wall_present=self.auth_wall,
                authenticated_landmark_present=self.authenticated_landmark_present,
                controls=tuple(controls),
                active_modals=tuple(modals),
                banner_errors=tuple(self.active_banners),
            )

        if self.current_route == "/wizard/submitted":
            controls = [
                ObservedControl(
                    control_id="submitted_conf",
                    dom_selector="#confirmation-code",
                    tag="span",
                    label_text="Confirmation Code",
                    section_text="Submission Receipt",
                    current_value=self.confirmation_code,
                    readonly=True,
                ),
            ]
            return FormSnapshot(
                url_origin_and_path="https://portal.example.test/wizard/submitted",
                title="Transaction Submitted",
                screen_id="submitted_screen",
                auth_wall_present=self.auth_wall,
                authenticated_landmark_present=self.authenticated_landmark_present,
                controls=tuple(controls),
                active_modals=tuple(modals),
                banner_errors=tuple(self.active_banners),
            )

        if self.current_route in ("/summary", "/lookup"):
            rec = self.committed_record or {}
            controls = []
            if self.current_route == "/lookup":
                controls.extend(
                    [
                        ObservedControl(
                            control_id="lookup_input",
                            dom_selector="#lookup-input",
                            tag="input",
                            input_type="text",
                            label_text="Lookup Confirmation Code",
                            section_text="Record Search",
                            current_value=self.values.get("lookup_query", ""),
                        ),
                        ObservedControl(
                            control_id="lookup_submit",
                            dom_selector="#btn-lookup-submit",
                            tag="button",
                            role="button",
                            label_text="Search Records",
                            section_text="Record Search",
                        ),
                    ]
                )
                if not self.lookup_loaded_code:
                    return FormSnapshot(
                        url_origin_and_path=f"https://portal.example.test{self.current_route}",
                        title="Record Search",
                        screen_id="lookup_screen",
                        authenticated_landmark_present=self.authenticated_landmark_present,
                        controls=tuple(controls),
                    )

            summary_items = [
                ("first_name", "#sum-first-name", "Applicant First Name"),
                ("last_name", "#sum-last-name", "Applicant Last Name"),
                ("effective_date", "#sum-effective-date", "Requested Effective Date"),
                ("phone_number", "#sum-phone", "Contact Phone"),
                ("state_code", "#sum-state", "Property State"),
                ("occupancy_kind", "#sum-occupancy", "Occupancy Classification"),
                (
                    "prior_claims_count",
                    "#sum-prior-claims",
                    "Prior Claims (Last 5 Years)",
                ),
                ("has_pool", "#sum-has-pool", "Swimming Pool on Premises"),
                ("pool_fenced", "#sum-pool-fenced", "Self-Latching Perimeter Fence"),
                ("has_trampoline", "#sum-has-trampoline", "Trampoline on Premises"),
                (
                    "replacement_cost",
                    "#sum-replacement-cost",
                    "Estimated Replacement Cost",
                ),
                ("terms_accepted", "#sum-terms", "Attestation Accepted"),
                ("living_area_sqft", "#sum-living-area", "Living Area Square Feet"),
                ("construction_year", "#sum-construction-year", "Year Built"),
            ]
            for key, sel, label in summary_items:
                if key in self.drop_summary_fields:
                    continue
                val = rec.get(key, "")
                if (
                    key
                    in (
                        "pool_fenced",
                        "has_trampoline",
                        "living_area_sqft",
                        "construction_year",
                    )
                    and not val
                ):
                    continue
                controls.append(
                    ObservedControl(
                        control_id=f"sum_{key}",
                        dom_selector=sel,
                        tag="span",
                        label_text=label,
                        section_text="Summary Readback",
                        current_value=val,
                        readonly=True,
                    )
                )
            controls.extend(
                [
                    ObservedControl(
                        control_id="sum_conf_code",
                        dom_selector="#confirmation-code",
                        tag="span",
                        label_text="Confirmation Code",
                        section_text="Summary Outputs",
                        current_value=self.confirmation_code,
                        readonly=True,
                    ),
                    ObservedControl(
                        control_id="sum_quoted_total",
                        dom_selector="#quoted-total",
                        tag="span",
                        label_text="Quoted Total",
                        section_text="Summary Outputs",
                        current_value=self.quoted_total_display,
                        readonly=True,
                    ),
                ]
            )
            return FormSnapshot(
                url_origin_and_path=f"https://portal.example.test{self.current_route}",
                title="Independent Summary View",
                screen_id="summary_screen",
                authenticated_landmark_present=self.authenticated_landmark_present,
                controls=tuple(controls),
            )

        raise KeyError(f"Unknown route: {self.current_route}")


# ---------------------------------------------------------------------------
# Shared Pack and Dossier Builders
# ---------------------------------------------------------------------------


def _fact(
    key: str,
    value: Any,
    provenance: ProvenanceKind = ProvenanceKind.INTAKE_DOCUMENT,
) -> DossierFact:
    return DossierFact(
        key=key,
        value=value,
        provenance=provenance,
        source_id="intake_packet_001",
    )


def _build_pack(
    *,
    prior_claims_policy: PrefillPolicy = PrefillPolicy.HALT_ON_CONFLICT,
    modal_policy: str = "halt_for_human",
    use_lookup_search: bool = False,
    readback_popup_inputs: bool = True,
) -> FormPack:
    state_codec = FieldCodecSpec(
        kind=FormCodecKind.ENUM_MAP,
        enum_aliases={
            "CA": ("California", "CA"),
            "NY": ("New York", "NY"),
            "TX": ("Texas", "TX"),
        },
    )
    occupancy_codec = FieldCodecSpec(
        kind=FormCodecKind.ENUM_MAP,
        enum_aliases={
            "primary_residence": ("Primary Residence", "Owner Occupied"),
            "seasonal": ("Seasonal / Secondary",),
            "tenant": ("Tenant Occupied",),
        },
    )
    bool_codec = FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)
    phone_codec = FieldCodecSpec(kind=FormCodecKind.PHONE_DIGITS)
    date_codec = FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="MDY")
    int_codec = FieldCodecSpec(kind=FormCodecKind.INTEGER)
    cents_codec = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")

    summary_check = SummaryCheckSpec(
        summary_url_path="/lookup" if use_lookup_search else "/summary",
        search_by_output_key="confirmation_code" if use_lookup_search else None,
        search_input_anchor=(
            FieldAnchorSpec(label_patterns=("Lookup Confirmation Code",))
            if use_lookup_search
            else None
        ),
        search_submit_anchor=(
            FieldAnchorSpec(label_patterns=("Search Records",))
            if use_lookup_search
            else None
        ),
        field_readbacks=(
            FieldBindingSpec(
                binding_id="rb_terms",
                dossier_key="terms_accepted",
                anchor=FieldAnchorSpec(label_patterns=("^Attestation Accepted$",)),
                codec=bool_codec,
            ),
            FieldBindingSpec(
                binding_id="rb_first_name",
                dossier_key="first_name",
                anchor=FieldAnchorSpec(
                    label_patterns=("Applicant First Name",),
                    section_patterns=("Summary Readback",),
                ),
            ),
            FieldBindingSpec(
                binding_id="rb_last_name",
                dossier_key="last_name",
                anchor=FieldAnchorSpec(
                    label_patterns=("Applicant Last Name",),
                    section_patterns=("Summary Readback",),
                ),
            ),
            FieldBindingSpec(
                binding_id="rb_effective_date",
                dossier_key="effective_date",
                anchor=FieldAnchorSpec(
                    label_patterns=("Requested Effective Date",),
                    section_patterns=("Summary Readback",),
                ),
                codec=date_codec,
            ),
            FieldBindingSpec(
                binding_id="rb_phone",
                dossier_key="phone_number",
                anchor=FieldAnchorSpec(
                    label_patterns=("Contact Phone",),
                    section_patterns=("Summary Readback",),
                ),
                codec=phone_codec,
            ),
            FieldBindingSpec(
                binding_id="rb_state",
                dossier_key="state_code",
                anchor=FieldAnchorSpec(
                    label_patterns=("Property State",),
                    section_patterns=("Summary Readback",),
                ),
                codec=state_codec,
            ),
            FieldBindingSpec(
                binding_id="rb_occupancy",
                dossier_key="occupancy_kind",
                anchor=FieldAnchorSpec(
                    label_patterns=("Occupancy Classification",),
                    section_patterns=("Summary Readback",),
                ),
                codec=occupancy_codec,
            ),
            FieldBindingSpec(
                binding_id="rb_prior_claims",
                dossier_key="prior_claims_count",
                anchor=FieldAnchorSpec(
                    label_patterns=("Prior Claims",),
                    section_patterns=("Summary Readback",),
                ),
                codec=int_codec,
            ),
            FieldBindingSpec(
                binding_id="rb_has_pool",
                dossier_key="has_pool",
                anchor=FieldAnchorSpec(
                    label_patterns=("Swimming Pool on Premises",),
                    section_patterns=("Summary Readback",),
                ),
                codec=bool_codec,
            ),
            FieldBindingSpec(
                binding_id="rb_pool_fenced",
                dossier_key="pool_fenced",
                anchor=FieldAnchorSpec(
                    label_patterns=("Self-Latching Perimeter Fence",),
                    section_patterns=("Summary Readback",),
                ),
                codec=bool_codec,
            ),
            FieldBindingSpec(
                binding_id="rb_has_trampoline",
                dossier_key="has_trampoline",
                anchor=FieldAnchorSpec(
                    label_patterns=("Trampoline on Premises",),
                    section_patterns=("Summary Readback",),
                ),
                codec=bool_codec,
            ),
            FieldBindingSpec(
                binding_id="rb_replacement_cost",
                dossier_key="replacement_cost",
                anchor=FieldAnchorSpec(
                    label_patterns=("Estimated Replacement Cost",),
                    section_patterns=("Summary Readback",),
                ),
                codec=cents_codec,
            ),
            *(
                (
                    FieldBindingSpec(
                        binding_id="rb_living_area_sqft",
                        dossier_key="living_area_sqft",
                        anchor=FieldAnchorSpec(
                            label_patterns=("Living Area Square Feet",),
                            section_patterns=("Summary Readback",),
                        ),
                        codec=int_codec,
                    ),
                    FieldBindingSpec(
                        binding_id="rb_construction_year",
                        dossier_key="construction_year",
                        anchor=FieldAnchorSpec(
                            label_patterns=("Year Built",),
                            section_patterns=("Summary Readback",),
                        ),
                        codec=int_codec,
                    ),
                )
                if readback_popup_inputs
                else ()
            ),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="confirmation_code",
                anchor=FieldAnchorSpec(label_patterns=("^Confirmation Code$",)),
                codec=FieldCodecSpec(kind=FormCodecKind.EXACT),
                required=True,
            ),
            OutputExtractionSpec(
                output_key="quoted_total_cents",
                anchor=FieldAnchorSpec(label_patterns=("Quoted Total",)),
                codec=cents_codec,
                required=True,
            ),
        ),
    )

    return FormPack(
        pack_id="residential_property_intake_v1",
        domain_schema="openadapt.domain.property/v1",
        ui_profile_id="generic_web_ui",
        bindings=(
            FieldBindingSpec(
                binding_id="b_first_name",
                dossier_key="first_name",
                anchor=FieldAnchorSpec(label_patterns=("^Applicant First Name$",)),
                control_kind=ControlKind.TEXT,
            ),
            FieldBindingSpec(
                binding_id="b_last_name",
                dossier_key="last_name",
                anchor=FieldAnchorSpec(label_patterns=("^Applicant Last Name$",)),
                control_kind=ControlKind.TEXT,
            ),
            FieldBindingSpec(
                binding_id="b_effective_date",
                dossier_key="effective_date",
                anchor=FieldAnchorSpec(label_patterns=("Requested Effective Date",)),
                control_kind=ControlKind.DATE,
                codec=date_codec,
            ),
            FieldBindingSpec(
                binding_id="b_phone",
                dossier_key="phone_number",
                anchor=FieldAnchorSpec(label_patterns=("Contact Phone",)),
                control_kind=ControlKind.TEXT,
                codec=phone_codec,
            ),
            FieldBindingSpec(
                binding_id="b_state",
                dossier_key="state_code",
                anchor=FieldAnchorSpec(label_patterns=("Property State",)),
                control_kind=ControlKind.SELECT,
                codec=state_codec,
            ),
            FieldBindingSpec(
                binding_id="b_occupancy",
                dossier_key="occupancy_kind",
                anchor=FieldAnchorSpec(label_patterns=("Occupancy Classification",)),
                control_kind=ControlKind.PORTAL_COMBOBOX,
                codec=occupancy_codec,
            ),
            FieldBindingSpec(
                binding_id="b_prior_claims",
                dossier_key="prior_claims_count",
                anchor=FieldAnchorSpec(label_patterns=("Prior Claims",)),
                control_kind=ControlKind.TEXT,
                codec=int_codec,
                prefill_policy=prior_claims_policy,
            ),
            FieldBindingSpec(
                binding_id="b_has_pool",
                dossier_key="has_pool",
                anchor=FieldAnchorSpec(label_patterns=("Swimming Pool on Premises",)),
                control_kind=ControlKind.SELECT,
                codec=bool_codec,
                triggers_re_render=True,
            ),
            FieldBindingSpec(
                binding_id="b_pool_fenced",
                dossier_key="pool_fenced",
                anchor=FieldAnchorSpec(
                    label_patterns=("Self-Latching Perimeter Fence",)
                ),
                control_kind=ControlKind.SELECT,
                codec=bool_codec,
            ),
            FieldBindingSpec(
                binding_id="b_terms",
                dossier_key="terms_accepted",
                anchor=FieldAnchorSpec(
                    label_patterns=("attest that all grounded entries",)
                ),
                control_kind=ControlKind.CHECKBOX,
                codec=bool_codec,
            ),
        ),
        entity_tables=(
            EntityTableSpec(
                table_id="household_table",
                entity_kind="household_member",
                row_selector="[data-hh-row]",
                column_selectors={
                    "first_name": ".col-first",
                    "last_name": ".col-last",
                },
                key_fields=("first_name", "last_name"),
                field_codecs={
                    "first_name": FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
                    "last_name": FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
                },
                dismiss_button_selector=".btn-dismiss-row",
            ),
        ),
        popup_subroutines=(
            PopupSubroutineSpec(
                subroutine_id="replacement_cost_calc",
                trigger_anchor=FieldAnchorSpec(
                    label_patterns=("Calculate Replacement Cost",)
                ),
                popup_url_pattern="/estimator/popup",
                popup_title_pattern="Estimator",
                bindings=(
                    FieldBindingSpec(
                        binding_id="pop_sqft",
                        dossier_key="living_area_sqft",
                        anchor=FieldAnchorSpec(label_patterns=("Living Area",)),
                        control_kind=ControlKind.TEXT,
                        codec=int_codec,
                    ),
                    FieldBindingSpec(
                        binding_id="pop_year",
                        dossier_key="construction_year",
                        anchor=FieldAnchorSpec(label_patterns=("Construction Year",)),
                        control_kind=ControlKind.TEXT,
                        codec=int_codec,
                    ),
                ),
                commit_anchor=FieldAnchorSpec(label_patterns=("Apply Estimate",)),
                parent_return_anchor=FieldAnchorSpec(
                    label_patterns=("Estimated Replacement Cost",)
                ),
                return_output_key="replacement_cost",
                return_codec=cents_codec,
            ),
        ),
        modal_rules=(
            ModalRuleSpec(
                modal_id="duplicate_contact_modal",
                container_selector="#modal-duplicate-contact",
                confirm_selector="#modal-duplicate-confirm",
                policy=modal_policy,  # type: ignore[arg-type]
                dossier_flag_key="allow_duplicate_contact",
            ),
        ),
        screens=(
            WizardScreenSpec(
                screen_id="applicant_screen",
                next_button_anchor=FieldAnchorSpec(
                    label_patterns=("Next: Property Details",)
                ),
            ),
            WizardScreenSpec(
                screen_id="property_screen",
                next_button_anchor=FieldAnchorSpec(label_patterns=("Next: Review",)),
            ),
            WizardScreenSpec(
                screen_id="review_screen",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(
                    label_patterns=("Submit Transaction",)
                ),
            ),
        ),
        summary_check=summary_check,
        max_commit_mutations=1,
    )


def _build_dossier(
    *,
    has_pool: str = "Yes",
    extra_facts: Optional[dict[str, Any]] = None,
    omit_keys: Sequence[str] = (),
) -> InputDossier:
    raw_facts: dict[str, Any] = {
        "first_name": "Elena",
        "last_name": "Vance",
        "effective_date": "05/15/2026",
        "phone_number": "+1 (415) 555-0192",
        "state_code": "California",
        "occupancy_kind": "Owner Occupied",
        "prior_claims_count": 0,
        "has_pool": has_pool,
        "living_area_sqft": 2425,
        "construction_year": 1998,
        "terms_accepted": True,
    }
    if str(has_pool).strip().lower() in ("yes", "true", "1"):
        raw_facts["pool_fenced"] = "Yes"
    if extra_facts:
        raw_facts.update(extra_facts)
    for k in omit_keys:
        raw_facts.pop(k, None)

    facts = {k: _fact(k, v) for k, v in raw_facts.items()}
    primary_entity = DossierEntity(
        entity_kind="primary_applicant",
        entity_id="ent_primary_1",
        fields={
            "first_name": _fact("first_name", "Elena"),
            "last_name": _fact("last_name", "Vance"),
        },
    )
    return InputDossier(
        dossier_id="dos_001",
        request_id="req_001",
        facts=facts,
        entities=(primary_entity,),
    )


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------


def test_fake_surface_satisfies_protocol() -> None:
    surface = FakeStatefulFormSurface()
    assert isinstance(surface, FormSurfaceBackend)


def test_ppr_conditional_cascade_human_resume_popup_summary_and_receipt() -> None:
    surface = FakeStatefulFormSurface()
    pack = _build_pack()
    dossier = _build_dossier(has_pool="Yes")
    priv_key, pub_key = generate_ed25519_keypair()

    reconciler = FormReconciler(surface)
    session = reconciler.create_session(
        pack, dossier, run_id="run_ppr_001", signing_key=priv_key
    )

    # First pass: fills Screen 1 -> advances to Screen 2 -> sets has_pool=Yes ->
    # PPR spawns mapped pool_fenced (auto-filled Yes) + unmapped has_trampoline ->
    # runs popup calculator ($485,000.00) -> abstains on unmapped has_trampoline!
    halted = reconciler.run_until_halt_or_done(session)
    assert halted.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert halted.safe_to_retry is True
    assert surface.current_route == "/wizard/property"
    assert surface.values["pool_fenced"] == "yes"
    assert surface.values["replacement_cost"] == "$485,000.00"
    assert session.subroutines_completed == ["replacement_cost_calc"]
    assert len(halted.halts) == 1

    halt = halted.halts[0]
    assert halt.kind == FormHaltKind.UNGROUNDED_REQUIRED_FIELD
    assert halt.field_label == "Trampoline on Premises"
    assert halt.dossier_key is None
    assert len(halt.available_options) == 3

    resolution = HumanResolutionInput(
        halt_id=halt.halt_id,
        action=HumanDecisionAction.PROVIDE_VALUE,
        actor_id="operator_jane",
        provided_value="No",
        mapped_dossier_key="has_trampoline",
        remember_as_candidate_rule=True,
        idempotency_key="idem_trampoline_001",
    )
    done = reconciler.apply_human_resolution(session, resolution)
    assert done.outcome == FormTxOutcome.VERIFIED_DONE
    assert done.safe_to_retry is False
    assert done.outputs == {
        "confirmation_code": "CONF-10001",
        "quoted_total_cents": 124050,
    }
    assert done.summary_verdict is not None
    assert done.summary_verdict.passed is True
    assert done.summary_verdict.checked_count == 14
    assert done.summary_verdict.not_checked_keys == ()

    verdict_by_key = {v.dossier_key: v for v in done.summary_verdict.field_verdicts}
    assert verdict_by_key["has_trampoline"].provenance == ProvenanceKind.HUMAN_OPERATOR
    assert verdict_by_key["first_name"].provenance == ProvenanceKind.INTAKE_DOCUMENT

    # Duplicate idempotency_key submission returns identical result without re-running.
    muts_before_dup = surface.mutation_count
    replayed = reconciler.apply_human_resolution(session, resolution)
    assert replayed == done
    assert surface.mutation_count == muts_before_dup
    assert len(session.human_resolutions) == 1

    # Receipt verification & PII scrubbing checks
    assert done.receipt is not None
    assert verify_form_tx_receipt(
        done.receipt, expected_public_key_hex=ed25519_public_key_hex(pub_key)
    )
    assert verify_salted_output_digest(done.receipt, "confirmation_code", "CONF-10001")
    assert not verify_salted_output_digest(
        done.receipt, "confirmation_code", "CONF-99999"
    )

    serialized_receipt = done.receipt.model_dump_json()
    # Complete phone representations are sensitive. Arbitrary 3/4-digit
    # substrings can occur by chance in valid cryptographic hex output.
    for pii_token in (
        "Elena",
        "Vance",
        "+1 (415) 555-0192",
        "14155550192",
        "4155550192",
        "CONF-10001",
        "operator_jane",
    ):
        assert pii_token not in serialized_receipt


def test_entity_table_prefill_conflict_dismiss_accept_and_broken_dismiss() -> None:
    grounded_row = {"first_name": "Elena", "last_name": "Vance"}
    unlisted_spouse = {"first_name": "Marcus", "last_name": "Vance"}

    # Path A: DISMISS_PORTAL_ENTITY
    surface_a = FakeStatefulFormSurface(entity_rows=[grounded_row, unlisted_spouse])
    pack = _build_pack()
    dossier = _build_dossier(has_pool="No")
    reconciler_a = FormReconciler(surface_a)
    session_a = reconciler_a.create_session(pack, dossier)

    halted_a = reconciler_a.run_until_halt_or_done(session_a)
    assert halted_a.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert len(halted_a.halts) == 1
    halt_a = halted_a.halts[0]
    assert halt_a.kind == FormHaltKind.PREFILL_ENTITY_CONFLICT
    assert halt_a.portal_value == unlisted_spouse
    assert set(halt_a.allowed_actions) == {
        HumanDecisionAction.ACCEPT_PORTAL_PREFILL,
        HumanDecisionAction.DISMISS_PORTAL_ENTITY,
        HumanDecisionAction.ESCALATE_TO_OWNER,
        HumanDecisionAction.ABORT_RUN,
    }

    done_a = reconciler_a.apply_human_resolution(
        session_a,
        HumanResolutionInput(
            halt_id=halt_a.halt_id,
            action=HumanDecisionAction.DISMISS_PORTAL_ENTITY,
            actor_id="op_1",
            idempotency_key="idem_dismiss_spouse",
        ),
    )
    assert done_a.outcome == FormTxOutcome.VERIFIED_DONE
    assert len(surface_a.household_rows) == 1
    assert len(session_a.dismissed_entities) == 1

    # Path B: ACCEPT_PORTAL_PREFILL
    surface_b = FakeStatefulFormSurface(entity_rows=[grounded_row, unlisted_spouse])
    reconciler_b = FormReconciler(surface_b)
    session_b = reconciler_b.create_session(pack, dossier)
    halted_b = reconciler_b.run_until_halt_or_done(session_b)
    assert halted_b.halts[0].kind == FormHaltKind.PREFILL_ENTITY_CONFLICT

    done_b = reconciler_b.apply_human_resolution(
        session_b,
        HumanResolutionInput(
            halt_id=halted_b.halts[0].halt_id,
            action=HumanDecisionAction.ACCEPT_PORTAL_PREFILL,
            actor_id="op_1",
            idempotency_key="idem_accept_spouse",
        ),
    )
    # The kept entity row has no read-back, so it is not_checked: the run
    # cannot end verified_done with a receipt (absent-evidence-as-success).
    assert done_b.outcome == FormTxOutcome.READBACK_INCOMPLETE
    assert done_b.receipt is None
    assert any(k.startswith("entity:") for k in done_b.summary_verdict.not_checked_keys)
    assert len(surface_b.household_rows) == 2

    # Path C: Fail-closed when dismiss click fails to remove row
    surface_c = FakeStatefulFormSurface(
        entity_rows=[unlisted_spouse],
        dismiss_works=False,
    )
    reconciler_c = FormReconciler(surface_c)
    session_c = reconciler_c.create_session(pack, dossier)
    halted_c = reconciler_c.run_until_halt_or_done(session_c)
    refused_c = reconciler_c.apply_human_resolution(
        session_c,
        HumanResolutionInput(
            halt_id=halted_c.halts[0].halt_id,
            action=HumanDecisionAction.DISMISS_PORTAL_ENTITY,
            actor_id="op_1",
            idempotency_key="idem_broken_dismiss",
        ),
    )
    assert refused_c.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert "Refused DISMISS_PORTAL_ENTITY" in refused_c.what_happened


def test_prefill_value_conflict_halt_accept_override_ungrounded_and_overwrite_policy() -> (
    None
):
    dossier = _build_dossier(has_pool="No")

    # 1. Portal prefills prior_claims_count = "2" vs dossier 0 -> halts PREFILL_VALUE_CONFLICT
    surface_accept = FakeStatefulFormSurface(prefill_values={"prior_claims_count": "2"})
    pack_halt = _build_pack(prior_claims_policy=PrefillPolicy.HALT_ON_CONFLICT)
    rec_accept = FormReconciler(surface_accept)
    sess_accept = rec_accept.create_session(pack_halt, dossier)

    halted = rec_accept.run_until_halt_or_done(sess_accept)
    assert halted.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    halt = halted.halts[0]
    assert halt.kind == FormHaltKind.PREFILL_VALUE_CONFLICT
    assert halt.portal_value == "2"
    assert halt.dossier_value == 0

    # Human accepts portal prefill "2" -> SummaryJudge checks against "2" with ACCEPTED_PORTAL_PREFILL
    done_accept = rec_accept.apply_human_resolution(
        sess_accept,
        HumanResolutionInput(
            halt_id=halt.halt_id,
            action=HumanDecisionAction.ACCEPT_PORTAL_PREFILL,
            actor_id="op_1",
            idempotency_key="idem_accept_claims",
        ),
    )
    assert done_accept.outcome == FormTxOutcome.VERIFIED_DONE
    assert done_accept.summary_verdict is not None
    claims_verdict = {
        v.dossier_key: v for v in done_accept.summary_verdict.field_verdicts
    }["prior_claims_count"]
    assert claims_verdict.passed is True
    assert claims_verdict.provenance == ProvenanceKind.ACCEPTED_PORTAL_PREFILL

    # 2. Human overrides prefill conflict via PROVIDE_VALUE("0")
    surface_override = FakeStatefulFormSurface(
        prefill_values={"prior_claims_count": "2"}
    )
    rec_override = FormReconciler(surface_override)
    sess_override = rec_override.create_session(pack_halt, dossier)
    halted_ov = rec_override.run_until_halt_or_done(sess_override)
    done_ov = rec_override.apply_human_resolution(
        sess_override,
        HumanResolutionInput(
            halt_id=halted_ov.halts[0].halt_id,
            action=HumanDecisionAction.PROVIDE_VALUE,
            actor_id="op_1",
            provided_value="0",
            idempotency_key="idem_override_claims",
        ),
    )
    assert done_ov.outcome == FormTxOutcome.VERIFIED_DONE
    assert surface_override.committed_record is not None
    assert surface_override.committed_record["prior_claims_count"] == "0"

    # 3. Portal prefills a mapped field when dossier has NO grounded fact -> halts PREFILL_VALUE_CONFLICT
    dossier_missing_claims = _build_dossier(
        has_pool="No", omit_keys=("prior_claims_count",)
    )
    surface_ungrounded = FakeStatefulFormSurface(
        prefill_values={"prior_claims_count": "2"}
    )
    rec_ungrounded = FormReconciler(surface_ungrounded)
    sess_ungrounded = rec_ungrounded.create_session(pack_halt, dossier_missing_claims)
    halted_ungrounded = rec_ungrounded.run_until_halt_or_done(sess_ungrounded)
    assert halted_ungrounded.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert halted_ungrounded.halts[0].kind == FormHaltKind.PREFILL_VALUE_CONFLICT
    assert halted_ungrounded.halts[0].dossier_value is None

    # 4. OVERWRITE_WITH_DOSSIER overwrites automatically without halting
    surface_ow = FakeStatefulFormSurface(prefill_values={"prior_claims_count": "2"})
    pack_ow = _build_pack(prior_claims_policy=PrefillPolicy.OVERWRITE_WITH_DOSSIER)
    rec_ow = FormReconciler(surface_ow)
    sess_ow = rec_ow.create_session(pack_ow, dossier)
    done_ow = rec_ow.run_until_halt_or_done(sess_ow)
    assert done_ow.outcome == FormTxOutcome.VERIFIED_DONE
    assert surface_ow.committed_record is not None
    assert surface_ow.committed_record["prior_claims_count"] == "0"


def test_portal_validation_banner_blocks_next_and_commit() -> None:
    pack = _build_pack()
    dossier = _build_dossier(has_pool="No")

    # Block Next transition with banner + inline field error
    surface_next = FakeStatefulFormSurface(
        block_next_with_banner="Unable to validate applicant address region.",
        block_next_field_error=("#phone-number", "Carrier lookup failed"),
    )
    rec_next = FormReconciler(surface_next)
    sess_next = rec_next.create_session(pack, dossier)
    res_next = rec_next.run_until_halt_or_done(sess_next)
    assert res_next.outcome == FormTxOutcome.VALIDATION_BLOCKED
    assert res_next.safe_to_retry is True
    assert len(res_next.halts) == 1
    assert res_next.halts[0].kind == FormHaltKind.PORTAL_VALIDATION_BLOCKED
    assert "Carrier lookup failed" in res_next.halts[0].message

    # Block Commit with banner error
    surface_commit = FakeStatefulFormSurface(
        block_commit_with_banner="Underwriting service temporarily unavailable."
    )
    rec_commit = FormReconciler(surface_commit)
    sess_commit = rec_commit.create_session(pack, dossier)
    res_commit = rec_commit.run_until_halt_or_done(sess_commit)
    assert res_commit.outcome == FormTxOutcome.VALIDATION_BLOCKED
    assert res_commit.safe_to_retry is True
    assert res_commit.halts[0].kind == FormHaltKind.PORTAL_VALIDATION_BLOCKED
    assert res_commit.receipt is None


def test_duplicate_record_modal_human_confirm_and_dossier_flag_auto_confirm() -> None:
    dossier = _build_dossier(has_pool="No")

    # 1. halt_for_human policy -> MODAL_CONFIRMATION_REQUIRED -> CONFIRM_MODAL_ACTION -> VERIFIED_DONE
    surface = FakeStatefulFormSurface(trigger_duplicate_modal_on_commit=True)
    pack = _build_pack(modal_policy="halt_for_human")
    reconciler = FormReconciler(surface)
    session = reconciler.create_session(pack, dossier)

    halted = reconciler.run_until_halt_or_done(session)
    assert halted.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert len(halted.halts) == 1
    modal_halt = halted.halts[0]
    assert modal_halt.kind == FormHaltKind.MODAL_CONFIRMATION_REQUIRED
    assert set(modal_halt.allowed_actions) == {
        HumanDecisionAction.CONFIRM_MODAL_ACTION,
        HumanDecisionAction.ABORT_RUN,
        HumanDecisionAction.ESCALATE_TO_OWNER,
    }

    done = reconciler.apply_human_resolution(
        session,
        HumanResolutionInput(
            halt_id=modal_halt.halt_id,
            action=HumanDecisionAction.CONFIRM_MODAL_ACTION,
            actor_id="op_modal",
            idempotency_key="idem_confirm_dup_modal",
        ),
    )
    assert done.outcome == FormTxOutcome.VERIFIED_DONE
    assert "duplicate_contact_modal" in session.confirmed_modals
    assert done.receipt is not None
    assert verify_form_tx_receipt(done.receipt)

    # 2. confirm_if_dossier_flag policy with truthy flag -> zero halts
    surface_auto = FakeStatefulFormSurface(trigger_duplicate_modal_on_commit=True)
    pack_auto = _build_pack(modal_policy="confirm_if_dossier_flag")
    dossier_auto = _build_dossier(
        has_pool="No", extra_facts={"allow_duplicate_contact": True}
    )
    rec_auto = FormReconciler(surface_auto)
    sess_auto = rec_auto.create_session(pack_auto, dossier_auto)
    done_auto = rec_auto.run_until_halt_or_done(sess_auto)
    assert done_auto.outcome == FormTxOutcome.VERIFIED_DONE
    assert "duplicate_contact_modal" in sess_auto.confirmed_modals


def test_fixed_on_screen_fail_closed_and_escalate_and_abort_paths() -> None:
    pack = _build_pack()
    dossier = _build_dossier(has_pool="Yes")

    # FIXED_ON_SCREEN refuses when field is still empty, then succeeds after manual screen edit
    surface = FakeStatefulFormSurface()
    reconciler = FormReconciler(surface)
    session = reconciler.create_session(pack, dossier)
    halted = reconciler.run_until_halt_or_done(session)
    halt_id = halted.halts[0].halt_id

    refused = reconciler.apply_human_resolution(
        session,
        HumanResolutionInput(
            halt_id=halt_id,
            action=HumanDecisionAction.FIXED_ON_SCREEN,
            actor_id="op_1",
            idempotency_key="idem_fixed_too_early",
        ),
    )
    assert refused.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert "Refused FIXED_ON_SCREEN" in refused.what_happened
    assert len(session.human_resolutions) == 0

    # Operator manually sets control on screen, then submits FIXED_ON_SCREEN
    surface.select_native_option("#has-trampoline", "no")
    done = reconciler.apply_human_resolution(
        session,
        HumanResolutionInput(
            halt_id=halt_id,
            action=HumanDecisionAction.FIXED_ON_SCREEN,
            actor_id="op_1",
            mapped_dossier_key="has_trampoline",
            idempotency_key="idem_fixed_after_edit",
        ),
    )
    assert done.outcome == FormTxOutcome.VERIFIED_DONE
    assert session.effective_facts["has_trampoline"].provenance == (
        ProvenanceKind.HUMAN_OPERATOR
    )

    # ESCALATE_TO_OWNER
    surface_esc = FakeStatefulFormSurface()
    rec_esc = FormReconciler(surface_esc)
    sess_esc = rec_esc.create_session(pack, dossier)
    h_esc = rec_esc.run_until_halt_or_done(sess_esc)
    res_esc = rec_esc.apply_human_resolution(
        sess_esc,
        HumanResolutionInput(
            halt_id=h_esc.halts[0].halt_id,
            action=HumanDecisionAction.ESCALATE_TO_OWNER,
            actor_id="op_1",
            idempotency_key="idem_escalate",
        ),
    )
    assert res_esc.outcome == FormTxOutcome.ESCALATED_TO_OWNER

    # ABORT_RUN
    surface_ab = FakeStatefulFormSurface()
    rec_ab = FormReconciler(surface_ab)
    sess_ab = rec_ab.create_session(pack, dossier)
    h_ab = rec_ab.run_until_halt_or_done(sess_ab)
    res_ab = rec_ab.apply_human_resolution(
        sess_ab,
        HumanResolutionInput(
            halt_id=h_ab.halts[0].halt_id,
            action=HumanDecisionAction.ABORT_RUN,
            actor_id="op_1",
            idempotency_key="idem_abort",
        ),
    )
    assert res_ab.outcome == FormTxOutcome.ABORTED_BY_HUMAN


def test_auth_wall_popup_failures_ambiguous_anchor_and_mutation_budget() -> None:
    pack = _build_pack()
    dossier = _build_dossier(has_pool="No")

    # 1. Auth wall detected
    surface_auth = FakeStatefulFormSurface(auth_wall=True)
    res_auth = FormReconciler(surface_auth).run_until_halt_or_done(
        ReconcilerSession(pack, dossier)
    )
    assert res_auth.outcome == FormTxOutcome.AUTH_REQUIRED
    assert res_auth.halts[0].kind == FormHaltKind.AUTH_WALL_DETECTED

    # 2. Missing authenticated landmark
    surface_lm = FakeStatefulFormSurface(
        require_authenticated_landmark=True,
        authenticated_landmark_present=False,
    )
    res_lm = FormReconciler(surface_lm).run_until_halt_or_done(
        ReconcilerSession(pack, dossier)
    )
    assert res_lm.outcome == FormTxOutcome.AUTH_REQUIRED

    # 3. Popup exits without populating parent return field -> POPUP_CONTRACT_FAILED
    surface_pop = FakeStatefulFormSurface(popup_broken_return=True)
    res_pop = FormReconciler(surface_pop).run_until_halt_or_done(
        ReconcilerSession(pack, dossier)
    )
    assert res_pop.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert res_pop.halts[0].kind == FormHaltKind.POPUP_CONTRACT_FAILED

    # 4. Popup has ungrounded required input -> halts cleanly, human provides value -> completes!
    surface_pop_ug = FakeStatefulFormSurface(popup_require_unmapped_garage=True)
    rec_pop_ug = FormReconciler(surface_pop_ug)
    sess_pop_ug = rec_pop_ug.create_session(pack, dossier)
    halted_pop_ug = rec_pop_ug.run_until_halt_or_done(sess_pop_ug)
    assert halted_pop_ug.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert halted_pop_ug.halts[0].kind == FormHaltKind.UNGROUNDED_REQUIRED_FIELD
    assert halted_pop_ug.halts[0].field_label == "Attached Garage Bays"

    done_pop_ug = rec_pop_ug.apply_human_resolution(
        sess_pop_ug,
        HumanResolutionInput(
            halt_id=halted_pop_ug.halts[0].halt_id,
            action=HumanDecisionAction.PROVIDE_VALUE,
            actor_id="op_popup",
            provided_value="2",
            idempotency_key="idem_popup_garage",
        ),
    )
    # The operator's popup answer is not stored with the quote, so the summary
    # cannot show it. It matched when re-read before apply, but that is not a
    # read-back of the record: it stays not_checked and no proof is issued.
    assert done_pop_ug.outcome == FormTxOutcome.READBACK_INCOMPLETE
    assert done_pop_ug.receipt is None
    assert done_pop_ug.summary_verdict is not None
    garage = {v.dossier_key: v for v in done_pop_ug.summary_verdict.field_verdicts}[
        "control:Attached Garage Bays"
    ]
    assert garage.status == ReadbackStatus.NOT_CHECKED
    assert "re-read before apply" in garage.reason

    # 5. Ambiguous anchor match -> AMBIGUOUS_OPTION_OR_TARGET
    surface_amb = FakeStatefulFormSurface(duplicate_ambiguous_first_name=True)
    res_amb = FormReconciler(surface_amb).run_until_halt_or_done(
        ReconcilerSession(pack, dossier)
    )
    assert res_amb.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert res_amb.halts[0].kind == FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET

    # 6. Commit exceeds max_commit_mutations -> DELIVERY_UNCERTAIN / MUTATION_BUDGET_EXCEEDED
    surface_mut = FakeStatefulFormSurface(commit_mutations_to_emit=2)
    res_mut = FormReconciler(surface_mut).run_until_halt_or_done(
        ReconcilerSession(pack, dossier)
    )
    assert res_mut.outcome == FormTxOutcome.DELIVERY_UNCERTAIN
    assert res_mut.safe_to_retry is False
    assert res_mut.halts[0].kind == FormHaltKind.MUTATION_BUDGET_EXCEEDED
    assert res_mut.receipt is None


def test_summary_judge_catches_silent_server_corruption_dropped_field_and_lookup_search() -> (
    None
):
    dossier = _build_dossier(has_pool="No")
    pack = _build_pack()

    # 1. Server silently corrupts phone_number on summary screen -> READBACK_REFUTED
    surface_corrupt = FakeStatefulFormSurface(
        corrupt_summary_overrides={"phone_number": "415-555-9999"}
    )
    res_corrupt = FormReconciler(surface_corrupt).run_until_halt_or_done(
        ReconcilerSession(pack, dossier)
    )
    assert res_corrupt.outcome == FormTxOutcome.READBACK_REFUTED
    assert res_corrupt.safe_to_retry is False
    assert res_corrupt.receipt is None
    assert res_corrupt.summary_verdict is not None
    assert res_corrupt.summary_verdict.passed is False
    assert res_corrupt.summary_verdict.failed_keys == ("phone_number",)

    # 2. Server drops last_name from the summary: it cannot be read back, so
    # it is not_checked (never passed) and the run is not verified.
    surface_drop = FakeStatefulFormSurface(drop_summary_fields=("last_name",))
    res_drop = FormReconciler(surface_drop).run_until_halt_or_done(
        ReconcilerSession(pack, dossier)
    )
    assert res_drop.outcome == FormTxOutcome.READBACK_INCOMPLETE
    assert res_drop.receipt is None
    assert res_drop.summary_verdict is not None
    assert res_drop.summary_verdict.passed is False
    assert res_drop.summary_verdict.failed_keys == ()
    assert res_drop.summary_verdict.not_checked_keys == ("last_name",)

    # 3. Server fails to render required confirmation_code output -> READBACK_REFUTED
    surface_missing_out = FakeStatefulFormSurface(drop_confirmation_output=True)
    res_missing_out = FormReconciler(surface_missing_out).run_until_halt_or_done(
        ReconcilerSession(pack, dossier)
    )
    assert res_missing_out.outcome == FormTxOutcome.READBACK_REFUTED
    assert res_missing_out.summary_verdict is not None
    assert res_missing_out.summary_verdict.failed_keys == ()
    assert res_missing_out.summary_verdict.missing_required_outputs == (
        "confirmation_code",
    )

    # 4. Independent lookup-by-output-key search path succeeds cleanly
    surface_lookup = FakeStatefulFormSurface()
    pack_lookup = _build_pack(use_lookup_search=True)
    res_lookup = FormReconciler(surface_lookup).run_until_halt_or_done(
        ReconcilerSession(pack_lookup, dossier)
    )
    assert res_lookup.outcome == FormTxOutcome.VERIFIED_DONE
    assert surface_lookup.lookup_loaded_code == "CONF-10001"
    assert res_lookup.outputs["confirmation_code"] == "CONF-10001"

    # 5. Direct unit check of build_authoritative_fact_map provenance overlay
    fact_map = build_authoritative_fact_map(
        dossier,
        (
            HumanResolutionRecord(
                halt_id="h1",
                halt_kind=FormHaltKind.PREFILL_VALUE_CONFLICT,
                action=HumanDecisionAction.ACCEPT_PORTAL_PREFILL,
                actor_id="op1",
                actor_role="operator",
                field_signature="sig1",
                dossier_key="prior_claims_count",
                resolved_value="3",
                timestamp_utc="2026-05-01T00:00:00Z",
                idempotency_key="ik1",
            ),
        ),
    )
    assert fact_map["prior_claims_count"].value == "3"
    assert (
        fact_map["prior_claims_count"].provenance
        == ProvenanceKind.ACCEPTED_PORTAL_PREFILL
    )


def _all_bound_passed(pack: FormPack, dossier: InputDossier) -> SummaryVerdict:
    """A passing verdict for every bound field the dossier has a value for."""
    keys = sorted(
        {b.dossier_key for b in pack.bindings if dossier.facts.get(b.dossier_key)}
    )
    return SummaryVerdict(
        passed=True,
        checked_count=len(keys),
        field_verdicts=tuple(
            ReadbackFieldVerdict(
                dossier_key=k,
                codec_kind=FormCodecKind.WHITESPACE_COLLAPSE,
                passed=True,
                status=ReadbackStatus.PASSED,
                provenance=ProvenanceKind.INTAKE_DOCUMENT,
            )
            for k in keys
        ),
    )


def test_ed25519_key_persistence_and_single_bit_receipt_tamper_detection(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "keys" / "form_tx_signer.pem"
    created_key = load_or_create_ed25519_key(key_file)
    assert key_file.exists()
    mode = stat.S_IMODE(os.stat(key_file).st_mode)
    assert mode == 0o600

    reloaded_key = load_or_create_ed25519_key(key_file)
    assert ed25519_public_key_hex(created_key) == ed25519_public_key_hex(reloaded_key)

    pack = _build_pack()
    dossier = _build_dossier(has_pool="No")
    receipt = issue_form_tx_receipt(
        run_id="run_tamper_test",
        summary_verdict=_all_bound_passed(pack, dossier),
        pack=pack,
        dossier=dossier,
        signing_key=created_key,
        fields_filled_auto=10,
        commit_mutations_observed=1,
        outputs={"confirmation_code": "CONF-777"},
    )
    assert verify_form_tx_receipt(receipt) is True

    # Tamper 1: mutate payload field AND recompute payload_sha256 without private key
    forged_payload = receipt.payload.model_copy(update={"fields_filled_auto": 11})
    tampered_payload_rcpt = receipt.model_copy(
        update={
            "payload": forged_payload,
            "payload_sha256": canonical_sha256(forged_payload),
        }
    )
    assert verify_form_tx_receipt(tampered_payload_rcpt) is False

    # Tamper 2: mutate payload_sha256 alone
    flipped_digest = (
        "0" if receipt.payload_sha256[0] != "0" else "1"
    ) + receipt.payload_sha256[1:]
    assert (
        verify_form_tx_receipt(
            receipt.model_copy(update={"payload_sha256": flipped_digest})
        )
        is False
    )

    # Tamper 3: flip single bit in Ed25519 signature_hex
    sig_bytes = bytearray.fromhex(receipt.signature_hex)
    sig_bytes[17] ^= 0x01
    assert (
        verify_form_tx_receipt(
            receipt.model_copy(update={"signature_hex": bytes(sig_bytes).hex()})
        )
        is False
    )

    # Tamper 4: flip single bit in public_key_hex or mismatch expected_public_key_hex
    pub_bytes = bytearray.fromhex(receipt.public_key_hex)
    pub_bytes[0] ^= 0x01
    assert (
        verify_form_tx_receipt(
            receipt.model_copy(update={"public_key_hex": bytes(pub_bytes).hex()})
        )
        is False
    )
    other_priv, other_pub = generate_ed25519_keypair()
    assert (
        verify_form_tx_receipt(
            receipt, expected_public_key_hex=ed25519_public_key_hex(other_pub)
        )
        is False
    )


def test_rule_accumulator_consensus_induction_pack_admission_and_disagreement_refusal() -> (
    None
):
    accumulator = RuleAccumulator()
    base_pack = _build_pack()

    # Run 1: human resolves unmapped trampoline field mapped to dossier_key="has_trampoline"
    surface_1 = FakeStatefulFormSurface()
    rec_1 = FormReconciler(surface_1)
    sess_1 = rec_1.create_session(
        base_pack, _build_dossier(has_pool="Yes"), run_id="r1"
    )
    h1 = rec_1.run_until_halt_or_done(sess_1)
    done_1 = rec_1.apply_human_resolution(
        sess_1,
        HumanResolutionInput(
            halt_id=h1.halts[0].halt_id,
            action=HumanDecisionAction.PROVIDE_VALUE,
            actor_id="op_1",
            provided_value="No",
            mapped_dossier_key="has_trampoline",
            remember_as_candidate_rule=True,
            idempotency_key="run1_trampoline",
        ),
    )
    assert done_1.outcome == FormTxOutcome.VERIFIED_DONE

    accumulator.record_run_resolutions(
        base_pack.pack_id,
        done_1.human_resolutions,
        run_id=done_1.run_id,
        pack_digest=base_pack.digest(),
    )
    # Only 1 supporting run so far -> zero candidates
    assert accumulator.induce_candidates(base_pack.pack_id, min_support=2) == ()
    # Replaying the exact same run's records cannot inflate support count
    accumulator.record_run_resolutions(
        base_pack.pack_id,
        done_1.human_resolutions,
        run_id=done_1.run_id,
        pack_digest=base_pack.digest(),
    )
    assert accumulator.induce_candidates(base_pack.pack_id, min_support=2) == ()

    # Run 2: distinct run resolves same field_signature mapped to same dossier_key="has_trampoline"
    surface_2 = FakeStatefulFormSurface()
    rec_2 = FormReconciler(surface_2)
    sess_2 = rec_2.create_session(
        base_pack, _build_dossier(has_pool="Yes"), run_id="r2"
    )
    h2 = rec_2.run_until_halt_or_done(sess_2)
    done_2 = rec_2.apply_human_resolution(
        sess_2,
        HumanResolutionInput(
            halt_id=h2.halts[0].halt_id,
            action=HumanDecisionAction.PROVIDE_VALUE,
            actor_id="op_2",
            provided_value="Yes",
            mapped_dossier_key="has_trampoline",
            remember_as_candidate_rule=True,
            idempotency_key="run2_trampoline",
        ),
    )
    assert done_2.outcome == FormTxOutcome.VERIFIED_DONE

    accumulator.record_run_resolutions(
        base_pack.pack_id,
        done_2.human_resolutions,
        run_id=done_2.run_id,
        pack_digest=base_pack.digest(),
    )
    candidates = accumulator.induce_candidates(base_pack.pack_id, min_support=2)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.admitted is False
    assert cand.proposed_kind == "field_binding"
    assert cand.proposed_dossier_key == "has_trampoline"
    assert cand.supporting_run_count == 2

    # Admit candidate into a new immutable FormPack; Run 3 executes with ZERO human halts!
    upgraded_pack = accumulator.admit_candidate_into_pack(
        base_pack,
        cand,
        anchor=FieldAnchorSpec(label_patterns=("Trampoline on Premises",)),
        codec=FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO),
        approval_id="synthetic-test-approval",
        qualification_check=lambda _: True,
    )
    assert len(base_pack.bindings) == 10
    assert len(upgraded_pack.bindings) == 11
    assert accumulator.induce_candidates(base_pack.pack_id, min_support=2) == ()
    assert len(accumulator.admitted_candidates(base_pack.pack_id)) == 1

    surface_3 = FakeStatefulFormSurface()
    rec_3 = FormReconciler(surface_3)
    dossier_3 = _build_dossier(has_pool="Yes", extra_facts={"has_trampoline": "No"})
    done_3 = rec_3.run_until_halt_or_done(
        rec_3.create_session(upgraded_pack, dossier_3, run_id="r3")
    )
    assert done_3.outcome == FormTxOutcome.VERIFIED_DONE
    assert done_3.halts == ()
    assert done_3.human_resolutions == ()

    # Disagreement fail-closed check: 2 runs disagreeing on the same field_signature emit nothing!
    disagree_acc = RuleAccumulator()
    disagree_acc.record_run_resolutions(
        "pack_conflict",
        (
            HumanResolutionRecord(
                halt_id="h_a",
                halt_kind=FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
                action=HumanDecisionAction.PROVIDE_VALUE,
                actor_id="op1",
                actor_role="operator",
                field_signature="hazards|yard item|select|yard_item_#",
                dossier_key="has_trampoline",
                resolved_value="No",
                remember_as_candidate_rule=True,
                timestamp_utc="2026-05-01T00:00:00Z",
                idempotency_key="conflict_r1",
            ),
        ),
        run_id="synthetic-support-4",
        pack_digest=base_pack.digest(),
    )
    disagree_acc.record_run_resolutions(
        "pack_conflict",
        (
            HumanResolutionRecord(
                halt_id="h_b",
                halt_kind=FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
                action=HumanDecisionAction.PROVIDE_VALUE,
                actor_id="op2",
                actor_role="operator",
                field_signature="hazards|yard item|select|yard_item_#",
                dossier_key="has_diving_board",
                resolved_value="No",
                remember_as_candidate_rule=True,
                timestamp_utc="2026-05-02T00:00:00Z",
                idempotency_key="conflict_r2",
            ),
        ),
        run_id="synthetic-support-5",
        pack_digest=base_pack.digest(),
    )
    assert disagree_acc.induce_candidates("pack_conflict", min_support=2) == ()

    # Repeated DISMISS_PORTAL_ENTITY and ESCALATE_TO_OWNER induce candidates even with remember=False
    policy_acc = RuleAccumulator()
    for idx in (1, 2):
        policy_acc.record_run_resolutions(
            "pack_policy",
            (
                HumanResolutionRecord(
                    halt_id=f"hd_{idx}",
                    halt_kind=FormHaltKind.PREFILL_ENTITY_CONFLICT,
                    action=HumanDecisionAction.DISMISS_PORTAL_ENTITY,
                    actor_id="op1",
                    actor_role="operator",
                    field_signature="entity_table|household_table|household_member",
                    resolved_value={"first_name": "Test"},
                    remember_as_candidate_rule=False,
                    timestamp_utc="2026-05-02T00:00:00Z",
                    idempotency_key=f"dismiss_run_{idx}",
                ),
                HumanResolutionRecord(
                    halt_id=f"he_{idx}",
                    halt_kind=FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
                    action=HumanDecisionAction.ESCALATE_TO_OWNER,
                    actor_id="op1",
                    actor_role="operator",
                    field_signature="underwriting|special endorsement|select|spec_#",
                    remember_as_candidate_rule=False,
                    timestamp_utc="2026-05-02T00:00:00Z",
                    idempotency_key=f"esc_run_{idx}",
                ),
            ),
            run_id=f"synthetic-support-7-{idx}",
            pack_digest=base_pack.digest(),
        )
    induced_policies = {
        c.proposed_kind: c
        for c in policy_acc.induce_candidates("pack_policy", min_support=2)
    }
    assert set(induced_policies) == {"prefill_policy", "default_escalation"}
    assert (
        induced_policies["prefill_policy"].proposed_action
        == HumanDecisionAction.DISMISS_PORTAL_ENTITY
    )
    assert (
        induced_policies["default_escalation"].proposed_action
        == HumanDecisionAction.ESCALATE_TO_OWNER
    )

    # Module-level admit_candidate_into_pack helper check
    re_admitted = admit_candidate_into_pack(
        base_pack,
        cand,
        anchor=FieldAnchorSpec(label_patterns=("Trampoline on Premises",)),
        approval_id="synthetic-test-approval",
        qualification_check=lambda _: True,
    )
    assert len(re_admitted.bindings) == 11


def _scan_source_for_banned_tokens(
    source_text: str,
    file_label: str,
    banned_tokens: Sequence[str],
) -> list[str]:
    violations: list[str] = []
    lower_source = source_text.casefold()
    for tok in banned_tokens:
        if tok.casefold() in lower_source:
            violations.append(f"{file_label}: raw text matched banned token")

    tree = ast.parse(source_text, filename=file_label)
    for node in ast.walk(tree):
        names_to_check: list[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names_to_check.append(node.name)
        elif isinstance(node, ast.Name):
            names_to_check.append(node.id)
        elif isinstance(node, ast.Attribute):
            names_to_check.append(node.attr)
        elif isinstance(node, ast.arg):
            names_to_check.append(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names_to_check.append(node.value)

        for candidate in names_to_check:
            cand_cf = candidate.casefold()
            for tok in banned_tokens:
                if tok.casefold() in cand_cf:
                    violations.append(
                        f"{file_label}:{getattr(node, 'lineno', 0)} AST matched banned token"
                    )
    return violations


def test_generality_gate_zero_banned_vendor_or_customer_names() -> None:
    banned_tokens = _decoded_banned_tokens()
    assert len(banned_tokens) >= 10

    form_tx_dir = Path(__file__).resolve().parent.parent / "openadapt_flow" / "form_tx"
    py_files = sorted(form_tx_dir.rglob("*.py"))
    assert (
        len(py_files) >= 7
    )  # __init__, types, codecs, surface, receipt, judge, learning, reconciler

    # Scan every engine module in openadapt_flow/form_tx/ AND this test module itself!
    targets = [*py_files, Path(__file__).resolve()]
    all_violations: list[str] = []
    for path in targets:
        text = path.read_text(encoding="utf-8")
        all_violations.extend(
            _scan_source_for_banned_tokens(text, path.name, banned_tokens)
        )

    assert all_violations == [], f"Generality Gate violations: {all_violations}"

    # Self-test: verify the scanner catches a synthetic snippet containing a banned token
    synthetic = f"def helper_{banned_tokens[0]}():\n    return 1\n"
    caught = _scan_source_for_banned_tokens(synthetic, "synthetic.py", banned_tokens)
    assert len(caught) >= 2


def test_receipt_raw_seed_loading_and_strict_validation_edges(tmp_path: Path) -> None:
    priv, pub = generate_ed25519_keypair()
    from cryptography.hazmat.primitives import serialization

    raw_seed = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    hex_path = tmp_path / "seed_hex.key"
    hex_path.write_text(raw_seed.hex() + "\n", encoding="utf-8")
    loaded_hex = load_or_create_ed25519_key(hex_path)
    assert ed25519_public_key_hex(loaded_hex) == ed25519_public_key_hex(pub)

    bin_path = tmp_path / "seed_raw.bin"
    bin_path.write_bytes(raw_seed)
    loaded_bin = load_or_create_ed25519_key(bin_path)
    assert ed25519_public_key_hex(loaded_bin) == ed25519_public_key_hex(pub)

    bad_path = tmp_path / "corrupt.key"
    bad_path.write_bytes(b"not-a-valid-key")
    with pytest.raises(ValueError):
        load_or_create_ed25519_key(bad_path)

    pack = _build_pack()
    dossier = _build_dossier(has_pool="No")
    # Passing (priv, pub) keypair tuple directly
    rcpt = issue_form_tx_receipt(
        run_id="r_tuple",
        summary_verdict=_all_bound_passed(pack, dossier),
        pack=pack,
        dossier=dossier,
        signing_key=(priv, pub),
        outputs={"k": "v"},
    )
    assert verify_form_tx_receipt(rcpt)
    assert not verify_salted_output_digest(rcpt, "nonexistent_key", "v")

    with pytest.raises(ValueError, match="salt_hex"):
        issue_form_tx_receipt(
            run_id="r_bad_salt",
            pack=pack,
            dossier=dossier,
            signing_key=priv,
            salt_hex="abcd",
        )

    with pytest.raises(ValueError, match="requires pack and dossier"):
        issue_form_tx_receipt(run_id="r_missing_ids", signing_key=priv)

    assert not verify_form_tx_receipt(rcpt.model_copy(update={"signature_hex": "zz"}))
    assert not verify_form_tx_receipt(rcpt.model_copy(update={"signature_hex": "0011"}))


def test_summary_judge_edge_cases_ungrounded_surprise_and_constructor_binding() -> None:
    dossier = _build_dossier(has_pool="No")
    pack_no_summary = _build_pack().model_copy(update={"summary_check": None})
    surface = FakeStatefulFormSurface()
    verdict_none = SummaryJudge(surface, pack_no_summary, dossier).verify(
        known_outputs={"seed": 42}
    )
    assert verdict_none.passed is False
    assert verdict_none.failed_keys == ("missing_readback_contract",)
    assert verdict_none.extracted_outputs == {"seed": 42}

    # Surprise non-empty value on summary screen for an ungrounded optional key -> fail-closed!
    surface_surprise = FakeStatefulFormSurface(
        corrupt_summary_overrides={"pool_fenced": "Yes"}
    )
    res_surprise = FormReconciler(surface_surprise).run_until_halt_or_done(
        ReconcilerSession("run_surprise", _build_pack(), dossier)
    )
    assert res_surprise.outcome == FormTxOutcome.READBACK_REFUTED
    assert res_surprise.summary_verdict is not None
    assert "pool_fenced" in res_surprise.summary_verdict.failed_keys


def test_learning_prefill_policy_admission_and_intra_run_contradiction() -> None:
    acc = RuleAccumulator()
    base_pack = _build_pack()

    # ABORT_RUN is ignored; intra-run contradiction on same signature blocks candidate
    acc.record_run_resolutions(
        base_pack.pack_id,
        (
            HumanResolutionRecord(
                halt_id="h_abort",
                halt_kind=FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
                action=HumanDecisionAction.ABORT_RUN,
                actor_id="op1",
                actor_role="operator",
                field_signature="sec|f|text|f",
                remember_as_candidate_rule=True,
                timestamp_utc="2026-05-01T00:00:00Z",
                idempotency_key="ik_abort",
            ),
            HumanResolutionRecord(
                halt_id="h_intra_1",
                halt_kind=FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
                action=HumanDecisionAction.PROVIDE_VALUE,
                actor_id="op1",
                actor_role="operator",
                field_signature="sec|intra|text|intra",
                dossier_key="key_one",
                resolved_value="A",
                remember_as_candidate_rule=True,
                timestamp_utc="2026-05-01T00:00:00Z",
                idempotency_key="ik_intra_1",
            ),
            HumanResolutionRecord(
                halt_id="h_intra_2",
                halt_kind=FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
                action=HumanDecisionAction.PROVIDE_VALUE,
                actor_id="op1",
                actor_role="operator",
                field_signature="sec|intra|text|intra",
                dossier_key="key_two",
                resolved_value="B",
                remember_as_candidate_rule=True,
                timestamp_utc="2026-05-01T00:00:00Z",
                idempotency_key="ik_intra_2",
            ),
        ),
        run_id="synthetic-support-6",
        pack_digest=base_pack.digest(),
    )
    assert acc.induce_candidates(base_pack.pack_id, min_support=2) == ()

    # Two consistent runs overriding PREFILL_VALUE_CONFLICT on prior_claims_count -> prefill_policy
    for idx in (1, 2):
        acc.record_run_resolutions(
            base_pack.pack_id,
            (
                HumanResolutionRecord(
                    halt_id=f"hp_{idx}",
                    halt_kind=FormHaltKind.PREFILL_VALUE_CONFLICT,
                    action=HumanDecisionAction.PROVIDE_VALUE,
                    actor_id="op1",
                    actor_role="operator",
                    field_signature="loss history|prior claims|text|prior_claims_count",
                    dossier_key="prior_claims_count",
                    resolved_value="0",
                    remember_as_candidate_rule=True,
                    timestamp_utc="2026-05-01T00:00:00Z",
                    idempotency_key=f"ik_prefill_ov_{idx}",
                ),
            ),
            run_id=f"synthetic-support-8-{idx}",
            pack_digest=base_pack.digest(),
        )
    cands = acc.induce_candidates(base_pack.pack_id, min_support=2)
    assert len(cands) == 1
    assert cands[0].proposed_kind == "prefill_policy"

    patched = acc.admit_candidate_into_pack(
        base_pack,
        cands[0],
        anchor=FieldAnchorSpec(label_patterns=("Prior Claims",)),
        approval_id="synthetic-test-approval",
        qualification_check=lambda _: True,
    )
    claims_binding = next(
        b for b in patched.bindings if b.dossier_key == "prior_claims_count"
    )
    assert claims_binding.prefill_policy == PrefillPolicy.OVERWRITE_WITH_DOSSIER

    with pytest.raises(ValueError, match="min_support"):
        acc.induce_candidates(base_pack.pack_id, min_support=1)


def test_reconciler_resolution_validation_auth_and_banner_recovery_and_budgets() -> (
    None
):
    pack = _build_pack()
    dossier = _build_dossier(has_pool="No")

    # 1. Auth wall recovery via FIXED_ON_SCREEN
    surface_auth = FakeStatefulFormSurface(auth_wall=True)
    rec_auth = FormReconciler(surface_auth)
    sess_auth = rec_auth.create_session(pack, dossier)
    h_auth = rec_auth.run_until_halt_or_done(sess_auth)
    assert h_auth.outcome == FormTxOutcome.AUTH_REQUIRED

    # Rejects disallowed action and unknown halt_id
    with pytest.raises(ValueError, match="does not match any active halt"):
        rec_auth.apply_human_resolution(
            sess_auth,
            HumanResolutionInput(
                halt_id="nonexistent_halt",
                action=HumanDecisionAction.ABORT_RUN,
                actor_id="op",
                idempotency_key="ik_bad_halt",
            ),
        )
    with pytest.raises(ValueError, match="not allowed"):
        rec_auth.apply_human_resolution(
            sess_auth,
            HumanResolutionInput(
                halt_id=h_auth.halts[0].halt_id,
                action=HumanDecisionAction.CONFIRM_MODAL_ACTION,
                actor_id="op",
                idempotency_key="ik_bad_action",
            ),
        )

    # Refuses FIXED_ON_SCREEN while auth_wall is still True
    still_locked = rec_auth.apply_human_resolution(
        sess_auth,
        HumanResolutionInput(
            halt_id=h_auth.halts[0].halt_id,
            action=HumanDecisionAction.FIXED_ON_SCREEN,
            actor_id="op",
            idempotency_key="ik_auth_still_locked",
        ),
    )
    assert still_locked.outcome == FormTxOutcome.AUTH_REQUIRED

    # Operator authenticates in browser -> FIXED_ON_SCREEN resumes to VERIFIED_DONE
    surface_auth.auth_wall = False
    done_auth = rec_auth.apply_human_resolution(
        sess_auth,
        HumanResolutionInput(
            halt_id=h_auth.halts[0].halt_id,
            action=HumanDecisionAction.FIXED_ON_SCREEN,
            actor_id="op",
            idempotency_key="ik_auth_unlocked",
        ),
    )
    assert done_auth.outcome == FormTxOutcome.VERIFIED_DONE

    # 2. Validation banner recovery via FIXED_ON_SCREEN & broken modal confirm refusal
    surface_banner = FakeStatefulFormSurface(
        block_next_with_banner="Temporary gateway timeout",
        trigger_duplicate_modal_on_commit=True,
        modal_close_works=False,
    )
    rec_b = FormReconciler(surface_banner)
    sess_b = rec_b.create_session(pack, dossier)
    h_banner = rec_b.run_until_halt_or_done(sess_b)
    assert h_banner.outcome == FormTxOutcome.VALIDATION_BLOCKED

    # Still has banner error -> refuses FIXED_ON_SCREEN
    still_err = rec_b.apply_human_resolution(
        sess_b,
        HumanResolutionInput(
            halt_id=h_banner.halts[0].halt_id,
            action=HumanDecisionAction.FIXED_ON_SCREEN,
            actor_id="op",
            idempotency_key="ik_banner_still_err",
        ),
    )
    assert still_err.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN

    # Clear banner -> resumes to commit modal -> modal fails to close -> refuses CONFIRM_MODAL_ACTION!
    surface_banner.active_banners.clear()
    surface_banner.block_next_with_banner = None
    h_modal = rec_b.apply_human_resolution(
        sess_b,
        HumanResolutionInput(
            halt_id=h_banner.halts[0].halt_id,
            action=HumanDecisionAction.FIXED_ON_SCREEN,
            actor_id="op",
            idempotency_key="ik_banner_cleared",
        ),
    )
    assert h_modal.halts[0].kind == FormHaltKind.MODAL_CONFIRMATION_REQUIRED

    refused_modal = rec_b.apply_human_resolution(
        sess_b,
        HumanResolutionInput(
            halt_id=h_modal.halts[0].halt_id,
            action=HumanDecisionAction.CONFIRM_MODAL_ACTION,
            actor_id="op",
            idempotency_key="ik_modal_stuck",
        ),
    )
    assert refused_modal.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert "remained open" in refused_modal.what_happened

    # 3. Combobox unmapped option -> AMBIGUOUS_OPTION_OR_TARGET -> human PROVIDE_VALUE resolves
    bad_occ_dossier = _build_dossier(
        has_pool="No", extra_facts={"occupancy_kind": "Unrecognized Warehouse"}
    )
    surface_cb = FakeStatefulFormSurface()
    rec_cb = FormReconciler(surface_cb)
    sess_cb = rec_cb.create_session(pack, bad_occ_dossier)
    h_cb = rec_cb.run_until_halt_or_done(sess_cb)
    assert h_cb.halts[0].kind == FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET
    assert len(h_cb.halts[0].available_options) == 3

    with pytest.raises(ValueError, match="non-empty provided_value"):
        rec_cb.apply_human_resolution(
            sess_cb,
            HumanResolutionInput(
                halt_id=h_cb.halts[0].halt_id,
                action=HumanDecisionAction.PROVIDE_VALUE,
                actor_id="op",
                provided_value="   ",
                idempotency_key="ik_empty_val",
            ),
        )

    done_cb = rec_cb.apply_human_resolution(
        sess_cb,
        HumanResolutionInput(
            halt_id=h_cb.halts[0].halt_id,
            action=HumanDecisionAction.PROVIDE_VALUE,
            actor_id="op",
            provided_value="Primary Residence",
            idempotency_key="ik_valid_occ",
        ),
    )
    assert done_cb.outcome == FormTxOutcome.VERIFIED_DONE

    # 4. Screen transition budget exhaustion -> DELIVERY_UNCERTAIN
    surface_budget = FakeStatefulFormSurface()
    rec_budget = FormReconciler(surface_budget, max_screen_transitions=1)
    res_budget = rec_budget.run_until_halt_or_done(
        rec_budget.create_session(pack, dossier)
    )
    assert res_budget.outcome == FormTxOutcome.DELIVERY_UNCERTAIN
    assert res_budget.safe_to_retry is False
    assert res_budget.halts[0].kind == FormHaltKind.MUTATION_BUDGET_EXCEEDED


@pytest.mark.parametrize("contract", ["missing", "empty", "partial"])
def test_readback_contract_cannot_verify_an_unchecked_commit(contract: str) -> None:
    pack = _build_pack()
    check = pack.summary_check
    assert check is not None
    if contract == "missing":
        check = None
    elif contract == "empty":
        check = check.model_copy(update={"field_readbacks": ()})
    else:
        check = check.model_copy(update={"field_readbacks": check.field_readbacks[:1]})
    pack = pack.model_copy(update={"summary_check": check})
    result = FormReconciler(FakeStatefulFormSurface()).run_until_halt_or_done(
        ReconcilerSession(
            f"run_incomplete_{contract}", pack, _build_dossier(has_pool="No")
        )
    )
    # No contract at all is refuted; a contract that leaves typed fields
    # uncovered reports them as not_checked. Neither is ever verified.
    assert result.outcome == (
        FormTxOutcome.READBACK_REFUTED
        if contract == "missing"
        else FormTxOutcome.READBACK_INCOMPLETE
    )
    assert result.safe_to_retry is False
    assert result.receipt is None
    assert result.summary_verdict is not None
    assert result.summary_verdict.passed is False
    if contract != "missing":
        assert result.summary_verdict.not_checked_keys


def test_verified_receipt_requires_positive_readback_evidence() -> None:
    private, _ = generate_ed25519_keypair()
    for evidence in (
        {},
        {"readback_passed": True},
        {"readback_passed": False, "readback_checked_count": 3},
    ):
        with pytest.raises(ValueError, match="read-back evidence"):
            issue_form_tx_receipt(
                run_id="run_no_evidence",
                pack=_build_pack(),
                dossier=_build_dossier(has_pool="No"),
                signing_key=private,
                **evidence,
            )


# --- Read-back coverage: every typed or selected field gets a verdict -------
#
# On main before this change, fields typed inside a popup estimator
# (living_area_sqft, construction_year) were never part of the read-back, and
# a run that typed them could still end VERIFIED_DONE with a signed receipt.


class _SpySurface:
    """Wrap a surface and record every selector the layer writes to."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.written: set[str] = set()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _spy(self, selector: str) -> None:
        self.written.add(selector)

    def fill_text(self, dom_selector: str, value: str) -> None:
        self._spy(dom_selector)
        self._inner.fill_text(dom_selector, value)

    def select_native_option(self, dom_selector: str, option_value: str) -> None:
        self._spy(dom_selector)
        self._inner.select_native_option(dom_selector, option_value)

    def set_checkbox_or_radio(self, dom_selector: str, checked: bool = True) -> None:
        self._spy(dom_selector)
        self._inner.set_checkbox_or_radio(dom_selector, checked)

    def choose_portal_combobox_option(self, dom_selector: str, option: Any) -> None:
        self._spy(dom_selector)
        self._inner.choose_portal_combobox_option(dom_selector, option)

    @contextmanager
    def open_popup_surface(self, trigger_selector: str, **kwargs: Any) -> Iterator[Any]:
        with self._inner.open_popup_surface(trigger_selector, **kwargs) as popup:
            spy = _SpySurface(popup)
            yield spy
            self.written |= spy.written


def _run_pool_case(pack: FormPack) -> tuple[Any, ReconcilerSession, _SpySurface]:
    spy = _SpySurface(FakeStatefulFormSurface())
    reconciler = FormReconciler(spy)  # type: ignore[arg-type]
    session = reconciler.create_session(pack, _build_dossier(has_pool="Yes"))
    halted = reconciler.run_until_halt_or_done(session)
    assert halted.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    done = reconciler.apply_human_resolution(
        session,
        HumanResolutionInput(
            halt_id=halted.halts[0].halt_id,
            action=HumanDecisionAction.PROVIDE_VALUE,
            actor_id="op",
            provided_value="No",
            mapped_dossier_key="has_trampoline",
            idempotency_key="ik_cov_trampoline",
        ),
    )
    return done, session, spy


def test_popup_inputs_the_summary_omits_are_not_checked_and_never_verified() -> None:
    # Old bug: these two fields were typed but had no verdict at all, and the
    # run ended verified with a signed receipt.
    done, session, _ = _run_pool_case(_build_pack(readback_popup_inputs=False))
    assert done.outcome == FormTxOutcome.READBACK_INCOMPLETE
    assert done.receipt is None
    assert done.safe_to_retry is False
    verdict = done.summary_verdict
    assert verdict is not None
    assert verdict.passed is False
    assert verdict.failed_keys == ()
    # The popup inputs, plus the figure that came only from them.
    assert set(verdict.not_checked_keys) == {
        "construction_year",
        "living_area_sqft",
        "replacement_cost",
    }
    by_key = {v.dossier_key: v for v in verdict.field_verdicts}
    for key in ("construction_year", "living_area_sqft"):
        assert by_key[key].status == ReadbackStatus.NOT_CHECKED
        assert by_key[key].passed is False
        assert "re-read before apply" in by_key[key].reason
    assert by_key["replacement_cost"].status == ReadbackStatus.NOT_CHECKED
    assert "displayed result" in by_key["replacement_cost"].reason
    assert verdict.checked_count == len(verdict.field_verdicts) - 3


def test_popup_input_that_does_not_hold_its_value_stops_before_apply() -> None:
    surface = FakeStatefulFormSurface(popup_truncate_living_area=True)
    reconciler = FormReconciler(surface)
    session = reconciler.create_session(
        _build_pack(readback_popup_inputs=False), _build_dossier(has_pool="No")
    )
    result = reconciler.run_until_halt_or_done(session)
    assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert result.halts[0].field_label == "Living Area (Sq Ft)"
    # Nothing was applied or submitted.
    assert surface.values["replacement_cost"] == "$0.00"
    assert surface.committed_record is None


def test_every_written_selector_is_in_the_ledger_and_every_ledger_entry_has_a_verdict() -> (
    None
):
    done, session, spy = _run_pool_case(_build_pack())
    assert done.outcome == FormTxOutcome.VERIFIED_DONE
    ledger_selectors = {w.selector for w in session.writes}
    assert spy.written, "the spy saw no writes"
    assert spy.written <= ledger_selectors
    assert done.summary_verdict is not None
    verdict_keys = {v.dossier_key for v in done.summary_verdict.field_verdicts}
    for write in session.writes:
        assert write.field_key in verdict_keys, write
    assert {"living_area_sqft", "construction_year", "has_trampoline"} <= {
        w.field_key for w in session.writes
    }


def test_operator_value_on_unbound_control_is_not_checked() -> None:
    spy = _SpySurface(FakeStatefulFormSurface())
    reconciler = FormReconciler(spy)  # type: ignore[arg-type]
    session = reconciler.create_session(_build_pack(), _build_dossier(has_pool="Yes"))
    halted = reconciler.run_until_halt_or_done(session)
    done = reconciler.apply_human_resolution(
        session,
        HumanResolutionInput(
            halt_id=halted.halts[0].halt_id,
            action=HumanDecisionAction.PROVIDE_VALUE,
            actor_id="op",
            provided_value="No",
            idempotency_key="ik_unbound_trampoline",
        ),
    )
    # The summary shows a trampoline answer the layer cannot attribute to any
    # expected value, so the run is refuted; the operator's control itself
    # is reported as not_checked, never passed.
    assert done.outcome == FormTxOutcome.READBACK_REFUTED
    assert done.receipt is None
    assert done.summary_verdict is not None
    assert "control:Trampoline on Premises" in done.summary_verdict.not_checked_keys
    assert "has_trampoline" in done.summary_verdict.failed_keys


def test_a_mismatch_still_refutes_even_when_other_fields_are_unchecked() -> None:
    surface = FakeStatefulFormSurface(
        corrupt_summary_overrides={"phone_number": "415-555-9999"},
        drop_summary_fields=("last_name",),
    )
    result = FormReconciler(surface).run_until_halt_or_done(
        ReconcilerSession(_build_pack(), _build_dossier(has_pool="No"))
    )
    assert result.outcome == FormTxOutcome.READBACK_REFUTED
    assert result.summary_verdict is not None
    assert result.summary_verdict.failed_keys == ("phone_number",)
    assert result.summary_verdict.not_checked_keys == ("last_name",)


def test_verdict_and_receipt_models_refuse_a_pass_with_unchecked_fields() -> None:
    from openadapt_flow.form_tx.types import ReadbackFieldVerdict, SummaryVerdict

    with pytest.raises(ValueError):
        ReadbackFieldVerdict(
            dossier_key="k",
            codec_kind=FormCodecKind.EXACT,
            passed=True,
            status=ReadbackStatus.NOT_CHECKED,
            provenance=ProvenanceKind.INTAKE_DOCUMENT,
        )
    unchecked = ReadbackFieldVerdict(
        dossier_key="k",
        codec_kind=FormCodecKind.EXACT,
        passed=False,
        status=ReadbackStatus.NOT_CHECKED,
        provenance=ProvenanceKind.INTAKE_DOCUMENT,
    )
    with pytest.raises(ValueError):
        SummaryVerdict(passed=True, checked_count=1, field_verdicts=(unchecked,))
    verdict = SummaryVerdict(
        passed=False,
        checked_count=0,
        not_checked_keys=("k",),
        field_verdicts=(unchecked,),
    )
    private, _ = generate_ed25519_keypair()
    with pytest.raises(ValueError, match="read-back evidence"):
        issue_form_tx_receipt(
            run_id="run_unchecked",
            pack=_build_pack(),
            dossier=_build_dossier(has_pool="No"),
            signing_key=private,
            summary_verdict=verdict,
            readback_passed=True,
            readback_checked_count=3,
        )


def test_pre_apply_reread_halts_when_a_popup_value_changed_after_typing() -> None:
    parent = FakeStatefulFormSurface()
    pack = _build_pack(readback_popup_inputs=False)
    session = ReconcilerSession(pack, _build_dossier(has_pool="No"))
    subroutine = pack.popup_subroutines[0]
    session.record_write(
        source="auto_fill",
        dossier_key="living_area_sqft",
        label="Living Area (Sq Ft)",
        selector="#popup-living-area",
        in_popup_subroutine=subroutine.subroutine_id,
    )
    # The widget rewrote the value after it was typed (for example on blur).
    popup = FakePopupFormSurface(parent=parent, living_area_sqft="2,4")
    halt = FormReconciler(parent)._check_popup_entries_before_apply(
        session, popup, parent.snapshot(), subroutine
    )
    assert halt is not None
    assert halt.kind == FormHaltKind.POPUP_CONTRACT_FAILED
    assert "before apply" in halt.message
    (write,) = [w for w in session.writes if w.field_key == "living_area_sqft"]
    assert not write.checked_before_apply

    popup_ok = FakePopupFormSurface(parent=parent, living_area_sqft="2425")
    assert (
        FormReconciler(parent)._check_popup_entries_before_apply(
            session, popup_ok, parent.snapshot(), subroutine
        )
        is None
    )
    (write,) = [w for w in session.writes if w.field_key == "living_area_sqft"]
    assert write.checked_before_apply
