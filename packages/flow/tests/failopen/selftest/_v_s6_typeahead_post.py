# vendored from c329c57dc217:packages/flow/openadapt_flow/form_tx/reconciler.py, lines 273-322, for the harness self-test only (re-indented)
# vendored from c329c57dc217:packages/flow/openadapt_flow/form_tx/reconciler.py, lines 391-406, for the harness self-test only (re-indented)
# ruff: noqa: F401, F841, I001
"""S6 post-fix: only text this run typed allows a leading-part match (c329c57dc).

Only the lines between ``# fmt: off`` and ``# fmt: on`` markers that
follow a ``# vendored:`` comment are verbatim; the wrapper ``def`` lines,
the ``return`` and the stubs above them are not."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

ObservedControl = Any
FieldBindingSpec = Any


class ControlKind(str, Enum):
    CHECKBOX = "checkbox"
    PORTAL_COMBOBOX = "portal_combobox"
    TEXT = "text"


class CodecValueError(ValueError):
    pass


def collapse_whitespace(text: str) -> str:
    return " ".join(str(text).split())


def normalize_boolean_yes_no(value: Any) -> str:
    raise CodecValueError(value)


def format_for_text_entry(value: Any, codec: Any) -> str:
    return str(value)


def values_equivalent(a: Any, b: Any, codec: Any) -> bool:
    return collapse_whitespace(a).casefold() == collapse_whitespace(b).casefold()


# vendored: c329c57dc217:packages/flow/openadapt_flow/form_tx/reconciler.py 273-322
# fmt: off
def _control_matches_fact(
    ctrl: ObservedControl,
    intended_value: Any,
    binding: FieldBindingSpec,
    *,
    typed_query: Optional[str] = None,
) -> bool:
    """True when ``ctrl`` shows ``intended_value`` under the binding's codec.

    ``typed_query`` is what this run actually typed into a type-ahead before
    picking. Only then may a leading part of the pick count as a match; a
    value already in the field, or one the run did not type, must match in
    full.
    """
    if binding.control_kind == ControlKind.CHECKBOX and ctrl.checked is not None:
        try:
            yn = normalize_boolean_yes_no(intended_value)
        except CodecValueError:
            return False
        return (yn == "yes") == bool(ctrl.checked)

    if (
        binding.control_kind == ControlKind.PORTAL_COMBOBOX
        and binding.typeahead_query_chars is not None
        and typed_query
    ):
        # A type-ahead that keeps only the leading part of its pick (see
        # _is_leading_part_of_choice); typed text alone never matches.
        try:
            formatted = format_for_text_entry(intended_value, binding.codec)
        except CodecValueError:
            formatted = ""
        if formatted and _is_leading_part_of_choice(
            ctrl.current_value,
            formatted,
            typed_query,
        ):
            return True

    if values_equivalent(intended_value, ctrl.current_value, binding.codec):
        return True

    if ctrl.options:
        matched_opts = [opt for opt in ctrl.options if opt.value == ctrl.current_value]
        if len(matched_opts) == 1 and values_equivalent(
            intended_value, matched_opts[0].label, binding.codec
        ):
            return True

    return False
# fmt: on


# vendored: c329c57dc217:packages/flow/openadapt_flow/form_tx/reconciler.py 391-406
# fmt: off
def _is_leading_part_of_choice(shown: str, label: str, typed: str) -> bool:
    """True when a pick left the leading part of its label in the input.

    Some type-aheads put only the first part of the chosen suggestion in the
    input ("2210 Juniper Loop" from "2210 Juniper Loop Apt 4, Brackenfield,
    OR") and fill the rest into other fields. That counts only when the input
    shows more than was typed, so text left over from typing never passes.
    """
    shown_n = collapse_whitespace(shown).casefold()
    label_n = collapse_whitespace(label).casefold()
    typed_n = collapse_whitespace(typed).casefold()
    if not shown_n or shown_n == typed_n or len(shown_n) <= len(typed_n):
        return False
    if not label_n.startswith(shown_n) or len(label_n) == len(shown_n):
        return False
    return label_n[len(shown_n)] in " ,;-/("
# fmt: on
