# vendored from d84e57c6f85e:packages/flow/openadapt_flow/form_tx/judge.py, lines 437-463, for the harness self-test only (re-indented)
# ruff: noqa: F401, F841, I001
"""S4 pre-fix: a written field with no expected value and an empty summary gets no verdict (#2429).

Only the lines between ``# fmt: off`` and ``# fmt: on`` markers that
follow a ``# vendored:`` comment are verbatim; the wrapper ``def`` lines,
the ``return`` and the stubs above them are not."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def _not_checked(key: str, prov: Any, reason: str) -> dict[str, Any]:
    return {"key": key, "passed": False, "status": "not_checked", "reason": reason}


def _failed(key: str, kind: Any, prov: Any, reason: str) -> dict[str, Any]:
    return {"key": key, "passed": False, "status": "failed", "reason": reason}


def _is_placeholder_or_empty(value: str, placeholders: Any) -> bool:
    return not value.strip() or value in (placeholders or ())


def _where(_written: Any) -> str:
    return ""


def judge(
    bindings: list[dict[str, Any]],
    facts: dict[str, Any],
    screen: dict[str, str],
    written: list[str],
) -> bool:
    verdicts: list[dict[str, Any]] = []
    written_by_key = {k: k for k in written}
    for spec in bindings:
        binding = SimpleNamespace(
            dossier_key=spec["key"],
            required_in_pack=spec.get("required", False),
            placeholder_values=(),
            codec=SimpleNamespace(kind="text"),
        )
        fact = (
            SimpleNamespace(value=facts[spec["key"]], provenance="intake")
            if spec["key"] in facts
            else None
        )
        prov = "intake"
        shown = screen.get(spec["key"])
        observed_ctrl = None if shown is None else SimpleNamespace(current_value=shown)
        # vendored: d84e57c6f85e:packages/flow/openadapt_flow/form_tx/judge.py 437-463
        # fmt: off
        if fact is None:
            has_ungrounded_screen_val = (
                observed_ctrl is not None
                and not _is_placeholder_or_empty(
                    observed_ctrl.current_value, binding.placeholder_values
                )
            )
            if has_ungrounded_screen_val:
                verdicts.append(
                    _failed(
                        binding.dossier_key,
                        binding.codec.kind,
                        prov,
                        f"Summary screen showed unexpected non-empty value for "
                        f"ungrounded key {binding.dossier_key!r}",
                    )
                )
            elif binding.required_in_pack:
                verdicts.append(
                    _not_checked(
                        binding.dossier_key,
                        prov,
                        f"Required summary read-back key {binding.dossier_key!r} "
                        f"has no expected value to compare",
                    )
                )
            continue
        # fmt: on
        if observed_ctrl is None:
            verdicts.append(_not_checked(binding.dossier_key, prov, "control missing"))
            continue
        ok = observed_ctrl.current_value == fact.value
        verdicts.append({"key": binding.dossier_key, "passed": ok})
    return bool(verdicts) and all(v["passed"] for v in verdicts)
