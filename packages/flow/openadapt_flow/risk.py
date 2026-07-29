"""Compile-time risk classification.

Heuristically infers whether a compiled :class:`~openadapt_flow.ir.Step`
performs a CONSEQUENTIAL, hard-to-undo write (``risk="irreversible"``) versus
a benign, repeatable action (``risk="reversible"``), from the step's intent and
its target's label / OCR text.

Why this exists
---------------
Historically every step compiled as ``reversible`` unless a human passed
``risk_overrides`` (see ``docs/LIMITS.md`` — "Risk classification is opt-in and
never auto-assigned"). That left the irreversible-step safeguards
(below-OCR-rung refusal, unreadable-identity-band refusal in
:class:`~openadapt_flow.runtime.Replayer`) UNREACHABLE from a default compile —
so a wrong-patient write behind an unreadable identity band proceeded with a
green report. This classifier turns those safeguards ON by default for
write-shaped steps. ``risk_overrides`` still wins (an operator can always force
a step either way).

Heuristic
---------
Pointer submissions, Enter/Delete, hotkeys, drags, file operations, and
API/FHIR/MCP/tool bindings can classify irreversible. Clipboard operations,
indirect navigation, and icon-only controls require review when retained
evidence cannot prove their business effect or data sensitivity. The step's
``intent`` and its anchor labels are scanned for consequential-write verbs on
word boundaries, so ``address`` does not trip ``add``. Reusable bounded
application rules can classify controls whose business meaning is specific to
one application. Built-in strong write signals keep precedence, and conflicting
rules require qualification. Every inference retains PHI-free evidence tokens
plus an explanation.

False-positive posture
----------------------
DELIBERATELY biased toward ``irreversible`` on write-shaped steps. A false
irreversible costs AVAILABILITY (an over-strict refusal, or a ``certify``
failure a human must clear); a false reversible costs SAFETY (a wrong,
unguarded write reported as success). We take the cheap error. Concretely,
labels like "Apply filter" or "Add to favourites" classify irreversible even
though they are cheap to undo — the safe direction. Qualification can override
these classifications with a recorded explanation; compilation does not infer
that an ambiguous actuator is harmless.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal, cast

from openadapt_flow.ir import ActionKind, PostconditionKind, Step

MAX_RISK_EVIDENCE = 8
MAX_APPLICATION_RISK_RULES = 8
_EVIDENCE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


@dataclass(frozen=True)
class RiskInference:
    """Compile-time classification plus its qualification disposition."""

    risk: Literal["reversible", "irreversible"]
    explanation: str
    evidence: tuple[str, ...]
    requires_review: bool = False

    def __post_init__(self) -> None:
        if self.risk not in {"reversible", "irreversible"}:
            raise ValueError("risk inference requires a valid risk")
        if (
            not self.explanation
            or len(self.explanation) > 512
            or any(character in self.explanation for character in "\r\n")
        ):
            raise ValueError("risk explanation must be one bounded line")
        if not 1 <= len(self.evidence) <= MAX_RISK_EVIDENCE:
            raise ValueError("risk inference requires bounded structured evidence")
        if len(set(self.evidence)) != len(self.evidence):
            raise ValueError("risk evidence tokens must be unique")
        if any(_EVIDENCE_RE.fullmatch(item) is None for item in self.evidence):
            raise ValueError("risk evidence must contain bounded PHI-free tokens")


@dataclass(frozen=True)
class ApplicationRiskRule:
    """A bounded, reusable application-specific compile-time risk rule.

    A rule can select an action kind, an exact retained top-level window name,
    retained text terms, or a conjunction of those selectors. Built-in strong
    write signals keep precedence, so a rule cannot silently downgrade a Save,
    API call, sensitive clipboard operation, or other known consequential act.
    """

    rule_id: str
    risk: Literal["reversible", "irreversible"]
    explanation: str
    actions: tuple[ActionKind, ...] = ()
    window_names: tuple[str, ...] = ()
    text_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _RULE_ID_RE.fullmatch(self.rule_id) is None:
            raise ValueError("application risk rule id must be a bounded safe token")
        if self.risk not in {"reversible", "irreversible"}:
            raise ValueError("application risk rule requires a valid risk")
        if (
            not self.explanation
            or len(self.explanation) > 240
            or any(character in self.explanation for character in "\r\n")
        ):
            raise ValueError(
                "application risk rule explanation must be one bounded line"
            )
        if not (self.actions or self.window_names or self.text_terms):
            raise ValueError("application risk rule requires at least one selector")
        if (
            len(self.actions) > 8
            or any(not isinstance(action, ActionKind) for action in self.actions)
            or len({action.value for action in self.actions}) != len(self.actions)
        ):
            raise ValueError("application risk rule actions must be unique and bounded")
        for values, label in (
            (self.window_names, "window names"),
            (self.text_terms, "text terms"),
        ):
            if len(values) > 8 or any(not isinstance(value, str) for value in values):
                raise ValueError(
                    f"application risk rule {label} must be unique and bounded"
                )
            if len({value.casefold() for value in values}) != len(values):
                raise ValueError(
                    f"application risk rule {label} must be unique and bounded"
                )
            if any(
                not value.strip()
                or len(value) > 64
                or any(character in value for character in "\r\n")
                for value in values
            ):
                raise ValueError(
                    f"application risk rule {label} must contain bounded one-line values"
                )

    def matches(self, step: Step) -> bool:
        if self.actions and step.action not in self.actions:
            return False
        if self.window_names:
            structural = step.anchor.structural if step.anchor is not None else None
            window_name = structural.window_name if structural is not None else None
            if window_name is None or window_name.casefold() not in {
                item.casefold() for item in self.window_names
            }:
                return False
        text = step_text(step).casefold()
        return not self.text_terms or all(
            term.casefold() in text for term in self.text_terms
        )

    def config_dict(self) -> dict[str, object]:
        """Return the deterministic, JSON-safe compiler-config representation."""

        return {
            "actions": [action.value for action in self.actions],
            "window_names": list(self.window_names),
            "explanation": self.explanation,
            "risk": self.risk,
            "rule_id": self.rule_id,
            "text_terms": list(self.text_terms),
        }


_APPLICATION_RULE_KEYS = frozenset(
    {
        "actions",
        "window_names",
        "explanation",
        "risk",
        "rule_id",
        "text_terms",
    }
)


def application_risk_rules_from_data(value: object) -> tuple[ApplicationRiskRule, ...]:
    """Parse the closed JSON-compatible application-rule configuration."""

    if not isinstance(value, list):
        raise ValueError("application risk rules must be a JSON array")
    if len(value) > MAX_APPLICATION_RISK_RULES:
        raise ValueError(
            f"at most {MAX_APPLICATION_RISK_RULES} application risk rules are allowed"
        )
    rules: list[ApplicationRiskRule] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or any(not isinstance(key, str) for key in item):
            raise ValueError(f"application risk rule {index} must be a JSON object")
        unknown = set(item) - _APPLICATION_RULE_KEYS
        if unknown:
            raise ValueError(
                f"application risk rule {index} has unknown fields: "
                f"{', '.join(sorted(unknown))}"
            )
        risk = item.get("risk")
        if risk not in {"reversible", "irreversible"}:
            raise ValueError(f"application risk rule {index} has an invalid risk")

        def string_tuple(field: str) -> tuple[str, ...]:
            raw = item.get(field, [])
            if not isinstance(raw, list) or any(
                not isinstance(entry, str) for entry in raw
            ):
                raise ValueError(
                    f"application risk rule {index} field {field!r} "
                    "must be a string array"
                )
            return tuple(raw)

        action_values = string_tuple("actions")
        try:
            actions = tuple(ActionKind(action) for action in action_values)
        except ValueError as exc:
            raise ValueError(
                f"application risk rule {index} has an unknown action"
            ) from exc
        rule_id = item.get("rule_id")
        explanation = item.get("explanation")
        if not isinstance(rule_id, str) or not isinstance(explanation, str):
            raise ValueError(
                f"application risk rule {index} requires rule_id and explanation"
            )
        rules.append(
            ApplicationRiskRule(
                rule_id=rule_id,
                risk=cast(Literal["reversible", "irreversible"], risk),
                explanation=explanation,
                actions=actions,
                window_names=string_tuple("window_names"),
                text_terms=string_tuple("text_terms"),
            )
        )
    if len({rule.rule_id.casefold() for rule in rules}) != len(rules):
        raise ValueError("application risk rule ids must be unique")
    return tuple(rules)


# Consequential-write verb stems. Each is matched case-insensitively on WORD
# boundaries against the step's combined text, so `add` matches "+Add" and
# "Add note" but NOT "address", and `post` (were it present) would not match
# "postal". Ordered roughly by how common they are on real controls.
#
# Deliberately EXCLUDED to avoid noisy false positives on navigation chrome:
# bare "sign" (collides with "sign in"; the writing sense is covered by
# save/submit/confirm and the explicit "sign up" below), bare "post"/"order"/
# "book"/"complete" (collide with "Posts"/"Orders"/"Bookings"/"Completed" tabs).
_WRITE_STEMS: tuple[str, ...] = (
    r"sav(?:e|es|ing)",
    r"submit(?:s|ted|ting)?",
    r"confirm(?:s|ed|ing)?",
    r"continue(?:s|d|ing)?",
    r"next",
    r"cancel(?:s|ed|ing)?",
    r"creat(?:e|es|ing)",
    r"delet(?:e|es|ing)",
    r"remov(?:e|es|ing)",
    r"updat(?:e|es|ing)",
    r"send(?:s|ing)?",
    r"publish(?:es|ed|ing)?",
    r"pay(?:s|ing)?",
    r"sign[\s\-_]?up",
    r"signup",
    r"regist(?:er|ers|ering|ration)",
    r"enroll(?:s|ed|ing|ment)?",
    r"add(?:s|ed|ing)?",
    r"insert(?:s|ed|ing)?",
    r"appl(?:y|ies|ied)",
    r"approv(?:e|es|ed|ing)",
    r"accept(?:s|ed|ing)?",
    r"transfer(?:s|red|ring)?",
    r"upload(?:s|ed|ing)?",
    r"overwrit(?:e|es|ing)",
    r"discard(?:s|ed|ing)?",
    r"archiv(?:e|es|ing)",
    r"finaliz(?:e|es|ing)",
    r"finalise",
    r"checkout",
    r"check[\s\-_]?out",
    r"purchas(?:e|es|ing)",
    r"place[\s\-_]order",
)

# One alternation, word-boundary anchored. `\b` around a leading/trailing
# non-word char (e.g. "+add") still matches because the boundary sits between
# the "+" and "a".
_WRITE_RE = re.compile(r"\b(?:" + "|".join(_WRITE_STEMS) + r")\b", re.IGNORECASE)

_FILE_WRITE_RE = re.compile(
    r"\b(?:download(?:s|ed|ing)?|export(?:s|ed|ing)?|import(?:s|ed|ing)?|"
    r"rename(?:s|d|ing)?(?:\s+(?:the\s+)?file)?|"
    r"replace(?:s|d|ing)?\s+(?:the\s+)?file|"
    r"write(?:s|written|writing)?\s+(?:a\s+|the\s+)?file)\b",
    re.IGNORECASE,
)

_SENSITIVE_CONTEXT_RE = re.compile(
    r"\b(?:secret|password|passcode|credential|token|api[\s_-]?key|phi|pii|"
    r"patient|member[\s_-]?id|medical[\s_-]?record|mrn|date[\s_-]?of[\s_-]?birth|"
    r"dob|social[\s_-]?security|ssn)\b",
    re.IGNORECASE,
)

_COMMAND_MODIFIERS = frozenset({"control", "ctrl", "meta", "command", "cmd"})
_NAVIGATION_POSTCONDITIONS = frozenset(
    {
        PostconditionKind.URL_CHANGED,
        PostconditionKind.TITLE_CHANGED,
        PostconditionKind.NEW_TAB_OPENED,
    }
)
_FILE_COMMIT_ACTIONS = frozenset(
    {
        ActionKind.CLICK,
        ActionKind.DOUBLE_CLICK,
        ActionKind.RIGHT_CLICK,
        ActionKind.DRAG,
        ActionKind.SELECT_OPTION,
        ActionKind.KEY,
        ActionKind.HOTKEY,
    }
)


def _evidence_value(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return normalized[:64] or "present"


def _matched_token(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return _evidence_value(match.group(0)) if match is not None else None


def _navigation_transition(step: Step) -> PostconditionKind | None:
    return next(
        (
            postcondition.kind
            for postcondition in step.expect
            if postcondition.kind in _NAVIGATION_POSTCONDITIONS
        ),
        None,
    )


def _clipboard_operation(step: Step) -> str | None:
    if step.action is not ActionKind.HOTKEY:
        return None
    key = (step.key or "").casefold()
    modifiers = {value.casefold() for value in step.modifiers}
    command = bool(modifiers & _COMMAND_MODIFIERS)
    if command and key in {"c", "insert"}:
        return "copy"
    if command and key == "x":
        return "cut"
    if (command and key == "v") or ("shift" in modifiers and key == "insert"):
        return "paste"
    return None


def _application_rule_inference(
    step: Step,
    rules: tuple[ApplicationRiskRule, ...],
) -> RiskInference | None:
    matched = tuple(
        (index, rule) for index, rule in enumerate(rules) if rule.matches(step)
    )
    if not matched:
        return None
    # The rule ID and selectors are operator-authored. They can contain an
    # application record identifier even though the configuration contract
    # asks operators not to put one there. Retain only the bounded position in
    # the compiler's deterministic rule sequence as PHI-free evidence. The
    # compiler-config SHA-256 still binds the complete exact rule set.
    evidence = tuple(f"application_rule:index_{index}" for index, _ in matched)
    risks = {rule.risk for _, rule in matched}
    if len(risks) != 1:
        return RiskInference(
            "reversible",
            "configured application risk rules conflict and require operator review",
            evidence,
            requires_review=True,
        )
    risk = matched[0][1].risk
    if len(matched) == 1:
        explanation = (
            f"configured application risk rule at index {matched[0][0]} "
            f"classifies the action {risk}"
        )
    else:
        explanation = (
            f"{len(matched)} configured application rules consistently classify "
            f"the action {risk}"
        )
    return RiskInference(risk, explanation, evidence)


def is_write_shaped(text: str) -> bool:
    """True if ``text`` names a consequential-write action (see module doc)."""
    return bool(text) and _WRITE_RE.search(text) is not None


def step_text(step: Step) -> str:
    """The text a click step's risk is inferred from: its intent plus its
    target's retained OCR and structural-accessibility labels (the intent
    already embeds the label for ordinary labelled clicks, but a coordinate
    click may carry none, and a healed anchor may carry fresher evidence than
    the frozen intent)."""
    parts = [step.intent or ""]
    if step.anchor is not None and step.anchor.ocr_text:
        parts.append(step.anchor.ocr_text)
    structural = step.anchor.structural if step.anchor is not None else None
    if structural is not None and structural.name:
        parts.append(structural.name)
    if step.field_label:
        parts.append(step.field_label)
    drag_end = step.drag_end_anchor
    if drag_end is not None and drag_end.ocr_text:
        parts.append(drag_end.ocr_text)
    drag_end_structural = drag_end.structural if drag_end is not None else None
    if drag_end_structural is not None and drag_end_structural.name:
        parts.append(drag_end_structural.name)
    return " ".join(parts)


def infer_step_risk(
    step: Step,
    *,
    application_rules: Iterable[ApplicationRiskRule] = (),
) -> RiskInference:
    """Infer risk while retaining why qualification must review ambiguity."""

    rules = tuple(application_rules)
    if len(rules) > MAX_APPLICATION_RISK_RULES:
        raise ValueError(
            f"at most {MAX_APPLICATION_RISK_RULES} application risk rules are allowed"
        )
    if len({rule.rule_id.casefold() for rule in rules}) != len(rules):
        raise ValueError("application risk rule ids must be unique")
    action_evidence = f"action:{step.action.value}"
    text = step_text(step)
    write_keyword = _matched_token(_WRITE_RE, text)
    file_write = _matched_token(_FILE_WRITE_RE, text)

    if step.api_binding is not None:
        method = step.api_binding.method.upper()
        safe_method = (
            method.casefold()
            if method in {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}
            else "configured"
        )
        return RiskInference(
            "irreversible",
            "the step declares an API, FHIR, MCP, or tool actuation binding",
            (
                action_evidence,
                f"binding:{step.api_binding.kind}",
                f"operation:{safe_method}",
            ),
        )

    if file_write is not None and step.action in _FILE_COMMIT_ACTIONS:
        return RiskInference(
            "irreversible",
            "retained context names a local or remote file write",
            (action_evidence, f"file_write:{file_write}"),
        )

    if step.action is ActionKind.SELECT_OPTION:
        if write_keyword is not None:
            return RiskInference(
                "irreversible",
                "option commit context names a consequential state change",
                (action_evidence, f"write_keyword:{write_keyword}"),
            )
        configured = _application_rule_inference(step, rules)
        if configured is not None:
            return configured
        return RiskInference(
            "reversible",
            "option commit may edit a form locally or trigger application state",
            (action_evidence, "ambiguity:option_commit"),
            requires_review=True,
        )
    if step.action is ActionKind.DRAG:
        if write_keyword is not None:
            return RiskInference(
                "irreversible",
                "drag context names a consequential state change",
                (action_evidence, f"write_keyword:{write_keyword}"),
            )
        configured = _application_rule_inference(step, rules)
        if configured is not None:
            return configured
        return RiskInference(
            "reversible",
            "drag may select, move, reorder, or mutate application state",
            (action_evidence, "ambiguity:drag_drop"),
            requires_review=True,
        )
    if step.action is ActionKind.HOTKEY:
        key = (step.key or "").lower()
        modifiers = {value.lower() for value in step.modifiers}
        clipboard_operation = _clipboard_operation(step)
        sensitive_marker = _matched_token(_SENSITIVE_CONTEXT_RE, text)
        if clipboard_operation is not None and sensitive_marker is not None:
            return RiskInference(
                "irreversible",
                "a clipboard operation carries retained sensitive-data context",
                (
                    action_evidence,
                    f"clipboard:{clipboard_operation}",
                    f"sensitivity:{sensitive_marker}",
                ),
            )
        if clipboard_operation is not None:
            configured = _application_rule_inference(step, rules)
            if configured is not None:
                return configured
            return RiskInference(
                "reversible",
                "clipboard content sensitivity and destination effect are not proven",
                (action_evidence, f"clipboard:{clipboard_operation}"),
                requires_review=True,
            )
        if key == "s" and bool(modifiers & _COMMAND_MODIFIERS):
            return RiskInference(
                "irreversible",
                "the shortcut commits a local or remote file write",
                (action_evidence, "shortcut:file_write"),
            )
        write_chord = (
            key in {"enter", "return"} and bool(modifiers & _COMMAND_MODIFIERS)
        ) or (key in {"delete", "backspace"} and "shift" in modifiers)
        if write_chord or write_keyword is not None:
            signal = (
                "shortcut:write" if write_chord else f"write_keyword:{write_keyword}"
            )
            return RiskInference(
                "irreversible",
                "hotkey chord or retained context names a consequential write",
                (action_evidence, signal),
            )
        configured = _application_rule_inference(step, rules)
        if configured is not None:
            return configured
        return RiskInference(
            "reversible",
            "hotkey may navigate, select, edit, submit, or mutate application state",
            (action_evidence, "ambiguity:shortcut"),
            requires_review=True,
        )
    if step.action is ActionKind.KEY:
        ambiguous = (step.key or "").lower() in {
            "enter",
            "return",
            "delete",
            "backspace",
        }
        consequential = ambiguous and write_keyword is not None
        if consequential:
            return RiskInference(
                "irreversible",
                "submission/deletion key has retained consequential-write context",
                (action_evidence, f"write_keyword:{write_keyword}"),
            )
        configured = _application_rule_inference(step, rules)
        if configured is not None:
            return configured
        navigation = _navigation_transition(step)
        if navigation is not None:
            return RiskInference(
                "reversible",
                "the key caused navigation whose business effect is not proven",
                (action_evidence, f"transition:{navigation.value}"),
                requires_review=True,
            )
        return RiskInference(
            "reversible",
            (
                "submission/deletion key may edit locally or commit application state"
                if ambiguous
                else "special key may navigate, edit, or invoke an application shortcut"
            ),
            (
                (action_evidence, "ambiguity:submission_deletion_key")
                if ambiguous
                else (action_evidence, "ambiguity:special_key")
            ),
            requires_review=True,
        )
    if write_keyword is not None and step.action in {
        ActionKind.CLICK,
        ActionKind.DOUBLE_CLICK,
        ActionKind.RIGHT_CLICK,
    }:
        return RiskInference(
            "irreversible",
            "retained action context names a consequential state change",
            (action_evidence, f"write_keyword:{write_keyword}"),
        )
    configured = _application_rule_inference(step, rules)
    if configured is not None:
        return configured
    navigation = _navigation_transition(step)
    if navigation is not None:
        return RiskInference(
            "reversible",
            "navigation occurred but its business-side effect is not proven",
            (action_evidence, f"transition:{navigation.value}"),
            requires_review=True,
        )
    if step.action not in (
        ActionKind.CLICK,
        ActionKind.DOUBLE_CLICK,
        ActionKind.RIGHT_CLICK,
    ):
        return RiskInference(
            "reversible",
            "action has no retained consequential commit signal",
            (action_evidence, "signal:no_commit"),
        )
    structural = step.anchor.structural if step.anchor is not None else None
    target_labels = (
        (step.anchor.ocr_text if step.anchor is not None else None),
        (structural.name if structural is not None else None),
    )
    if step.anchor is None or not any(
        isinstance(value, str) and value.strip() for value in target_labels
    ):
        # An unlabelled primary click may be a focus/navigation action or an
        # icon-only write. Demo replay remains usable, but no policy may certify
        # the unresolved classification until qualification supplies an
        # explicit override.
        return RiskInference(
            "reversible",
            "unlabelled pointer control may be navigation, focus, or a state change",
            (action_evidence, "ambiguity:icon_only"),
            requires_review=True,
        )
    return RiskInference(
        "reversible",
        "control label has no retained consequential-write signal",
        (action_evidence, "signal:labelled_non_write"),
    )


def classify_step_risk(
    step: Step,
    *,
    application_rules: Iterable[ApplicationRiskRule] = (),
) -> Literal["reversible", "irreversible"]:
    """Return the inferred risk; use :func:`infer_step_risk` for rationale.

    Proven write-shaped drags/hotkeys/key presses and write-shaped labelled
    clicks classify irreversible. Ambiguous rich actions and icon-only primary
    clicks remain provisionally reversible but require explicit qualification
    review before certification.
    """
    return infer_step_risk(step, application_rules=application_rules).risk
