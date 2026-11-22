"""The two interfaces this package codes against before they reach main.

- ``openadapt_flow.verdict_sites.verdict_site`` (#2476) registers a verdict
  site with the fail-open harness. Until it is on main, the decorator here
  records the id on the function and changes nothing else.
- ``openadapt_types.check_level.outcome_word`` and ``level_for_basis``
  (#2495) choose the receipt word and say which level a basis reaches. Until
  they are on main, no word is chosen (``None``) and no basis is accepted
  for another channel. Nothing here defines what ``VERIFIED`` means.

Delete this module once both are on main and import them directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional, TypeVar

from openadapt_types import check_level as _check_level

F = TypeVar("F", bound=Callable[..., Any])

try:  # pragma: no cover - which branch runs depends on what is merged
    from openadapt_flow.verdict_sites import verdict_site as verdict_site
except ImportError:  # pragma: no cover

    def verdict_site(id: str, *, classes: str) -> Callable[[F], F]:  # type: ignore[no-redef]
        """Record ``id`` on the function, as #2476's decorator does."""

        def mark(fn: F) -> F:
            setattr(fn, "__verdict_site__", id)
            return fn

        return mark


_level_for_basis: Optional[Callable[..., Any]] = getattr(
    _check_level, "level_for_basis", None
)
_outcome_word: Optional[Callable[..., Any]] = getattr(
    _check_level, "outcome_word", None
)


def has_ladder_words() -> bool:
    """True when ``check_level.outcome_word`` is available."""

    return _outcome_word is not None


def reaches(basis: Optional[str], level: Any) -> bool:
    """True when ``basis`` is a known basis that reaches exactly ``level``."""

    if basis is None or _level_for_basis is None:
        return False
    try:
        return bool(_level_for_basis(basis) == level)
    except ValueError:
        return False


def word(level: Any, basis: Optional[str], *, confirmed: bool) -> Optional[str]:
    """The receipt word for ``level`` and ``basis``, or None before #2495."""

    if _outcome_word is None:
        return None
    return str(_outcome_word(level, basis, confirmed=confirmed))
