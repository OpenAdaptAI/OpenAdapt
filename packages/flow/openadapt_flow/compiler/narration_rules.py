"""Turn a spoken sentence into a proposed branch guard, and check it.

When ``induce`` (or ``teach-branch``) finds a branch, the recordings show
*that* the person did something different, not *why*. Narration often says
why: "if the insurance is Medicare, skip the copay screen". This module

1. parses such a sentence into a condition and an action
   (:func:`parse_rule`, deterministic; an optional compile-time
   :class:`NarrationInterpreter` may help on the customer's own key),
2. maps the condition onto something the runtime can check without a model:
   a recorded field (``param_equals`` on a run parameter) or a screen element
   (``text_present``), and
3. checks the proposed guard against every recording
   (:func:`check_against_recordings`).

A narrated guard is a **proposal**. It becomes a certified guard only when
every recording agrees with it, when ``induce --probe-on`` confirms it on a
test copy, or when a person accepts it. A recording that contradicts it keeps
the point refused. At run time the guard is an ordinary predicate: a string
compare or an OCR presence check, never a model call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable

from openadapt_flow.ir import Predicate, PredicateKind

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    """a an the is are was be to of on in at for and or but so then i we you it
    its it's this that they them their my our your just can will should would
    do does did dont don't not no need have has had go going now here there
    screen page step field box button tab form when if since because unless
    as with by from into out up down please also skip skipping skipped ignore
    leave omit bypass never always only don t s""".split()
)
_SKIP_VERBS = (
    "skip",
    "don't",
    "dont",
    "do not",
    "no need to",
    "ignore",
    "leave out",
    "bypass",
    "omit",
    "never",
    "won't",
    "will not",
)
_MARKERS = re.compile(
    r"\b(if|when|whenever|unless|since|because|as long as|only if|only when)\b",
    re.IGNORECASE,
)
_COPULA = re.compile(
    r"^(?P<subj>.*?)\s*\b(?P<cop>is not|isn't|isnt|is|are not|aren't|are|was|"
    r"equals|says|shows|reads|=)\s+(?P<val>.+)$",
    re.IGNORECASE,
)
_PRONOUN_SUBJECTS = frozenset(
    {"", "it", "this", "that", "they", "he", "she", "its", "it's"}
)


@dataclass(frozen=True)
class ParsedRule:
    """A sentence split into a condition and an action."""

    sentence: str
    value: str
    subject: Optional[str]
    negated: bool
    action: str
    skip: bool

    @property
    def runs_when_condition_holds(self) -> bool:
        """True: the action happens when the condition holds. A "skip" (or an
        "unless", or "is not") flips it."""
        return self.skip == self.negated


def tokens(text: str) -> set[str]:
    """Content words, lower-case, with a trailing plural ``s`` dropped."""
    out = set()
    for w in _WORD.findall((text or "").lower().replace("_", " ")):
        if w in _STOP or len(w) < 2:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return out


def _norm_value(text: str) -> str:
    text = re.sub(r"[\"'“”‘’]", "", text or "").strip().lower()
    text = re.sub(r"^(a|an|the)\s+", "", text)
    return re.sub(r"\s+", " ", text).strip(" .,;:!?")


def _clean_value(text: str) -> str:
    text = re.sub(r"[\"“”]", "", text or "").strip()
    text = re.sub(r"^(a|an|the)\s+", "", text, flags=re.IGNORECASE)
    # "Medicare, so" / "Medicare then" -> "Medicare"
    text = re.split(r"\s*(?:,|;|\bthen\b|\bso\b)\s*", text, maxsplit=1)[0]
    return text.strip(" .,;:!?")


_OTHERWISE = re.compile(r",?\s*\b(?:otherwise|or else|else)\b", re.I)
_UNCLEAR_NEGATION = re.compile(
    r"\b(?:not|no|never|n't|isn't|aren't|won't|don't)\b", re.I
)
_NOT_NEEDED = re.compile(
    r"^(?P<obj>.+?)\s+(?:is not|isn't|are not|aren't)\s+(?:needed|required|necessary)$"
    r"|^(?P<obj2>.+?)\s+(?:is|are)\s+(?:not needed|not required|unnecessary)$",
    re.I,
)


def _split_action(action: str) -> tuple[str, Optional[bool]]:
    """``(object, skip)``; ``skip`` is None when the negation is unclear."""
    action = _OTHERWISE.split(action, maxsplit=1)[0]
    a = re.sub(r"^(?:then|so|and)\s+", "", action.strip(" ,.;:!?"), flags=re.I)
    a = re.sub(
        r"^(?:i|we|you)\s+(?:can\s+|will\s+|should\s+|just\s+|always\s+|need to\s+)*",
        "",
        a,
        flags=re.I,
    ).strip()
    low = a.lower()
    blank = re.match(r"^leave\s+(?P<obj>.+?)\s+(?:blank|empty|alone)$", a, flags=re.I)
    if blank is not None:
        return blank.group("obj"), True
    needless = _NOT_NEEDED.match(a)
    if needless is not None:
        return needless.group("obj") or needless.group("obj2"), True
    for verb in _SKIP_VERBS:
        if low.startswith(verb + " ") or low == verb:
            rest = a[len(verb) :].strip()
            rest = re.sub(
                r"^(?:need to|have to|bother|fill in|do)\s+", "", rest, flags=re.I
            )
            # "don't skip X": a double negative is too easy to misread.
            if any(rest.lower().startswith(v + " ") for v in _SKIP_VERBS):
                return rest, None
            if _UNCLEAR_NEGATION.search(rest):
                return rest, None
            return rest, True
    if _UNCLEAR_NEGATION.search(a):
        return a, None
    return a, False


def parse_rule(sentence: str) -> Optional[ParsedRule]:
    """Split a narrated sentence into a condition and an action, or None.

    Handles the shapes people use while demonstrating:

    * "If the insurance is Medicare, skip the copay screen."
    * "Skip the copay screen when the insurance is Medicare."
    * "Since it's Medicare, I don't fill in the copay."
    * "The insurance is Medicare, so I skip copay."
    * "Unless the payer is Aetna, leave the referral blank."
    """
    text = re.sub(r"\s+", " ", (sentence or "").strip())
    if not text:
        return None
    marker = _MARKERS.search(text)
    negated = False
    if marker is not None:
        word = marker.group(1).lower()
        negated = word == "unless"
        after = text[marker.end() :]
        before = text[: marker.start()].strip(" ,.;:")
        boundary = re.search(r",|;|\bthen\b|\bso\b", after)
        if boundary is not None:
            condition = after[: boundary.start()]
            action = after[boundary.end() :]
        else:
            condition, action = after, ""
        # "..., otherwise enter it": only the first action is the rule.
        action = _OTHERWISE.split(action, maxsplit=1)[0]
        if not action.strip(" .,;:!?"):
            action = before
        elif before and not tokens(action):
            action = before
    else:
        so = re.search(r",?\s*\bso\b\s+", text)
        if so is None:
            return None
        condition, action = text[: so.start()], text[so.end() :]
    condition = condition.strip(" ,.;:!?")
    match = _COPULA.match(condition)
    if match is not None:
        subject = match.group("subj").strip()
        subject = re.sub(r"^(?:the|a|an|its|their)\s+", "", subject, flags=re.I)
        value = _clean_value(match.group("val"))
        cop = match.group("cop").lower()
        if cop in ("is not", "isn't", "isnt", "are not", "aren't"):
            negated = not negated
    else:
        lowered = condition.lower()
        subject = ""
        for pron in ("it's ", "it is ", "this is ", "they're ", "they are "):
            if lowered.startswith(pron):
                condition = condition[len(pron) :]
                break
        flipped = re.match(r"^(?:not|n't)\s+", condition, flags=re.I)
        if flipped is not None:
            condition = condition[flipped.end() :]
            negated = not negated
        value = _clean_value(condition)
    if re.match(r"^(?:not|no)\b", value, flags=re.I):
        return None  # a negation the parser did not place
    if subject.lower() in _PRONOUN_SUBJECTS:
        subject = ""
    action, skip = _split_action(action)
    if skip is None or not value or not action and not skip:
        return None
    return ParsedRule(
        sentence=text,
        value=value,
        subject=subject or None,
        negated=negated,
        action=action,
        skip=skip,
    )


# -- the compile-time model, behind an interface ------------------------------


@runtime_checkable
class NarrationInterpreter(Protocol):
    """Turns a sentence the parser could not read into a condition.

    Called at COMPILE time only, at most once per branch point. Returns
    ``{"field": <one of context["fields"]> | None, "text": <screen text> |
    None, "value": str, "step_runs_when_true": bool}`` or None. Whatever it
    returns is checked against the recordings like any narrated guard, and it
    may only name a field it was offered.
    """

    source: str

    def interpret(
        self, sentence: str, context: dict[str, Any]
    ) -> Optional[dict[str, Any]]: ...


class AnthropicNarrationInterpreter:
    """Compile-time interpreter on the customer's own Anthropic key.

    Imports the SDK lazily; constructing it needs no key. The sentence (already
    scrubbed) is scrubbed again on the way out, or the call is refused when
    openadapt-privacy is missing (``openadapt_flow.privacy.prepare_outbound``).
    Never used at run time.
    """

    source = "narration+model"

    def __init__(self, *, client: Any = None, model: Optional[str] = None) -> None:
        self._client = client
        self._model = model

    def interpret(
        self, sentence: str, context: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        import json

        from openadapt_flow import privacy

        payload = privacy.prepare_outbound(
            {"sentence": sentence, **context}, "narration rule interpretation"
        )
        client = self._client
        if client is None:
            import anthropic

            from openadapt_flow.benchmark.agent_baseline import load_api_key

            client = anthropic.Anthropic(api_key=load_api_key())
        model = self._model
        if model is None:
            from openadapt_flow.benchmark.agent_baseline import MODEL

            model = MODEL
        prompt = (
            "A person demonstrating a workflow said the sentence below. Say which "
            "condition decides whether the step runs. Use only a field from "
            "'fields', or text that would be on screen. Reply with JSON only: "
            '{"field": str|null, "text": str|null, "value": str, '
            '"step_runs_when_true": bool}. Reply null if the sentence names no '
            "condition.\n" + json.dumps(payload)
        )
        response = client.messages.create(
            model=model, max_tokens=200, messages=[{"role": "user", "content": prompt}]
        )
        raw = "".join(
            getattr(b, "text", "")
            for b in response.content
            if getattr(b, "type", "") == "text"
        ).strip()
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None


# -- mapping a rule onto a checkable guard ------------------------------------


@dataclass
class FieldValues:
    """A recorded field that became a run parameter, with each recording's value."""

    param: str
    label: str
    values: list[Optional[str]]


@dataclass
class Candidate:
    """A guard proposed from one sentence, before it is checked."""

    sentence: str
    trace: int
    step_id: Optional[str]
    condition: Predicate
    runs_when_condition_holds: bool
    source: str = "narration"
    target_arm: Optional[int] = None  # divergence: the arm the sentence is about

    @property
    def guard_id(self) -> str:
        """Short digest of the guard, so an acceptance or a probe result pins
        this exact guard and not whatever a later pass proposes."""
        from openadapt_flow.ir import predicate_contract_sha256

        return predicate_contract_sha256(self.guard)[:12]

    @property
    def guard(self) -> Predicate:
        """The predicate under which the step (or target arm) runs."""
        if self.runs_when_condition_holds:
            return self.condition
        return Predicate(
            kind=PredicateKind.NOT,
            operands=[self.condition],
            intent=f"not ({self.condition.intent})",
        )

    def describe(self, what: str) -> str:
        cond = self.condition
        if cond.kind is PredicateKind.PARAM_EQUALS:
            fact = f"{cond.param} is {cond.value!r}"
            nfact = f"{cond.param} is not {cond.value!r}"
        else:
            fact = f"the screen shows {cond.text!r}"
            nfact = f"the screen does not show {cond.text!r}"
        return f"{what} only when {fact if self.runs_when_condition_holds else nfact}"


def _match_field(
    subject: Optional[str], value: str, fields: Sequence[FieldValues]
) -> Optional[tuple[FieldValues, str]]:
    want = _norm_value(value)
    with_value = [
        (f, v)
        for f in fields
        for v in dict.fromkeys(x for x in f.values if x is not None)
        if _norm_value(v) == want
    ]
    if subject:
        subj = tokens(subject)
        named = [
            (f, v) for f, v in with_value if subj & (tokens(f.param) | tokens(f.label))
        ]
        if len({f.param for f, _ in named}) == 1:
            return named[0]
        if named:
            return None
    if len({f.param for f, _ in with_value}) == 1:
        return with_value[0]
    return None


def candidate_from_rule(
    rule: ParsedRule,
    *,
    trace: int,
    step_id: Optional[str],
    fields: Sequence[FieldValues],
) -> Candidate:
    """Map a parsed rule onto a recorded field when one matches, else the screen."""
    matched = _match_field(rule.subject, rule.value, fields)
    if matched is not None:
        f, recorded = matched
        condition = Predicate(
            kind=PredicateKind.PARAM_EQUALS,
            param=f.param,
            value=recorded,
            intent=f"{f.param} is {recorded}",
        )
    else:
        condition = Predicate(
            kind=PredicateKind.TEXT_PRESENT,
            text=rule.value,
            intent=f"{rule.value} on screen",
        )
    return Candidate(
        sentence=rule.sentence,
        trace=trace,
        step_id=step_id,
        condition=condition,
        runs_when_condition_holds=rule.runs_when_condition_holds,
    )


def candidate_from_model(
    data: dict[str, Any],
    *,
    sentence: str,
    trace: int,
    step_id: Optional[str],
    fields: Sequence[FieldValues],
    source: str,
) -> Optional[Candidate]:
    """Validate an interpreter's answer. It may only name an offered field and
    a value some recording actually had; anything else is dropped."""
    value = str(data.get("value") or "").strip()
    runs = data.get("step_runs_when_true")
    if not value or not isinstance(runs, bool):
        return None  # only a real JSON boolean says which way the guard goes
    name = data.get("field")
    if name:
        f = next((x for x in fields if x.param == name), None)
        if f is None:
            return None
        recorded = next(
            (
                v
                for v in f.values
                if v is not None and _norm_value(v) == _norm_value(value)
            ),
            None,
        )
        if recorded is None:
            return None
        condition = Predicate(
            kind=PredicateKind.PARAM_EQUALS,
            param=f.param,
            value=recorded,
            intent=f"{f.param} is {recorded}",
        )
    else:
        text = str(data.get("text") or value).strip()
        # The screen text must be words the person actually said.
        if not text or _norm_value(text) not in _norm_value(sentence):
            return None
        condition = Predicate(
            kind=PredicateKind.TEXT_PRESENT, text=text, intent=f"{text} on screen"
        )
    return Candidate(
        sentence=sentence,
        trace=trace,
        step_id=step_id,
        condition=condition,
        runs_when_condition_holds=runs,
        source=source,
    )


def is_about(rule: ParsedRule, descriptor: set[str], *, in_window: bool) -> bool:
    """Does the rule's action talk about this step (or arm)?

    An action with content words must share one with the step. An action with
    none ("skip it") counts only when the sentence was spoken right there.
    """
    words = tokens(rule.action)
    if not words:
        return in_window
    return bool(words & descriptor)


# -- checking a guard against the recordings ----------------------------------

ScreenTexts = Callable[[int, str], Optional[Sequence[str]]]


@dataclass
class Agreement:
    agree: list[int] = field(default_factory=list)
    contradict: list[int] = field(default_factory=list)
    unknown: list[int] = field(default_factory=list)

    @property
    def all_agree(self) -> bool:
        return bool(self.agree) and not self.contradict and not self.unknown


def _holds(
    pred: Predicate,
    trace: int,
    fields: Sequence[FieldValues],
    screen: Optional[Sequence[str]],
) -> Optional[bool]:
    kind = pred.kind
    if kind is PredicateKind.NOT:
        inner = _holds(pred.operands[0], trace, fields, screen)
        return None if inner is None else not inner
    if kind is PredicateKind.PARAM_EQUALS:
        f = next((x for x in fields if x.param == pred.param), None)
        if f is None or trace >= len(f.values) or f.values[trace] is None:
            return None
        # The run-time check is an exact string compare
        # (runtime.program_predicates); check the recordings the same way, so
        # "medicare" and "Medicare" are different values here too.
        return f.values[trace] == pred.value
    if kind is PredicateKind.TEXT_PRESENT:
        if screen is None:
            return None
        want = _norm_value(pred.text or "")
        return any(want and want in _norm_value(line) for line in screen)
    return None


def check_against_recordings(
    guard: Predicate,
    expected: dict[int, bool],
    *,
    fields: Sequence[FieldValues],
    screen_step: dict[int, Optional[str]],
    screen_texts: Optional[ScreenTexts] = None,
) -> Agreement:
    """Evaluate ``guard`` in every recording and compare with what it did.

    ``expected[t]`` is True when recording ``t`` took the guarded path.
    ``screen_step[t]`` names the step whose before-screen is where the guard
    would be checked in recording ``t`` (for screen-element guards).
    """
    out = Agreement()
    for t, took in sorted(expected.items()):
        screen = None
        step_id = screen_step.get(t)
        if screen_texts is not None and step_id is not None:
            screen = screen_texts(t, step_id)
        holds = _holds(guard, t, fields, screen)
        if holds is None:
            out.unknown.append(t)
        elif holds == took:
            out.agree.append(t)
        else:
            out.contradict.append(t)
    return out


def recording_screen_texts(recording_dirs: dict[int, Any]) -> ScreenTexts:
    """Screen text at a step, read by OCR from a recording's before-frame.

    ``recording_dirs`` maps trace index -> recording directory. Steps are
    ``step_<event index>``. Returns None when the frame or OCR is missing, so
    the guard stays unchecked rather than guessed.
    """
    from pathlib import Path

    cache: dict[tuple[int, str], Optional[list[str]]] = {}

    def read(trace: int, step_id: str) -> Optional[Sequence[str]]:
        key = (trace, step_id)
        if key in cache:
            return cache[key]
        texts: Optional[list[str]] = None
        root = recording_dirs.get(trace)
        m = re.fullmatch(r"step_(\d+)", step_id or "")
        if root is not None and m:
            frame = Path(root) / "frames" / f"{int(m.group(1)):04d}_before.png"
            if frame.is_file():
                try:
                    from openadapt_flow.vision.ocr import ocr

                    texts = [line.text for line in ocr(frame.read_bytes())]
                except Exception:  # noqa: BLE001 - no OCR means unchecked
                    texts = None
        cache[key] = texts
        return texts

    return read
