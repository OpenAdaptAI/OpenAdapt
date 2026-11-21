"""Backend-agnostic surface protocol and anchor matching for ``form_tx``.

Allows ``FormReconciler``, ``SummaryJudge``, and ``WarmSessionLease`` to drive
Playwright/CDP web browsers, native desktop accessibility backends, or
deterministic test surfaces through one contract with zero per-app logic.
"""

from __future__ import annotations

import re
from typing import ContextManager, Optional, Protocol, Sequence, runtime_checkable

from openadapt_flow.form_tx.codecs import collapse_whitespace
from openadapt_flow.form_tx.types import (
    FieldAnchorSpec,
    FormSnapshot,
    ObservedControl,
    ObservedOption,
    UIProfileSpec,
)


class AmbiguousAnchorError(LookupError):
    """Raised when a ``FieldAnchorSpec`` matches multiple visible controls."""


class MissingAnchorError(LookupError):
    """Raised when a ``FieldAnchorSpec`` matches zero visible controls."""


@runtime_checkable
class FormSurfaceBackend(Protocol):
    """Protocol implemented by any UI actuator/observer participating in ``form_tx``."""

    @property
    def ui_profile(self) -> UIProfileSpec: ...

    @property
    def mutation_count(self) -> int:
        """Total state-changing requests (POST/PUT/PATCH/DELETE) sent so far."""
        ...

    def reset_mutation_counter(self) -> None: ...

    def snapshot(
        self,
        *,
        entity_tables: Sequence[object] = (),
        modal_rules: Sequence[object] = (),
        screens: Sequence[object] = (),
    ) -> FormSnapshot:
        """Wait for surface quiescence and capture a fresh ``FormSnapshot``."""
        ...

    def goto(self, url_or_path: str) -> None:
        """Navigate to ``url_or_path`` and wait for quiescence."""
        ...

    def fill_text(self, dom_selector: str, value: str) -> None:
        """Focus, clear, enter ``value`` into ``dom_selector``, blur, and wait for settle."""
        ...

    def select_native_option(self, dom_selector: str, option_value: str) -> None:
        """Select ``option_value`` in a native ``<select>`` and wait for settle."""
        ...

    def set_checkbox_or_radio(self, dom_selector: str, checked: bool = True) -> None:
        """Set a checkbox or radio control and wait for settle."""
        ...

    def open_portal_combobox_options(
        self, dom_selector: str
    ) -> tuple[ObservedOption, ...]:
        """Open a detached/portal combobox and read its rendered options."""
        ...

    def choose_portal_combobox_option(
        self, dom_selector: str, option: ObservedOption
    ) -> None:
        """Choose ``option`` in an open portal combobox and wait for settle."""
        ...

    def click(self, dom_selector: str) -> None:
        """Click ``dom_selector`` and wait for settle."""
        ...

    def open_popup_surface(
        self,
        trigger_selector: str,
        *,
        url_pattern: Optional[str] = None,
        title_pattern: Optional[str] = None,
    ) -> ContextManager["FormSurfaceBackend"]:
        """Click ``trigger_selector``, bind the resulting child window, and yield its surface."""
        ...

    def ping_keepalive(self, path: str) -> int:
        """Issue a read-only keepalive GET inside the session and return HTTP status."""
        ...


def _matches_any_pattern(text: str, patterns: Sequence[str]) -> bool:
    if not patterns:
        return True
    norm = collapse_whitespace(text)
    for pat in patterns:
        if re.search(pat, norm, flags=re.IGNORECASE):
            return True
    return False


def match_anchor_in_snapshot(
    anchor: FieldAnchorSpec,
    snapshot: FormSnapshot,
    *,
    include_readonly: bool = False,
) -> Optional[ObservedControl]:
    """Deterministically resolve ``anchor`` to at most one ``ObservedControl``.

    Raises ``AmbiguousAnchorError`` when more than one visible control matches.
    Returns ``None`` when no visible control matches (for example, a conditional
    field that has not materialized on the current screen).
    """
    candidates: list[ObservedControl] = []
    for ctrl in snapshot.controls:
        if not ctrl.visible or ctrl.is_secret:
            continue
        if not include_readonly and (ctrl.disabled or ctrl.readonly):
            continue
        if anchor.css_selector and anchor.css_selector != ctrl.dom_selector:
            # When an explicit css_selector is the sole signal, require exact match
            if not anchor.label_patterns and not anchor.name_or_id_patterns:
                continue
        if anchor.role and ctrl.role.casefold() != anchor.role.casefold():
            continue
        if anchor.section_patterns and not _matches_any_pattern(
            ctrl.section_text, anchor.section_patterns
        ):
            continue
        label_ok = (
            _matches_any_pattern(ctrl.label_text, anchor.label_patterns)
            or (
                is_radio(ctrl)
                and bool(ctrl.group_label)
                and _matches_any_pattern(ctrl.group_label, anchor.label_patterns)
            )
            if anchor.label_patterns
            else True
        )
        attr_target = f"{ctrl.name_attr} {ctrl.id_attr}".strip()
        name_ok = (
            _matches_any_pattern(attr_target, anchor.name_or_id_patterns)
            if anchor.name_or_id_patterns
            else True
        )
        if not label_ok or not name_ok:
            continue
        if (
            anchor.css_selector == ctrl.dom_selector
            or anchor.label_patterns
            or anchor.name_or_id_patterns
        ):
            candidates.append(ctrl)

    candidates = collapse_radio_groups(candidates)

    if len(candidates) > 1 and anchor.label_patterns and anchor.name_or_id_patterns:
        both = [
            c
            for c in candidates
            if _matches_any_pattern(c.label_text, anchor.label_patterns)
            and _matches_any_pattern(
                f"{c.name_attr} {c.id_attr}".strip(), anchor.name_or_id_patterns
            )
        ]
        if len(both) == 1:
            return both[0]

    if len(candidates) > 1:
        labels = [
            f"{c.label_text or c.dom_selector} ({c.dom_selector})" for c in candidates
        ]
        raise AmbiguousAnchorError(
            f"Anchor {anchor.model_dump(exclude_none=True)} matched {len(candidates)} controls: {labels}"
        )
    if len(candidates) == 1:
        return candidates[0]
    return None


def is_native_radio(ctrl: ObservedControl) -> bool:
    """True for an ``<input type="radio">`` control."""
    return ctrl.tag.casefold() == "input" and ctrl.input_type.casefold() == "radio"


def is_radio(ctrl: ObservedControl) -> bool:
    """True for a native radio or an ARIA ``role="radio"`` control."""
    return is_native_radio(ctrl) or ctrl.role.casefold() == "radio"


def radio_group_key(ctrl: ObservedControl) -> Optional[str]:
    """The group a native radio belongs to, or ``None`` for any other control.

    Radios sharing a ``name`` are one field. The key is the name plus the
    sorted values of the group, so two same-named groups in different forms
    with different choices stay apart.
    """
    values = ",".join(sorted(o.value for o in ctrl.options))
    if is_radio(ctrl) and ctrl.radio_group:
        return f"radio:{ctrl.radio_group}|{values}"
    if not is_native_radio(ctrl) or not ctrl.name_attr:
        return None
    return f"radio:{ctrl.name_attr}|{values}"


def collapse_radio_groups(
    controls: Sequence[ObservedControl],
) -> list[ObservedControl]:
    """Represent each native radio group by one control.

    The representative is the checked radio, so its ``current_value`` is the
    group's answer; with none checked it is the first radio, whose
    ``current_value`` is empty. Other controls keep their order.
    """
    groups: dict[str, list[ObservedControl]] = {}
    for ctrl in controls:
        key = radio_group_key(ctrl)
        if key is not None:
            groups.setdefault(key, []).append(ctrl)
    out: list[ObservedControl] = []
    seen: set[str] = set()
    for ctrl in controls:
        key = radio_group_key(ctrl)
        if key is None:
            out.append(ctrl)
            continue
        if key in seen:
            continue
        seen.add(key)
        members = groups[key]
        checked = [m for m in members if m.checked]
        out.append(checked[0] if checked else members[0])
    return out


def compute_field_signature(ctrl: ObservedControl) -> str:
    """Stable value-free structural signature for an observed control."""
    section = collapse_whitespace(ctrl.section_text).casefold()
    label = collapse_whitespace(ctrl.label_text).casefold()
    kind = (ctrl.role or ctrl.input_type or ctrl.tag).casefold()
    name = (ctrl.name_attr or ctrl.id_attr).casefold()
    if is_radio(ctrl) and (ctrl.radio_group or ctrl.name_attr):
        # Every radio of a group, checked or not, is the same field.
        label = collapse_whitespace(ctrl.group_label).casefold()
        name = (ctrl.radio_group or ctrl.name_attr).casefold()
    # Strip volatile numeric render suffixes from dynamic server IDs while keeping field tokens
    stable_name = re.sub(r"\b\d+\b|_[0-9a-f]{6,}", "#", name)
    return f"{section}|{label}|{kind}|{stable_name}"
