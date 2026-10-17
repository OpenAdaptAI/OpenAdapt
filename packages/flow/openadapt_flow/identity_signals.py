"""Strict, explicit transforms for qualified identity signals.

This module is deliberately independent of the qualification and runtime
models.  It is shared by compile-time identity-template construction and
run-time comparison so an operator-selected normalizer has one definition.
There is no fuzzy matching or OCR-confusion folding on this path.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from itertools import combinations
from typing import Iterable, Mapping, Sequence

NORMALIZER_ORDER = (
    "unicode_nfkc",
    "casefold",
    "collapse_whitespace",
    "strip_punctuation",
)


def canonical_normalizers(values: Iterable[object]) -> tuple[str, ...]:
    """Return and validate one explicit normalizer sequence.

    The order is fixed so compile-time hashes and replay-time comparisons
    cannot give the same set of transforms different semantics.
    """

    result = tuple(
        value.value if hasattr(value, "value") else str(value) for value in values
    )
    if len(result) != len(set(result)):
        raise ValueError("identity normalizers must not be repeated")
    unknown = sorted(set(result).difference(NORMALIZER_ORDER))
    if unknown:
        raise ValueError("unknown identity normalizer(s): " + ", ".join(unknown))
    expected = tuple(item for item in NORMALIZER_ORDER if item in result)
    if result != expected:
        raise ValueError(
            "identity normalizers must use canonical order: "
            + ", ".join(NORMALIZER_ORDER)
        )
    return result


def normalize_signal_text(text: str, normalizers: Iterable[object]) -> str:
    """Apply only the explicitly selected transforms, in canonical order."""

    result = text
    for normalizer in canonical_normalizers(normalizers):
        if normalizer == "unicode_nfkc":
            result = unicodedata.normalize("NFKC", result)
        elif normalizer == "casefold":
            result = result.casefold()
        elif normalizer == "collapse_whitespace":
            result = " ".join(result.split())
        elif normalizer == "strip_punctuation":
            result = "".join(
                char
                for char in result
                if not unicodedata.category(char).startswith("P")
            )
    return result


def signal_hash_key(
    source: object,
    match: object,
    normalizers: Iterable[object] = (),
    *,
    extract_pattern: str | None = None,
    parameter_names: Iterable[str] = (),
) -> str:
    """Stable key for one retained-source comparison contract.

    An extracted semantic field must never reuse the full-source hash slot.
    Only the extractor's digest is retained, not captured identity content.
    """

    source_name = source.value if hasattr(source, "value") else str(source)
    match_name = match.value if hasattr(match, "value") else str(match)
    normalized = canonical_normalizers(normalizers)
    suffix = ",".join(normalized)
    key = f"{source_name}|{match_name}|{suffix}"
    if extract_pattern is not None:
        digest = hashlib.sha256(extract_pattern.encode("utf-8")).hexdigest()
        key += f"|extract-sha256:{digest}"
        params = ",".join(sorted(parameter_names))
        params_digest = hashlib.sha256(params.encode("utf-8")).hexdigest()
        key += f"|params-sha256:{params_digest}"
    return key


def supported_normalizer_sets() -> tuple[tuple[str, ...], ...]:
    """Every non-empty subset in canonical order (15 sets for four transforms)."""

    return tuple(
        subset
        for size in range(1, len(NORMALIZER_ORDER) + 1)
        for subset in combinations(NORMALIZER_ORDER, size)
    )


def parameterize_identity_text(
    text: str,
    values: Mapping[str, str],
    *,
    names: Sequence[str] | None = None,
    minimum_chars: int = 4,
    case_sensitive: bool = False,
) -> tuple[str, list[str]]:
    """Replace exact run-specific values with stable non-value sentinels.

    This lets a retained hash bind the surrounding identity structure while
    the named workflow parameter changes per run. Replacement is exact,
    boundary-aware, and limited to the explicitly supplied ``names`` when that
    argument is present. A parameter value such as ``John`` therefore never
    claims the ``John`` prefix in ``Johnson``.
    """

    result = text
    candidates = list(names) if names is not None else list(values)
    candidates.sort(key=lambda name: len(str(values.get(name, ""))), reverse=True)
    used: list[str] = []
    for name in candidates:
        value = str(values.get(name, ""))
        if len("".join(value.split())) < minimum_chars:
            continue
        flags = 0 if case_sensitive else re.IGNORECASE
        # A whole parameter value may begin/end in punctuation (``(555)``,
        # ``--AB--``) while still being embedded in a larger word-shaped
        # identifier (``X(555)Y``). Guard the OUTSIDE edges whenever the value
        # contains any word character; looking only at value[0]/value[-1]
        # would silently parameterize those embedded substrings.
        has_word = any(char.isalnum() or char == "_" for char in value)
        left_boundary = r"(?<!\w)" if has_word else ""
        right_boundary = r"(?!\w)" if has_word else ""
        pattern = re.compile(
            left_boundary + re.escape(value) + right_boundary,
            flags,
        )
        if not pattern.search(result):
            continue
        result = pattern.sub(f"__OPENADAPT_PARAM_{name}__", result)
        used.append(name)
    return result, used
