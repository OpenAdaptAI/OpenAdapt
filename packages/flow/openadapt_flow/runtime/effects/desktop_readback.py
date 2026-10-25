"""Separate-session desktop read-back: look a saved record up the way a
person does, in a desktop, RDP, or Citrix session that is not the one that
typed.

``ui_readback`` covers web applications. Many systems a team automates have
no web page to open by URL: a Windows client, an app published over RDP or
Citrix, a kiosk with no accessibility tree. A person still checks a save in
those systems the same way: search for the record, open it, and read it. This
verifier does exactly that after the save:

1. opens its own session: a new RDP connection with its own credentials, or a
   window or Windows session that the deployment declares for read-back only.
   It never reads through the session or window that typed;
2. optionally signs in through the application's own sign-in form;
3. types the record's identifier or name into the search box and submits it;
4. if a results list appears, opens the one result that shows the record's
   identifier (zero matches is "not found" only when the application shows
   its not-found marker; two or more is ambiguous);
5. confirms that the record view shows the requested identifier;
6. reads the declared fields through UI Automation (the control's value or
   name) or through OCR on the screen pixels;
7. judges them with the shared effect judge, exactly as ``ui_readback`` and
   the REST, SQL, and FHIR verifiers do.

Read-only by construction. The look-up may type only into the search box (and
the sign-in fields while signing in). It may press only Enter in the search
box, select the search box's own text before retyping it, and click only the
declared search button, sign-in button, and one result row. Before any click
it reads the control's label; a label with a save-like word (Save, Submit,
Sign, Post, Delete, and so on) stops the check. A broken rule stops the check
with INDETERMINATE and the run halts.

What this is not. The read goes through the same application, so it is not a
system-of-record read (``independent_system_of_record = False``). With its own
sign-in (an RDP logon with the read-back credentials, or the application's
sign-in form) it declares
:attr:`~openadapt_flow.verification.VerificationTier.INDEPENDENT_SESSION`
(tier 2, check level ``separate_session``), the same as ``ui_readback``.
Without its own sign-in it declares
:attr:`~openadapt_flow.verification.VerificationTier.PERSISTED_STATE_REACQUISITION`
(tier 3, ``reopened``): a separate window re-reads saved state, but as the same
user. Its assurance grade is ``screen_readback`` either way.

On the pixel path OpenAdapt cannot prove where keyboard focus is. It clicks
the search box, types, and checks that the box then shows exactly the query
before it submits; any difference stops the check. Use a read-only account
for the read-back session.

Fail-safe posture, as for every verifier: a session that does not open, a
sign-in that does not complete, a control that is missing or matched twice, a
record view for another identifier, an OCR read below the confidence floor, a
shortened value, or a date that does not parse each give INDETERMINATE. Only
an affirmative contradiction on a readable record view gives REFUTED. A
record the search does not find is never proof that nothing was saved.
"""

from __future__ import annotations

import os
import re
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.assurance import SCREEN_READBACK, UNCHECKED
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.adapter import ConnectionProbe, VerifierAdapterBase
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    ValueExpr,
    Verdict,
)
from openadapt_flow.runtime.effects.ui_readback import (
    DEFAULT_TRUNCATION_MARKERS,
    blocking_uncertain_fields,
    normalize_value,
    normalized_effect,
    relabel_screen_read,
    without_absence_claim,
)
from openadapt_flow.verification import VerificationTier

SUBSTRATE = "desktop-readback"

#: Words that mark a control the look-up must never click. Matched as whole
#: words, case-insensitively, so "Submitted" in a status column is not "Submit".
SAVE_LIKE_WORDS: frozenset[str] = frozenset(
    {
        "accept",
        "add",
        "apply",
        "approve",
        "book",
        "cancel",
        "change",
        "clear",
        "close",
        "commit",
        "complete",
        "confirm",
        "create",
        "delete",
        "discard",
        "edit",
        "export",
        "file",
        "finalise",
        "finalize",
        "finish",
        "insert",
        "modify",
        "new",
        "ok",
        "order",
        "pay",
        "post",
        "print",
        "release",
        "remove",
        "reset",
        "save",
        "schedule",
        "send",
        "sign",
        "submit",
        "transmit",
        "update",
        "upload",
        "void",
        "yes",
    }
)

#: Phrases a sign-in button may show. Only the sign-in click may contain them.
_SIGN_IN_PHRASES = ("sign in", "sign on", "log in", "log on")


def save_like_words(text: str, *, sign_in: bool = False) -> list[str]:
    """Save-like words in a control's label (empty when it is safe to click).

    With ``sign_in`` (the sign-in form, before any record is open) the
    phrases "sign in", "log in" and their variants, and "OK" and "Submit",
    are allowed.
    """

    # "saveButton" is "save button": split camelCase names before matching.
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    lowered = " ".join(re.sub(r"[_\-]+", " ", spaced).casefold().split())
    allowed: frozenset[str] = frozenset()
    if sign_in:
        for phrase in _SIGN_IN_PHRASES:
            lowered = lowered.replace(phrase, " ")
        # A sign-in form's button often says OK or Submit; no record is open.
        allowed = frozenset({"ok", "submit"})
    return sorted(
        {
            w
            for w in re.findall(r"[a-z]+", lowered)
            if w in SAVE_LIKE_WORDS and w not in allowed
        }
    )


# -- configuration ------------------------------------------------------------


class ControlRef(BaseModel):
    """How to find one control in the read-back session.

    Accessibility path (UI Automation): ``automation_id``, or ``role`` with
    ``name``. Pixel path (OCR): ``text`` is the control's own visible text or
    the label next to it, and ``at`` says where the control (or the value to
    read) is relative to that text. ``region`` limits the OCR search to an
    ``(x, y, width, height)`` area; a ``region`` alone names an area, such as
    a results list.
    """

    model_config = ConfigDict(extra="forbid")

    automation_id: Optional[str] = None
    role: Optional[str] = None
    name: Optional[str] = None
    text: Optional[str] = None
    at: Literal["self", "right", "below"] = "self"
    region: Optional[tuple[int, int, int, int]] = None
    #: Used by markers (``ready``, ``not_found``, ``signed_in``): the control
    #: counts as present only when its text contains this.
    shows: Optional[str] = None
    #: Pixel path: how far past the label edge a click lands.
    offset_px: int = Field(default=24, ge=0, le=400)
    #: Pixel path: fuzzy-match floor for ``text``.
    min_ratio: float = Field(default=0.85, ge=0.5, le=1.0)

    @model_validator(mode="after")
    def _one_path(self) -> "ControlRef":
        structured = bool(self.automation_id) or bool(self.role and self.name)
        pixel = bool(self.text) or self.region is not None
        if structured == pixel:
            raise ValueError(
                "a control reference names either automation_id (or role and "
                "name) for UI Automation, or text (or region) for OCR, not both"
            )
        if self.region is not None and (self.region[2] <= 0 or self.region[3] <= 0):
            raise ValueError("a control region needs a positive width and height")
        return self

    @property
    def structured(self) -> bool:
        return bool(self.automation_id) or bool(self.role and self.name)

    def describe(self) -> str:
        if self.automation_id:
            return f"automation_id={self.automation_id!r}"
        if self.role and self.name:
            return f"{self.role} {self.name!r}"
        if self.text:
            return f"text {self.text!r}"
        return f"region {self.region!r}"


class DesktopFieldReader(BaseModel):
    """How to read one field from the record view."""

    model_config = ConfigDict(extra="forbid")

    control: ControlRef
    #: ``strptime`` format of a displayed date; normalized to ISO 8601.
    date_format: Optional[str] = None
    casefold: bool = False


class DesktopSignIn(BaseModel):
    """The application's own sign-in form in the read-back session."""

    model_config = ConfigDict(extra="forbid")

    username: ControlRef
    password: ControlRef
    submit: ControlRef
    #: Present only after a successful sign-in.
    signed_in: ControlRef


class SessionCredentials(BaseModel):
    """The read-back session's own credentials, as environment-variable names.

    For a network RDP session they are the RDP logon. With ``sign_in`` they
    are typed into the application's sign-in form.
    """

    model_config = ConfigDict(extra="forbid")

    username_env: str
    password_env: str


class SearchStep(BaseModel):
    """Find the record: type ``query`` into ``box`` and submit."""

    model_config = ConfigDict(extra="forbid")

    box: ControlRef
    #: The identifier or name to search for, usually a run parameter.
    query: ValueExpr
    #: The search button. None presses Enter in the search box.
    submit: Optional[ControlRef] = None


class OpenStep(BaseModel):
    """Open the record from a results list: the one row showing the identity."""

    model_config = ConfigDict(extra="forbid")

    #: The results list (UI Automation: the list or grid; OCR: its region).
    results: ControlRef
    double_click: bool = False


class DesktopIdentity(BaseModel):
    """The check that the record view shows the record that was searched for."""

    model_config = ConfigDict(extra="forbid")

    reader: ControlRef
    value: ValueExpr
    #: When set, the confirmed identifier is added to the record under this
    #: field name, so an effect can match on it.
    field: Optional[str] = None


class DesktopReadbackSpec(BaseModel):
    """Deployment config for ``effects.kind: desktop_readback``."""

    model_config = ConfigDict(extra="forbid")

    #: The read-back session's backend, in ``backend:`` syntax (``kind:
    #: windows | rdp | citrix | macos | linux``). It must not be the session
    #: or window the run types into; the deployment loader refuses one that is.
    session: dict[str, Any]
    credentials: Optional[SessionCredentials] = None
    sign_in: Optional[DesktopSignIn] = None
    search: SearchStep
    open: Optional[OpenStep] = None
    #: Present when the record view has loaded.
    ready: ControlRef
    #: Present when the application says no record matched the search.
    not_found: Optional[ControlRef] = None
    identity: DesktopIdentity
    fields: dict[str, DesktopFieldReader]
    truncation_markers: list[str] = Field(
        default_factory=lambda: list(DEFAULT_TRUNCATION_MARKERS)
    )
    #: OCR lines below this engine confidence are uncertain (never REFUTED).
    min_ocr_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    navigation_timeout_s: float = Field(default=15.0, gt=0)
    #: Pause after each input for the screen to update.
    settle_s: float = Field(default=0.3, ge=0.0, le=10.0)

    @model_validator(mode="before")
    @classmethod
    def _coerce_literals(cls, data: Any) -> Any:
        """A bare YAML string is a literal, as in every other effects config."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for section, key in (("search", "query"), ("identity", "value")):
            part = data.get(section)
            if isinstance(part, dict) and isinstance(part.get(key), str):
                part = dict(part)
                part[key] = {"literal": part[key]}
                data[section] = part
        return data

    @model_validator(mode="after")
    def _shape(self) -> "DesktopReadbackSpec":
        if not self.fields:
            raise ValueError("desktop_readback.fields must name at least one field")
        if self.identity.field is not None and self.identity.field in self.fields:
            raise ValueError(
                "desktop_readback.identity.field must not also be a record field"
            )
        if self.sign_in is not None and self.credentials is None:
            raise ValueError(
                "desktop_readback.sign_in needs desktop_readback.credentials"
            )
        if self.sign_in is not None and not (
            str(self.session.get("kind") or "").strip().lower() == "rdp"
            and self.session.get("rdp_host")
        ):
            # A window or a second agent persists between look-ups and stays
            # signed in, so its sign-in form is not a sign-in per check.
            raise ValueError(
                "desktop_readback.sign_in needs a network RDP session "
                "(session.kind rdp with rdp_host); other sessions persist and "
                "stay signed in"
            )
        kind = str(self.session.get("kind") or "").strip().lower()
        if kind not in ("windows", "rdp", "citrix", "macos", "linux"):
            raise ValueError(
                "desktop_readback.session.kind must be windows, rdp, citrix, macos "
                "or linux (a web record page uses effects.kind: ui_readback)"
            )
        markers = [("ready", self.ready), ("not_found", self.not_found)]
        if self.sign_in is not None:
            markers.append(("sign_in.signed_in", self.sign_in.signed_in))
        for name, marker in markers:
            # An area alone is "there" whether or not anything is in it.
            if marker is not None and not (
                marker.structured or marker.text or marker.shows
            ):
                raise ValueError(
                    f"desktop_readback.{name} needs text or shows, not only a region"
                )
        if not (self.identity.reader.structured or self.identity.reader.text):
            raise ValueError(
                "desktop_readback.identity.reader must name a control or a label, "
                "not only a region"
            )
        return self

    def refs(self) -> list[ControlRef]:
        refs = [self.search.box, self.ready, self.identity.reader]
        refs += [r.control for r in self.fields.values()]
        if self.search.submit is not None:
            refs.append(self.search.submit)
        if self.open is not None:
            refs.append(self.open.results)
        if self.not_found is not None:
            refs.append(self.not_found)
        if self.sign_in is not None:
            refs += [
                self.sign_in.username,
                self.sign_in.password,
                self.sign_in.submit,
                self.sign_in.signed_in,
            ]
        return refs

    def own_sign_in(self) -> bool:
        """Whether the session signs in with its own credentials."""

        if self.credentials is None:
            return False
        if self.sign_in is not None:
            return True
        kind = str(self.session.get("kind") or "").strip().lower()
        return kind == "rdp" and bool(self.session.get("rdp_host"))


@dataclass(frozen=True)
class ResolvedDesktopReadback:
    """A spec bound to one run: the concrete query and identifier."""

    spec: DesktopReadbackSpec
    query: str
    identity_value: str


def resolve_desktop_spec(
    spec: DesktopReadbackSpec, params: Optional[Mapping[str, object]] = None
) -> ResolvedDesktopReadback:
    """Bind ``spec`` to the run parameters, failing loud on a missing one."""

    values = dict(params or {})
    query = spec.search.query.resolve(values)
    if query is None:
        raise ValueError(
            "desktop_readback.search.query references run parameter "
            f"{spec.search.query.param!r}, which was not supplied"
        )
    if not query.strip() or any(ord(c) < 32 for c in query):
        raise ValueError(
            "desktop_readback.search.query must be one line of visible text"
        )
    identity_value = spec.identity.value.resolve(values)
    if identity_value is None:
        raise ValueError(
            "desktop_readback.identity.value references run parameter "
            f"{spec.identity.value.param!r}, which was not supplied"
        )
    return ResolvedDesktopReadback(
        spec=spec, query=query, identity_value=identity_value
    )


# -- what a session offers ------------------------------------------------------


@dataclass(frozen=True)
class Seen:
    """One control or line of text as the session sees it."""

    text: str
    point: tuple[int, int]
    #: OCR engine confidence; 1.0 for a UI Automation read.
    confidence: float = 1.0
    #: Read from screen pixels by OCR (a misread is possible) rather than
    #: from UI Automation.
    ocr: bool = False
    #: For a result row: its pieces (OCR lines or grid cells) as
    #: ``(text, point)``, so the look-up can click the piece that shows the
    #: identifier instead of the middle of the row.
    parts: tuple[tuple[str, tuple[int, int]], ...] = ()


@dataclass(frozen=True)
class Located:
    """The result of finding or reading one control."""

    status: Literal["ok", "missing", "ambiguous", "unavailable"]
    item: Optional[Seen] = None


@dataclass(frozen=True)
class DesktopSessionFacts:
    """What the driver can attest about the read-back session itself."""

    #: Opened (or declared) for read-back; never the session that typed.
    own_session: bool
    #: Newly connected or launched for this read.
    fresh_session: bool
    #: Signed in with the read-back credentials (RDP logon) in this session.
    signed_in: bool
    #: How fields are read: "uia", "ocr", or "uia+ocr".
    path: str


class DesktopReadSurface(Protocol):
    """An open read-back session. Every input goes through the guard first."""

    facts: DesktopSessionFacts

    def locate(self, ref: ControlRef) -> Located:
        """The control itself (to click or type into)."""
        ...

    def read(self, ref: ControlRef) -> Located:
        """The text of the control, or of the value next to its label.

        ``ok`` with empty text means the control (or label) is there and shows
        nothing; ``missing`` means the control or label is not there.
        """
        ...

    def rows(self, ref: ControlRef) -> Optional[list[Seen]]:
        """Rows of a results list; None when the list is not readable."""
        ...

    def type_into(self, target: Seen, text: str) -> None:
        """Focus ``target`` and type ``text``."""
        ...

    def select_all_in(self, target: Seen) -> None:
        """Focus ``target`` and select its text (so typing replaces it)."""
        ...

    def text_at(self, point: tuple[int, int]) -> Optional[str]:
        """The text a person sees at ``point`` (for the save-word check)."""
        ...

    def click(self, target: Seen, *, double: bool = False) -> None: ...

    def press_enter(self) -> None: ...

    def settle(self) -> None:
        """Wait for the screen to reflect the last input."""
        ...


class DesktopReadbackDriver(Protocol):
    """Opens a read-back session."""

    def open(
        self, target: ResolvedDesktopReadback, username: str, password: str
    ) -> AbstractContextManager[DesktopReadSurface]: ...


class DesktopReadbackSessionError(RuntimeError):
    """The session could not be opened. Never carries credentials or values."""


class ReadOnlyViolation(RuntimeError):
    """The look-up was about to do something a read-only check must not do."""


# -- the guard ------------------------------------------------------------------


class LookupCancelled(RuntimeError):
    """The verifier gave up on the session; stop sending input."""


class ReadOnlyLookupGuard:
    """Every input the look-up sends passes here first.

    It allows typing only into the search box (and the sign-in fields while
    signing in), Enter only in the search box, and clicks only on the search
    button, the sign-in button, and a result row. Before any click or typing
    it reads what is under the point; a save-like word there, in the
    control's label, or in its name or id refuses the input.
    """

    def __init__(
        self,
        spec: DesktopReadbackSpec,
        *,
        cancel: Optional[threading.Event] = None,
    ) -> None:
        self._spec = spec
        self._cancel = cancel
        self.phase: Literal["sign_in", "look_up"] = "look_up"
        #: Inputs sent, for the evidence (control descriptions, no values).
        self.inputs: list[str] = []

    def check_cancelled(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise LookupCancelled("the read-back session ran out of time")

    def _typing_allowed(self, ref: ControlRef) -> bool:
        if self.phase == "sign_in" and self._spec.sign_in is not None:
            return (
                ref is self._spec.sign_in.username or ref is self._spec.sign_in.password
            )
        return self.phase == "look_up" and ref is self._spec.search.box

    @staticmethod
    def _labels(
        surface: DesktopReadSurface, ref: ControlRef, target: Seen
    ) -> list[str]:
        under = surface.text_at(target.point) or ""
        return [
            label
            for label in (target.text, under, ref.name or "", ref.automation_id or "")
            if label
        ]

    def _refuse_save_like(
        self,
        surface: DesktopReadSurface,
        ref: ControlRef,
        target: Seen,
        *,
        sign_in: bool = False,
        data_row: bool = False,
    ) -> None:
        # A result row shows record data ("Complete", "Post-op", a surname
        # like Post), not a command, so only its declared name and id are
        # checked.
        if data_row:
            under = surface.text_at(target.point) or ""
            labels = [
                label
                for label in (ref.name or "", ref.automation_id or "", under)
                if label
            ]
        else:
            labels = self._labels(surface, ref, target)
        for label in labels:
            words = save_like_words(label, sign_in=sign_in)
            if words:
                raise ReadOnlyViolation(
                    f"refused to touch {ref.describe()}: its label has save-like "
                    f"word(s) {', '.join(repr(w) for w in words)}"
                )

    def type_into(
        self, surface: DesktopReadSurface, ref: ControlRef, target: Seen, text: str
    ) -> None:
        self.check_cancelled()
        if not self._typing_allowed(ref):
            raise ReadOnlyViolation(
                f"refused to type into {ref.describe()}: the read-back may type only "
                "into the search box"
            )
        if any(ord(c) < 32 for c in text):
            raise ReadOnlyViolation("refused to type a control character")
        self._refuse_save_like(surface, ref, target, sign_in=self.phase == "sign_in")
        surface.type_into(target, text)
        self.inputs.append(f"type:{ref.describe()}")

    def select_all_in(
        self, surface: DesktopReadSurface, ref: ControlRef, target: Seen
    ) -> None:
        self.check_cancelled()
        if not (self.phase == "look_up" and ref is self._spec.search.box):
            raise ReadOnlyViolation(
                f"refused to select text in {ref.describe()}: only the search box"
            )
        self._refuse_save_like(surface, ref, target)
        surface.select_all_in(target)
        self.inputs.append(f"select_all:{ref.describe()}")

    def press_enter(self, surface: DesktopReadSurface, ref: ControlRef) -> None:
        self.check_cancelled()
        if not (
            self.phase == "look_up"
            and ref is self._spec.search.box
            and self._spec.search.submit is None
        ):
            raise ReadOnlyViolation(
                "refused to press Enter: only in the search box, and only when no "
                "search button is declared"
            )
        surface.press_enter()
        self.inputs.append(f"enter:{ref.describe()}")

    def click(
        self,
        surface: DesktopReadSurface,
        ref: ControlRef,
        target: Seen,
        *,
        double: bool = False,
    ) -> None:
        self.check_cancelled()
        spec = self._spec
        sign_in_click = (
            self.phase == "sign_in"
            and spec.sign_in is not None
            and ref is spec.sign_in.submit
        )
        allowed = sign_in_click or (
            self.phase == "look_up"
            and (
                ref is spec.search.submit
                or (spec.open is not None and ref is spec.open.results)
            )
        )
        if not allowed:
            raise ReadOnlyViolation(
                f"refused to click {ref.describe()}: the read-back clicks only the "
                "search button, the sign-in button, and one result"
            )
        if not self._labels(surface, ref, target):
            raise ReadOnlyViolation(
                f"refused to click {ref.describe()}: it shows no label to check"
            )
        self._refuse_save_like(
            surface,
            ref,
            target,
            sign_in=sign_in_click,
            data_row=spec.open is not None and ref is spec.open.results,
        )
        surface.click(target, double=double)
        self.inputs.append(f"click:{ref.describe()}")


# -- the look-up ------------------------------------------------------------------


@dataclass
class RawRecordView:
    """One look-up, before normalization."""

    ok: bool
    reason: str = ""
    not_found: bool = False
    identity: Optional[Seen] = None
    values: dict[str, Seen] = field(default_factory=dict)
    inputs: list[str] = field(default_factory=list)
    #: The guard refused an input (or the session ran out of time): stop,
    #: don't retry.
    refused: bool = False


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


#: What may sit right before or after an identifier that is shown on its own.
#: ``-``, ``.`` and ``/`` are not here: ``123`` must not match ``123-01``.
_BEFORE = r"(?:^|(?<=[\s(\[:,;\"']))"
_AFTER = r"(?=$|[\s)\]:,;\"'])"


def _shows_identity(text: str, identity: str) -> bool:
    """Whether ``text`` shows ``identity`` on its own: separated by spaces,
    brackets, or a colon, never as part of a longer identifier."""

    want = _norm(identity)
    if not want:
        return False
    return re.search(_BEFORE + re.escape(want) + _AFTER, _norm(text)) is not None


def _identity_piece(row: Seen, identity: str) -> Seen:
    """The row, aimed at the piece that shows the identifier (or a word of
    it), so the click lands on data the look-up checked, not on a button
    that shares the row."""

    words = set(_norm(identity).split())
    for wanted in (
        lambda text: _shows_identity(text, identity),
        lambda text: bool(_norm(text)) and set(_norm(text).split()) <= words,
    ):
        for text, point in row.parts:
            if wanted(text):
                return Seen(
                    text=row.text, point=point, confidence=row.confidence, ocr=row.ocr
                )
    return row


def _present(surface: DesktopReadSurface, ref: ControlRef) -> bool:
    """Whether a marker is on screen, shows some text, and (when set) shows
    ``ref.shows``."""

    seen = surface.read(ref)
    if ref.structured and ref.shows is None and seen.status == "unavailable":
        # A UI Automation control that exposes no text at all (a panel):
        # present when it is there. A control that reads as empty is absent.
        return surface.locate(ref).status == "ok"
    if seen.status != "ok" or seen.item is None or not seen.item.text.strip():
        return False
    if ref.shows is None:
        return True
    from openadapt_flow.runtime.effects.readback_values import token_occurrences

    # Whole tokens: a marker "Saved" is not shown by "Unsaved".
    return token_occurrences(_norm(seen.item.text), _norm(ref.shows)) >= 1


def _view(surface: DesktopReadSurface, spec: DesktopReadbackSpec) -> tuple[Any, ...]:
    """What the record view shows: the identifier and every field."""

    refs = [spec.identity.reader, *(r.control for r in spec.fields.values())]
    out = []
    for ref in refs:
        seen = surface.read(ref)
        out.append((seen.status, seen.item.text if seen.item else None))
    return tuple(out)


def _wait_for(
    surface: DesktopReadSurface,
    guard: "ReadOnlyLookupGuard",
    check: Callable[[], Optional[Any]],
    timeout_s: float,
) -> Optional[Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        guard.check_cancelled()
        result = check()
        if result is not None or time.monotonic() >= deadline:
            return result
        surface.settle()


def look_up(
    surface: DesktopReadSurface,
    target: ResolvedDesktopReadback,
    guard: ReadOnlyLookupGuard,
    *,
    username: str = "",
    password: str = "",
) -> RawRecordView:
    """Find, open, and read the record once, the way a person would."""

    spec = target.spec
    timeout = spec.navigation_timeout_s

    def fail(reason: str) -> RawRecordView:
        return RawRecordView(ok=False, reason=reason, inputs=list(guard.inputs))

    # -- sign in (the application's own form) --------------------------------
    if spec.sign_in is not None:
        sign_in = spec.sign_in
        if _present(surface, sign_in.signed_in):
            # Someone is already signed in here. The check must sign in with
            # its own account, or it is not a separately signed-in session.
            return fail(
                "the read-back session was already signed in; it must sign in "
                "with its own account"
            )
        guard.phase = "sign_in"
        for ref, secret in (
            (sign_in.username, username),
            (sign_in.password, password),
        ):
            found = surface.locate(ref)
            if found.status != "ok" or found.item is None:
                return fail(f"the sign-in form has no unique {ref.describe()}")
            guard.type_into(surface, ref, found.item, secret)
        button = surface.locate(sign_in.submit)
        if button.status != "ok" or button.item is None:
            return fail("the sign-in form has no unique sign-in button")
        guard.click(surface, sign_in.submit, button.item)
        surface.settle()
        signed = _wait_for(
            surface,
            guard,
            lambda: True if _present(surface, sign_in.signed_in) else None,
            timeout,
        )
        guard.phase = "look_up"
        if not signed:
            return fail("the sign-in did not complete")

    # -- search ----------------------------------------------------------------
    box_ref = spec.search.box
    box = surface.locate(box_ref)
    if box.status != "ok" or box.item is None:
        return fail(f"the search box ({box_ref.describe()}) is {box.status}")
    current = surface.read(box_ref)
    shown = current.item.text if current.status == "ok" and current.item else ""
    if _norm(shown) != _norm(target.query):
        if shown.strip():
            guard.select_all_in(surface, box_ref, box.item)
        guard.type_into(surface, box_ref, box.item, target.query)
        surface.settle()
        typed = surface.read(box_ref)
        if (
            typed.status != "ok"
            or typed.item is None
            or _norm(typed.item.text) != _norm(target.query)
        ):
            return fail(
                "the search box does not show the query after typing; the typed "
                "text may have gone elsewhere"
            )
    # What a session that persists between look-ups may still show from an
    # earlier one. A view (or a not-found message) that was up before the
    # search counts only once it changes or goes away and comes back.
    stale_view = _view(surface, spec) if _present(surface, spec.ready) else None
    stale_not_found = spec.not_found is not None and _present(surface, spec.not_found)
    # A marker counts as gone only after two polls in a row without it, so
    # one OCR miss or a brief overlay can't pass a stale screen.
    seen_gone = {"view": stale_view is None, "not_found": not stale_not_found}
    absent = {"view": 0, "not_found": 0}
    if spec.search.submit is None:
        guard.press_enter(surface, box_ref)
    else:
        button = surface.locate(spec.search.submit)
        if button.status != "ok" or button.item is None:
            return fail(f"the search button is {button.status}")
        guard.click(surface, spec.search.submit, button.item)
    surface.settle()

    def gone(name: str, shown: bool) -> None:
        absent[name] = 0 if shown else absent[name] + 1
        if absent[name] >= 2:
            seen_gone[name] = True

    def not_found_shown() -> bool:
        shown = spec.not_found is not None and _present(surface, spec.not_found)
        gone("not_found", shown)
        return shown and seen_gone["not_found"]

    def fresh_view() -> bool:
        shown = _present(surface, spec.ready)
        gone("view", shown)
        if not shown:
            return False
        return seen_gone["view"] or _view(surface, spec) != stale_view

    # -- open the one matching result -------------------------------------------
    if spec.open is not None:
        results_ref = spec.open.results

        def results_or_absent() -> Optional[Any]:
            if not_found_shown():
                return "not_found"
            rows = surface.rows(results_ref)
            if rows is None:
                return None
            matching = [
                r for r in rows if _shows_identity(r.text, target.identity_value)
            ]
            return matching or None

        outcome = _wait_for(surface, guard, results_or_absent, timeout)
        if outcome == "not_found":
            return RawRecordView(ok=True, not_found=True, inputs=list(guard.inputs))
        if outcome is None:
            return fail(
                "no search result shows the record identifier, and the application "
                "shows no not-found marker"
            )
        if len(outcome) != 1:
            return fail(
                f"{len(outcome)} search results show the record identifier; the "
                "read-back does not pick one"
            )
        row = _identity_piece(outcome[0], target.identity_value)
        guard.click(surface, results_ref, row, double=spec.open.double_click)
        surface.settle()

    # -- the record view -----------------------------------------------------------
    def ready_or_absent() -> Optional[str]:
        if fresh_view():
            return "ready"
        if not_found_shown():
            return "not_found"
        return None

    state = _wait_for(surface, guard, ready_or_absent, timeout)
    if state == "not_found":
        return RawRecordView(ok=True, not_found=True, inputs=list(guard.inputs))
    if state is None:
        if stale_view is not None and _present(surface, spec.ready):
            return fail(
                "the record view still shows what it showed before the search; "
                "it may not have refreshed, so it can't be read as this save"
            )
        return fail("the record view did not load")
    identity = surface.read(spec.identity.reader)
    if identity.status != "ok" or identity.item is None:
        return fail(f"the record view's identifier is {identity.status}")
    values: dict[str, Seen] = {}
    for name, reader in spec.fields.items():
        value = surface.read(reader.control)
        if value.status != "ok" or value.item is None:
            return fail(f"field {name!r} on the record view is {value.status}")
        values[name] = value.item
    return RawRecordView(
        ok=True, identity=identity.item, values=values, inputs=list(guard.inputs)
    )


# -- the verifier -------------------------------------------------------------------


@dataclass
class _ReadRecord:
    readable: bool
    reason: str
    records: list[dict[str, Any]]
    uncertain: set[tuple[int, str]]
    not_found: bool
    #: Fields read by OCR. A mismatch there may be a misread, so it never
    #: becomes REFUTED.
    ocr_fields: set[str] = field(default_factory=set)


def _without_spaces_for(effect: Effect, fields: set[str]) -> Effect:
    """``effect`` with spaces removed from the expected values of ``fields``.

    Fields read by OCR are compared without spaces on both sides, because OCR
    drops and adds them (``2026-11-02 09:30`` reads as ``2026-11-0209:30``).
    """

    if not fields:
        return effect

    def squash(name: str, expr: Optional[ValueExpr]) -> Optional[ValueExpr]:
        if expr is None or expr.literal is None or name not in fields:
            return expr
        return ValueExpr(literal="".join(expr.literal.split()))

    match = {k: squash(k, v) or v for k, v in effect.match.items()}
    value = squash(effect.field, effect.value) if effect.field else effect.value
    return effect.model_copy(update={"match": match, "value": value}, deep=True)


class DesktopReadbackVerifier(VerifierAdapterBase):
    """Check a saved record by looking it up in a separate desktop, RDP, or
    Citrix session, as a person would.

    Args:
        target: The spec bound to this run (:func:`resolve_desktop_spec`).
        driver: Opens sessions (built from ``spec.session`` by the deployment).
        poll_interval_s: Gap between look-ups while waiting for a save to
            appear, within the effect's ``timeout_s``.
        session_timeout_s: Hard cap on one session. A session that runs past
            it is abandoned and the verdict is INDETERMINATE.
        environ: Where credentials are read from (``os.environ`` by default).
    """

    substrate = SUBSTRATE
    #: The read goes through the same application's screens.
    independent_system_of_record = False
    adapter_version = "1"

    def __init__(
        self,
        target: ResolvedDesktopReadback,
        *,
        driver: DesktopReadbackDriver,
        poll_interval_s: float = 0.5,
        session_timeout_s: Optional[float] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._target = target
        self._driver = driver
        self.poll_interval_s = poll_interval_s
        self._session_timeout_s = session_timeout_s
        self._environ = environ if environ is not None else os.environ
        self._worker: Optional[threading.Thread] = None
        spec = target.spec
        self._own_sign_in = spec.own_sign_in()
        #: Tier 2 needs the session's own sign-in; a separate window as the
        #: same user re-reads saved state (tier 3).
        self.verification_tier = (
            VerificationTier.INDEPENDENT_SESSION
            if self._own_sign_in
            else VerificationTier.PERSISTED_STATE_REACQUISITION
        )
        if spec.credentials is not None:
            for name in (spec.credentials.username_env, spec.credentials.password_env):
                if not self._environ.get(name):
                    raise ValueError(
                        f"desktop_readback.credentials references environment "
                        f"variable {name!r}, which is not set (or empty); refusing "
                        "to wire a read-back session that cannot sign in"
                    )

    # -- session plumbing ----------------------------------------------------------
    def _credentials(self) -> tuple[str, str]:
        creds = self._target.spec.credentials
        if creds is None:
            return "", ""
        return (
            self._environ.get(creds.username_env, ""),
            self._environ.get(creds.password_env, ""),
        )

    def _session_budget(self, effect_timeout_s: float = 0.0) -> float:
        if self._session_timeout_s is not None:
            return self._session_timeout_s + effect_timeout_s
        return 6 * self._target.spec.navigation_timeout_s + effect_timeout_s

    def _in_session(
        self,
        work: Callable[[DesktopReadSurface, str, str, threading.Event], Any],
        *,
        budget_s: float,
    ) -> tuple[Optional[Any], str, Optional[DesktopSessionFacts]]:
        """Run ``work`` in a new session on a worker thread.

        A session that runs past ``budget_s`` is told to stop (no more input)
        and is abandoned; no new session opens while it is still running.
        """

        previous = self._worker
        if previous is not None and previous.is_alive():
            return (
                None,
                "an earlier read-back session is still running; not opening another",
                None,
            )
        outcome: dict[str, Any] = {}
        cancel = threading.Event()

        def run() -> None:
            username, password = self._credentials()
            try:
                with self._driver.open(self._target, username, password) as surface:
                    facts = surface.facts
                    outcome["facts"] = facts
                    if not facts.own_session:
                        outcome["error"] = (
                            "the read-back session is not its own session; it must "
                            "never be the session or window that typed"
                        )
                        return
                    if (
                        self._own_sign_in
                        and self._target.spec.sign_in is None
                        and not facts.signed_in
                    ):
                        outcome["error"] = (
                            "the read-back session did not sign in with its own "
                            "credentials"
                        )
                        return
                    outcome["result"] = work(surface, username, password, cancel)
            except DesktopReadbackSessionError as exc:
                outcome["error"] = str(exc)
            except Exception as exc:  # noqa: BLE001 - never raise out of a verdict
                outcome["error"] = f"read-back session failed ({type(exc).__name__})"

        worker = threading.Thread(target=run, name="desktop-readback", daemon=True)
        self._worker = worker
        worker.start()
        worker.join(timeout=budget_s)
        if worker.is_alive():
            cancel.set()
            return None, "the read-back session did not finish in time", None
        if "error" in outcome:
            return None, str(outcome["error"]), outcome.get("facts")
        return outcome.get("result"), "", outcome.get("facts")

    def _look_up_once(
        self,
        surface: DesktopReadSurface,
        username: str,
        password: str,
        cancel: Optional[threading.Event] = None,
    ) -> RawRecordView:
        guard = ReadOnlyLookupGuard(self._target.spec, cancel=cancel)
        try:
            return look_up(
                surface, self._target, guard, username=username, password=password
            )
        except ReadOnlyViolation as exc:
            return RawRecordView(
                ok=False, reason=str(exc), inputs=list(guard.inputs), refused=True
            )
        except LookupCancelled as exc:
            return RawRecordView(
                ok=False, reason=str(exc), inputs=list(guard.inputs), refused=True
            )

    # -- normalization ------------------------------------------------------------------
    def _normalize(self, raw: RawRecordView) -> _ReadRecord:
        spec = self._target.spec
        if not raw.ok:
            return _ReadRecord(
                False, raw.reason or "the record was not readable", [], set(), False
            )
        if raw.not_found:
            return _ReadRecord(True, "", [], set(), True)
        assert raw.identity is not None
        if raw.identity.confidence < spec.min_ocr_confidence:
            return _ReadRecord(
                False,
                "the record view's identifier was read below the OCR confidence floor",
                [],
                set(),
                False,
            )
        if not _shows_identity(raw.identity.text, self._target.identity_value):
            return _ReadRecord(
                False,
                "the record view shows a different record identifier than the one "
                "searched for; nothing on it can be attributed to this record",
                [],
                set(),
                False,
            )
        record: dict[str, Any] = {}
        uncertain: set[tuple[int, str]] = set()
        for name, reader in spec.fields.items():
            seen = raw.values.get(name)
            if seen is None:
                return _ReadRecord(
                    False, f"field {name!r} was not read", [], set(), False
                )
            text = seen.text
            stripped = text.rstrip()
            if seen.confidence < spec.min_ocr_confidence or any(
                m and stripped.endswith(m) for m in spec.truncation_markers
            ):
                uncertain.add((0, name))
            normalized, parsed = normalize_value(text, reader)  # type: ignore[arg-type]
            if seen.ocr and normalized is not None:
                # OCR drops and adds spaces; compare OCR reads without them.
                normalized = "".join(normalized.split())
            if not parsed:
                uncertain.add((0, name))
            record[name] = normalized
        if spec.identity.field is not None:
            record[spec.identity.field] = self._target.identity_value
        ocr_fields = {name for name, seen in raw.values.items() if seen.ocr}
        return _ReadRecord(True, "", [record], uncertain, False, ocr_fields)

    def _effect_problem(self, effect: Effect) -> Optional[EffectVerdict]:
        """An effect this look-up can't judge, found before any session opens."""

        spec = self._target.spec
        readable_fields = set(spec.fields)
        if spec.identity.field is not None:
            readable_fields.add(spec.identity.field)
        named = set(effect.match)
        if effect.kind is EffectKind.FIELD_EQUALS and effect.field:
            named.add(effect.field)
        if effect.idempotency_key is not None:
            named.add(effect.key_field)
        for member in effect.new_records:
            named.update(member)
        unknown = sorted(named - readable_fields)
        if unknown:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=effect.kind,
                substrate=self.substrate,
                reason=(
                    "the effect names field(s) the look-up does not read ("
                    + ", ".join(repr(n) for n in unknown)
                    + "); declare them under desktop_readback.fields; HALT"
                ),
            )
        counts = (
            effect.kind in (EffectKind.RECORD_WRITTEN, EffectKind.EXACT_NEW_SET)
            or effect.count_new_only
            or effect.idempotency_key is not None
        )
        if effect.kind is EffectKind.EXACT_NEW_SET or (
            effect.kind is EffectKind.RECORD_WRITTEN and effect.expected_count > 1
        ):
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=effect.kind,
                substrate=self.substrate,
                reason=(
                    "the effect expects more than one record, and a look-up opens "
                    "one record view; check it with a system-of-record read; HALT"
                ),
            )
        if counts and spec.open is None:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=effect.kind,
                substrate=self.substrate,
                reason=(
                    "the effect counts records, but the look-up opens one record "
                    "view and can't see a duplicate. Declare open.results so the "
                    "search results show exactly one match, or check a field "
                    "(field_equals); HALT"
                ),
            )
        return None

    def _judge(
        self, effect: Effect, before: EffectState, read: _ReadRecord
    ) -> EffectVerdict:
        if not read.readable:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=effect.kind,
                substrate=self.substrate,
                reason=f"{read.reason}; cannot certify the save; HALT",
                unavailable=True,
            )
        problem = self._effect_problem(effect)
        if problem is not None:
            return problem
        spec = self._target.spec
        normalized = _without_spaces_for(
            normalized_effect(effect, spec.fields), read.ocr_fields
        )
        blocking = blocking_uncertain_fields(normalized, read.records, read.uncertain)
        if blocking:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=effect.kind,
                substrate=self.substrate,
                reason=(
                    "the record view shows a shortened, unparseable, or low-confidence "
                    f"value for {', '.join(repr(n) for n in blocking)}; a value read "
                    "that way can neither confirm nor contradict the input; HALT"
                ),
            )
        verdict = judge_records(
            normalized, before, read.records, substrate=self.substrate
        )
        if verdict.confirmed and not read.records:
            # A search that finds nothing never proves anything, so it never
            # confirms (for example an effect that expects no record).
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=effect.kind,
                substrate=self.substrate,
                reason=(
                    "the look-up found no record; a search that finds nothing is "
                    "not proof of what was saved; HALT"
                ),
            )
        if verdict.verdict is Verdict.REFUTED and read.ocr_fields and read.records:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=effect.kind,
                substrate=self.substrate,
                reason=(
                    "the record view, read by OCR, does not match the input "
                    f"({', '.join(sorted(read.ocr_fields))}); OCR can misread, so "
                    "this is not proof the save is wrong. Check the record; HALT"
                ),
            )
        return without_absence_claim(
            relabel_screen_read(
                verdict, "the record view (separate session)", "record view"
            )
        )

    def _detail(
        self, facts: Optional[DesktopSessionFacts], raw: Optional[RawRecordView]
    ) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "read": "separate desktop session: search, open, read",
            "session_kind": str(self._target.spec.session.get("kind")),
            "own_sign_in": self._own_sign_in,
        }
        if facts is not None:
            detail.update(
                own_session=facts.own_session,
                fresh_session=facts.fresh_session,
                signed_in=facts.signed_in,
                read_path=facts.path,
            )
        if raw is not None:
            detail.update(inputs=list(raw.inputs), record_not_found=raw.not_found)
        return detail

    # -- adapter lifecycle -----------------------------------------------------------
    def _read_once(
        self,
    ) -> tuple[Optional[RawRecordView], str, Optional[DesktopSessionFacts]]:
        return self._in_session(self._look_up_once, budget_s=self._session_budget())

    def capture_pre_state(self, context: Any = None) -> EffectState:
        raw, failure, facts = self._read_once()
        read = self._normalize(raw) if raw is not None else None
        if read is None or not read.readable:
            return EffectState(
                substrate=self.substrate,
                reachable=False,
                records=[],
                detail={
                    **self._detail(facts, raw),
                    "reason": failure or (read.reason if read else ""),
                },
            )
        return EffectState(
            substrate=self.substrate,
            reachable=True,
            records=read.records,
            detail=self._detail(facts, raw),
        )

    def test_connection(self, context: Any = None) -> ConnectionProbe:
        """Open the session, sign in, and look the record up once."""

        raw, failure, _facts = self._read_once()
        if raw is None:
            return ConnectionProbe(ok=False, substrate=self.substrate, reason=failure)
        read = self._normalize(raw)
        if not read.readable:
            return ConnectionProbe(
                ok=False, substrate=self.substrate, reason=read.reason
            )
        return ConnectionProbe(
            ok=True,
            substrate=self.substrate,
            reason="record look-up readable in a separate session",
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        """Look the record up until the effect is CONFIRMED or
        ``expected.timeout_s`` passes. Each look-up opens its own session, so
        no look-up reads a screen an earlier one left behind."""

        problem = self._effect_problem(expected)
        if problem is not None:
            return problem.model_copy(update={"assurance": UNCHECKED})
        deadline = time.monotonic() + max(0.0, expected.timeout_s)
        readable: Optional[EffectVerdict] = None
        while True:
            raw, failure, _facts = self._read_once()
            if raw is None:
                verdict = EffectVerdict(
                    verdict=Verdict.INDETERMINATE,
                    kind=expected.kind,
                    substrate=self.substrate,
                    reason=f"{failure}; cannot certify the save; HALT",
                    unavailable=True,
                )
            else:
                verdict = self._judge(expected, before, self._normalize(raw))
            if not verdict.unavailable:
                readable = verdict
            elif readable is not None:
                # A later look-up that couldn't read (a screen that didn't
                # refresh, say) never replaces what an earlier one read.
                verdict = readable
                break
            if (
                verdict.confirmed
                or raw is None
                or raw.refused
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(self.poll_interval_s)
        return verdict.model_copy(
            update={"assurance": SCREEN_READBACK if verdict.confirmed else UNCHECKED}
        )


__all__ = [
    "ControlRef",
    "DesktopFieldReader",
    "DesktopIdentity",
    "DesktopReadSurface",
    "DesktopReadbackDriver",
    "DesktopReadbackSessionError",
    "DesktopReadbackSpec",
    "DesktopReadbackVerifier",
    "DesktopSessionFacts",
    "DesktopSignIn",
    "Located",
    "LookupCancelled",
    "OpenStep",
    "RawRecordView",
    "ReadOnlyLookupGuard",
    "ReadOnlyViolation",
    "ResolvedDesktopReadback",
    "SAVE_LIKE_WORDS",
    "SUBSTRATE",
    "SearchStep",
    "Seen",
    "SessionCredentials",
    "look_up",
    "resolve_desktop_spec",
    "save_like_words",
]
