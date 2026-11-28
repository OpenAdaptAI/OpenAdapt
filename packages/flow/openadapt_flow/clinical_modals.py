"""Declarative clinical and VDI modal policy pack classifier.

Classifies unexpected dialogs using declarative rules rather than per-app
hardcoded checks. Unknown modals and clinical safety alerts (such as drug or
allergy warnings and unsaved-changes prompts) always halt for a human operator.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "ModalCategory",
    "ModalClassification",
    "ModalDisposition",
    "ModalPolicyPack",
    "ModalRule",
    "ModalSnapshot",
    "default_clinical_modal_policy_pack",
]


class ModalCategory(str, Enum):
    SESSION_TIMEOUT = "session_timeout"
    CONFIRMATION_PROMPT = "confirmation_prompt"
    DRUG_ALLERGY_WARNING = "drug_allergy_warning"
    UNSAVED_CHANGES = "unsaved_changes"
    UNKNOWN = "unknown"


class ModalDisposition(str, Enum):
    KEEP_ALIVE = "keep_alive"
    CONFIRM_CONTROLLED = "confirm_controlled"
    DISMISS_SAFE = "dismiss_safe"
    HALT_FOR_HUMAN = "halt_for_human"


@dataclass(frozen=True)
class ModalSnapshot:
    """Observed text and buttons of an active modal dialog."""

    title: str = ""
    body: str = ""
    buttons: tuple[str, ...] = ()
    role: str = "dialog"


@dataclass(frozen=True)
class ModalRule:
    """One declarative policy rule matching modal text and buttons."""

    rule_id: str
    category: ModalCategory
    disposition: ModalDisposition
    priority: int = 100
    any_text_terms: tuple[str, ...] = ()
    all_text_terms: tuple[str, ...] = ()
    any_button_terms: tuple[str, ...] = ()
    recommended_button_terms: tuple[str, ...] = ()
    description: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModalRule":
        return cls(
            rule_id=str(data["rule_id"]),
            category=ModalCategory(data["category"]),
            disposition=ModalDisposition(data["disposition"]),
            priority=int(data.get("priority", 100)),
            any_text_terms=tuple(str(t) for t in data.get("any_text_terms", ())),
            all_text_terms=tuple(str(t) for t in data.get("all_text_terms", ())),
            any_button_terms=tuple(str(t) for t in data.get("any_button_terms", ())),
            recommended_button_terms=tuple(
                str(t) for t in data.get("recommended_button_terms", ())
            ),
            description=str(data.get("description", "")),
        )


@dataclass(frozen=True)
class ModalClassification:
    """Result of classifying a modal against a :class:`ModalPolicyPack`."""

    category: ModalCategory
    disposition: ModalDisposition
    rule_id: Optional[str]
    matched_terms: tuple[str, ...]
    recommended_button: Optional[str]
    requires_human: bool
    reason: str


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _find_matching_button(
    buttons: Sequence[str], preferred_terms: Sequence[str]
) -> Optional[str]:
    norm_buttons = [(btn, _norm(btn)) for btn in buttons]
    for term in preferred_terms:
        t_norm = _norm(term)
        if not t_norm:
            continue
        for original, b_norm in norm_buttons:
            if b_norm == t_norm or t_norm in b_norm:
                return original
    return None


class ModalPolicyPack:
    """Declarative policy pack evaluating modal snapshots in priority order."""

    def __init__(self, rules: Sequence[ModalRule], *, name: str = "custom") -> None:
        self.name = name
        self.rules: tuple[ModalRule, ...] = tuple(
            sorted(rules, key=lambda r: (r.priority, r.rule_id))
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModalPolicyPack":
        name = str(data.get("name", "custom"))
        raw_rules = data.get("rules", ())
        rules = [ModalRule.from_dict(item) for item in raw_rules]
        return cls(rules, name=name)

    def classify(self, modal: ModalSnapshot) -> ModalClassification:
        """Classify ``modal`` against the pack; unknown modals halt for human."""
        combined_text = _norm(f"{modal.title} {modal.body}")
        norm_buttons = [_norm(b) for b in modal.buttons]

        for rule in self.rules:
            matched: list[str] = []

            if rule.all_text_terms:
                if not all(_norm(t) in combined_text for t in rule.all_text_terms):
                    continue
                matched.extend(rule.all_text_terms)

            if rule.any_text_terms:
                hits = [t for t in rule.any_text_terms if _norm(t) in combined_text]
                if not hits:
                    continue
                matched.extend(hits)

            if rule.any_button_terms:
                btn_hits = [
                    t
                    for t in rule.any_button_terms
                    if any(_norm(t) in b for b in norm_buttons)
                ]
                if not btn_hits:
                    continue
                matched.extend(btn_hits)

            if not (
                rule.all_text_terms or rule.any_text_terms or rule.any_button_terms
            ):
                continue

            if rule.disposition is ModalDisposition.HALT_FOR_HUMAN:
                return ModalClassification(
                    category=rule.category,
                    disposition=ModalDisposition.HALT_FOR_HUMAN,
                    rule_id=rule.rule_id,
                    matched_terms=tuple(matched),
                    recommended_button=None,
                    requires_human=True,
                    reason=rule.description
                    or f"policy rule {rule.rule_id} requires human review",
                )

            rec_btn = _find_matching_button(
                modal.buttons, rule.recommended_button_terms
            )
            if rule.recommended_button_terms and rec_btn is None:
                return ModalClassification(
                    category=rule.category,
                    disposition=ModalDisposition.HALT_FOR_HUMAN,
                    rule_id=rule.rule_id,
                    matched_terms=tuple(matched),
                    recommended_button=None,
                    requires_human=True,
                    reason=(
                        f"matched {rule.rule_id} but no expected button was present; "
                        "halting for human"
                    ),
                )

            return ModalClassification(
                category=rule.category,
                disposition=rule.disposition,
                rule_id=rule.rule_id,
                matched_terms=tuple(matched),
                recommended_button=rec_btn,
                requires_human=False,
                reason=rule.description or f"matched policy rule {rule.rule_id}",
            )

        return ModalClassification(
            category=ModalCategory.UNKNOWN,
            disposition=ModalDisposition.HALT_FOR_HUMAN,
            rule_id=None,
            matched_terms=(),
            recommended_button=None,
            requires_human=True,
            reason="unclassified modal dialog; halting for human review",
        )


def default_clinical_modal_policy_pack() -> ModalPolicyPack:
    """Return the default declarative clinical and VDI modal policy pack."""
    return ModalPolicyPack(
        name="clinical-vdi-default-v1",
        rules=(
            ModalRule(
                rule_id="clinical.drug_allergy_warning",
                category=ModalCategory.DRUG_ALLERGY_WARNING,
                disposition=ModalDisposition.HALT_FOR_HUMAN,
                priority=10,
                any_text_terms=(
                    "allergy alert",
                    "allergy warning",
                    "drug interaction",
                    "drug-allergy",
                    "contraindication",
                    "adverse reaction",
                    "duplicate therapy",
                    "high alert medication",
                    "dose range warning",
                ),
                description="clinical drug or allergy warning requires clinician review",
            ),
            ModalRule(
                rule_id="clinical.unsaved_changes",
                category=ModalCategory.UNSAVED_CHANGES,
                disposition=ModalDisposition.HALT_FOR_HUMAN,
                priority=20,
                any_text_terms=(
                    "unsaved changes",
                    "discard changes",
                    "leave without saving",
                    "changes will be lost",
                    "not been saved",
                ),
                description="unsaved changes prompt requires operator decision",
            ),
            ModalRule(
                rule_id="vdi.session_timeout",
                category=ModalCategory.SESSION_TIMEOUT,
                disposition=ModalDisposition.KEEP_ALIVE,
                priority=30,
                any_text_terms=(
                    "session timeout",
                    "session expiring",
                    "session will expire",
                    "idle timeout",
                    "due to inactivity",
                ),
                recommended_button_terms=(
                    "stay signed in",
                    "continue session",
                    "extend session",
                    "keep alive",
                    "continue",
                    "resume",
                ),
                description="idle session timeout can be extended via keep-alive button",
            ),
            ModalRule(
                rule_id="ui.confirmation_prompt",
                category=ModalCategory.CONFIRMATION_PROMPT,
                disposition=ModalDisposition.CONFIRM_CONTROLLED,
                priority=40,
                any_text_terms=(
                    "are you sure",
                    "please confirm",
                    "confirm action",
                    "do you want to proceed",
                ),
                recommended_button_terms=("confirm", "yes", "ok", "proceed"),
                description="generic confirmation prompt",
            ),
        ),
    )
