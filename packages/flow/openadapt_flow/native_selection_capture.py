"""Corroborate a changed native choice with its literal typed label.

These are passive recording/compiler checks, never option discovery at replay.
The exact committed option still comes from the backend's guarded readback.
"""

from openadapt_flow.ir import NativeSelectContract


def demonstrated_native_choice(
    contract: NativeSelectContract,
    *,
    value_before: object,
    label_before: object,
    typed_text: object,
) -> bool:
    """Require an observed change and an exact or unique enabled label match."""
    if (
        not isinstance(value_before, str)
        or not isinstance(label_before, str)
        or not isinstance(typed_text, str)
        or not typed_text
        or value_before == contract.value
    ):
        return False
    previous = [option for option in contract.options if option.value == value_before]
    if len(previous) != 1 or previous[0].label != label_before:
        return False
    literal = typed_text.casefold()
    enabled = [option for option in contract.options if not option.disabled]
    # A complete label remains meaningful beside longer labels (Dog/Dogwood).
    exact = [option for option in enabled if option.label.casefold() == literal]
    matches = exact or [
        option for option in enabled if option.label.casefold().startswith(literal)
    ]
    return len(matches) == 1 and matches[0].value == contract.value
