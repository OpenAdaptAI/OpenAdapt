"""Error text that never quotes the input it rejected.

A Pydantic validation error quotes the offending value (``input_value=...``).
When that value was decrypted from a sealed artifact, printing the error
would write private content to stdout, stderr and any log that captures them
(docs/phi_at_rest.md). Every refusal the CLI prints goes through
:func:`message`, which drops those quoted values, and a failure caused by
sealed or strict-mode input prints :func:`withheld` instead of its text.
"""

from __future__ import annotations

import re

# Pydantic renders ``[type=..., input_value=<repr>, input_type=...]`` on one
# line; a repr never spans lines. Greedy, so a value that itself contains
# ", input_type=" is dropped whole.
_INPUT_VALUE = re.compile(r"input_value=.*, input_type=")
_ANY_INPUT_VALUE = re.compile(r"input_value=[^\n]*")


def message(error: BaseException | str) -> str:
    """``error`` as text, with every quoted input value removed."""
    text = _INPUT_VALUE.sub("input_type=", str(error))
    return _ANY_INPUT_VALUE.sub("input_value withheld]", text)


def root_cause(error: BaseException) -> BaseException:
    """The first error in ``error``'s chain."""
    seen = {id(error)}
    while True:
        cause = error.__cause__ or error.__context__
        if cause is None or id(cause) in seen:
            return error
        seen.add(id(cause))
        error = cause


def withheld(error: BaseException) -> str:
    """A refusal that names only the kind of the error that caused it."""
    return (
        f"the input was refused ({type(root_cause(error)).__name__}). The "
        "details are withheld because the input is sealed or strict mode is on."
    )
