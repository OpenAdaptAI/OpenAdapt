"""What makes a UI read-back count, and the checks it adds beyond the record page.

The separate-session read-back (:mod:`.ui_readback`) is the record check for
an application with no API, database or FHIR channel. Three rules and two
optional checks make it evidence rather than a second look at the same screen.

Independence (:func:`assess_independence`). A read counts toward
``VERIFIED`` only when all three hold, and the receipt records each one:

- ``separate_session``: its own browser, a new empty context (no cookies,
  storage, cache or service worker), signed in with its own credentials;
- ``requery``: the record view came from a fresh request to the server, not a
  cached or restored page. A brand-new context has no cache to serve from, and
  the driver also reports whether the document response came over the network;
- ``distinct_view``: the view is not the page the save left on the acting
  session's screen. The URL the acting session showed right after the save is
  compared with the URL the read opened (scheme, host, path and query, minus
  the fragment). The same URL is the same view, so the read never counts, even
  re-fetched: the page the save renders can echo what was posted.

A read that fails any rule can't confirm a save: a CONFIRMED becomes
INDETERMINATE (the run halts for a person). A contradiction still refutes. A
rule that can't be established from the evidence is recorded as ``unknown``;
``distinct_view`` is unknown only when no acting session is bound (the
verifier used outside a run) and then doesn't block.

History (:class:`HistoryView`). Where the application has an audit, history
or activity screen, the separate session reads it before and after the save.
Every new entry must be about this record and must match one of the entries
the demonstration showed for the save (``explained``). A new entry about
another record, or one nobody declared, is a collateral change: REFUTED. A
history that shows no entry for the save is INDETERMINATE. Entries by other
users are excused when the view shows the user and ``acting_user_env`` names
the run's account.

Provisional, then final (:class:`FinalCheck`). A portal that shows only a
confirmation number and a "pending review" queue, or any application whose
record can change after the save, gets a provisional check now and a final
one later. The provisional read confirms the record and its values and writes
a waiting entry to the deferred-check ledger
(:mod:`.deferred_report`): the record's locator and a keyed digest of the
values read (never the values). The run ends ``SAVED_AWAITING_CHECK``.
``openadapt-flow deferred settle --config deployment.yaml`` later opens one
fresh session, re-reads every due record in it (one sign-in for the whole
batch) and settles each entry: a final status with unchanged values is
VERIFIED; a rejected status, a changed value, a missing record or a deadline
passed is RECONCILIATION_REQUIRED; a status still pending waits.

No model calls. Nothing here guesses CONFIRMED.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "FinalCheck",
    "HistoryEntryReader",
    "HistoryView",
    "ReadIndependence",
    "assess_independence",
    "judge_history",
    "record_digest_payload",
    "same_view",
    "view_key",
]


# -- independence --------------------------------------------------------------


def view_key(url: Optional[str]) -> Optional[str]:
    """Scheme, host, path and query of ``url``; None when it isn't http(s).

    The fragment is dropped (it never reaches the server). The query is kept
    in order: two URLs that differ in a parameter are different views.
    """

    if not isinstance(url, str) or not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{parts.netloc}".lower() + path + query


def same_view(a: Optional[str], b: Optional[str]) -> Optional[bool]:
    """True when both URLs name the same view; None when either is unknown."""

    ka, kb = view_key(a), view_key(b)
    if ka is None or kb is None:
        return None
    return ka == kb


Tri = Literal["yes", "no", "unknown"]


@dataclass(frozen=True)
class ReadIndependence:
    """The receipt's account of why a UI read is (or isn't) independent."""

    separate_session: Tri
    requery: Tri
    distinct_view: Tri
    #: How each rule was established, in plain words (no record values).
    basis: dict[str, str] = field(default_factory=dict)
    #: Seconds the separate session spent (launch, sign-in, reads).
    read_seconds: Optional[float] = None

    @property
    def qualifies(self) -> bool:
        """All three rules hold; ``distinct_view`` may be unknown (no acting
        session bound), never ``no``."""

        return (
            self.separate_session == "yes"
            and self.requery == "yes"
            and self.distinct_view != "no"
        )

    @property
    def failed(self) -> list[str]:
        out = []
        if self.separate_session != "yes":
            out.append("separate_session")
        if self.requery != "yes":
            out.append("requery")
        if self.distinct_view == "no":
            out.append("distinct_view")
        return out

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "separate_session": self.separate_session,
            "requery": self.requery,
            "distinct_view": self.distinct_view,
            "qualifies": self.qualifies,
            "basis": dict(self.basis),
        }
        if self.read_seconds is not None:
            data["read_seconds"] = round(self.read_seconds, 3)
        return data


def assess_independence(
    *,
    own_browser: bool,
    fresh_context: bool,
    signed_in: bool,
    fetched: Optional[bool],
    read_url: Optional[str],
    acting_url: Optional[str],
    acting_bound: bool,
    acting_has_url: bool,
    read_seconds: Optional[float] = None,
) -> ReadIndependence:
    """Apply the three rules to what the driver and the acting session report.

    ``fetched`` is the driver's report that the record document came over
    the network (None when the driver doesn't say). ``acting_url`` is the URL
    the acting session showed right after the save; ``acting_has_url`` is
    False for an acting session with no URL (a desktop, RDP or Citrix
    session), whose screen is never the browser view the read opens.
    """

    basis: dict[str, str] = {}
    if own_browser and fresh_context and signed_in:
        separate: Tri = "yes"
        basis["separate_session"] = "own browser, new empty context, own sign-in"
    else:
        separate = "no"
        basis["separate_session"] = "the session was not a fresh signed-in one"
    if fetched is True:
        requery: Tri = "yes"
        basis["requery"] = "the record document came from the server"
    elif fetched is False:
        requery = "no"
        basis["requery"] = "the record document did not come from the server"
    elif fresh_context:
        requery = "yes"
        basis["requery"] = "a new context has no cache to serve the page from"
    else:
        requery = "unknown"
        basis["requery"] = "the driver did not say where the page came from"
    if not acting_bound:
        distinct: Tri = "unknown"
        basis["distinct_view"] = "no acting session is bound to compare with"
    elif not acting_has_url:
        distinct = "yes"
        basis["distinct_view"] = (
            "the acting session is not a browser; the read opens a web view"
        )
    else:
        same = same_view(read_url, acting_url)
        if same is True:
            distinct = "no"
            basis["distinct_view"] = "the read opened the page the save left on screen"
        elif same is False:
            distinct = "yes"
            basis["distinct_view"] = "the read opened a different view than the save"
        else:
            distinct = "unknown"
            basis["distinct_view"] = "the acting session's URL could not be read"
    return ReadIndependence(
        separate_session=separate,
        requery=requery,
        distinct_view=distinct,
        basis=basis,
        read_seconds=read_seconds,
    )


# -- history / audit view -------------------------------------------------------


class HistoryEntryReader(BaseModel):
    """CSS selectors, relative to one history row, for its columns."""

    model_config = ConfigDict(extra="forbid")

    #: The record the entry is about (a patient ID, a third-party ref).
    record: str = Field(min_length=1)
    #: What the entry says happened ("Third party modified", "patient-record update").
    what: str = Field(min_length=1)
    #: Who made the change, when the view shows it.
    user: Optional[str] = None
    #: An attribute to read instead of the text, for every column.
    attribute: Optional[str] = None


class HistoryView(BaseModel):
    """The application's history, audit or activity screen (optional)."""

    model_config = ConfigDict(extra="forbid")

    #: URL of the history view. ``{name}`` placeholders come from the
    #: read-back's ``url_params`` (never the captured reference: there is no
    #: reference before the save, and the view is read before it too).
    url: str
    ready_selector: str
    row_selector: str
    entry: HistoryEntryReader
    #: Regular expressions; every new entry about this record must match one.
    #: Learned from the demonstration's own history entries.
    explained: list[str] = Field(min_length=1, max_length=64)
    #: A save that leaves no history entry is INDETERMINATE (the view didn't
    #: show it). Turn off only for an application that doesn't log this save.
    expect_entry: bool = True
    #: Env var holding the run's own account name, so entries by other
    #: users are excused. Needs ``entry.user``.
    acting_user_env: Optional[str] = None

    @model_validator(mode="after")
    def _shape(self) -> "HistoryView":
        for pattern in self.explained:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"ui_readback.history.explained has an invalid pattern: {exc}"
                ) from exc
        if self.acting_user_env and not self.entry.user:
            raise ValueError("ui_readback.history.acting_user_env needs entry.user")
        if view_key(self.url.split("{", 1)[0] or self.url) is None:
            raise ValueError("ui_readback.history.url must be an absolute http(s) URL")
        return self

    def script_spec(self) -> dict[str, Any]:
        def reader(selector: Optional[str]) -> Optional[dict[str, Optional[str]]]:
            if not selector:
                return None
            return {"selector": selector, "attribute": self.entry.attribute}

        fields = {"record": reader(self.entry.record), "what": reader(self.entry.what)}
        if self.entry.user:
            fields["user"] = reader(self.entry.user)
        return {
            "ready": self.ready_selector,
            "notFound": None,
            "empty": None,
            "row": self.row_selector,
            "identity": {"selector": "", "attribute": None},
            "fields": fields,
        }


def _entry_key(row: Mapping[str, Optional[str]]) -> tuple[str, str, str]:
    def norm(value: Optional[str]) -> str:
        return " ".join(str(value or "").split())

    return (norm(row.get("record")), norm(row.get("what")), norm(row.get("user")))


@dataclass(frozen=True)
class HistoryJudgement:
    """``ok`` True: the history explains the save. Otherwise ``refuted``
    says whether it contradicts it (collateral) or only can't tell."""

    ok: bool
    refuted: bool
    reason: str
    new_entries: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


def judge_history(
    view: HistoryView,
    before: Optional[list[dict[str, Optional[str]]]],
    after: Optional[list[dict[str, Optional[str]]]],
    *,
    record_id: str,
    acting_user: Optional[str] = None,
) -> HistoryJudgement:
    """Judge the entries that appeared between the two reads."""

    if before is None or after is None:
        return HistoryJudgement(
            False, False, "the history view could not be read before and after the save"
        )
    remaining = Counter(_entry_key(r) for r in before)
    new: list[tuple[str, str, str]] = []
    for row in after:
        key = _entry_key(row)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            new.append(key)
    others = 0
    mine: list[tuple[str, str, str]] = []
    for key in new:
        if acting_user and key[2] and key[2] != acting_user:
            others += 1
            continue
        mine.append(key)
    target = " ".join(record_id.split())
    elsewhere = [k for k in mine if k[0] != target]
    detail = {
        "new_entries": len(new),
        "excused_other_users": others,
        "other_records": len(elsewhere),
    }
    if elsewhere:
        return HistoryJudgement(
            False,
            True,
            f"the history shows {len(elsewhere)} new entr"
            f"{'y' if len(elsewhere) == 1 else 'ies'} about another record: a "
            "collateral change",
            len(new),
            detail,
        )
    patterns = [re.compile(p) for p in view.explained]
    unexplained = [k for k in mine if not any(p.search(k[1]) for p in patterns)]
    detail["unexplained"] = len(unexplained)
    if unexplained:
        return HistoryJudgement(
            False,
            True,
            f"the history shows {len(unexplained)} new entr"
            f"{'y' if len(unexplained) == 1 else 'ies'} on this record that the "
            "save doesn't explain: a collateral change",
            len(new),
            detail,
        )
    if not mine and view.expect_entry:
        return HistoryJudgement(
            False,
            False,
            "the history shows no entry for the save",
            len(new),
            detail,
        )
    return HistoryJudgement(True, False, "", len(new), detail)


# -- provisional, then final ------------------------------------------------------


class FinalCheck(BaseModel):
    """Make the first read provisional and settle it with a later read."""

    model_config = ConfigDict(extra="forbid")

    #: Deferred-check ledger directory (created 0700).
    ledger: str
    #: Seconds after the provisional read before the final read may run:
    #: the quiet window in which late changes must land. Set per application
    #: from its observed behavior, never from evaluation data.
    after_s: float = Field(default=0.0, ge=0.0, le=90 * 24 * 3600)
    #: Hours after the save by which the record must reach a final status.
    deadline_hours: float = Field(gt=0, le=24 * 90)
    #: A field in ``ui_readback.fields`` that shows the record's status.
    #: Read at both checks, never compared with an input.
    status_field: Optional[str] = None
    #: Statuses that settle the record (the portal accepted or posted it).
    final_values: list[str] = Field(default_factory=list)
    #: Statuses that say the record is still in review: wait.
    pending_values: list[str] = Field(default_factory=list)
    #: Statuses that say it was turned down: reconcile.
    rejected_values: list[str] = Field(default_factory=list)
    describe: str = Field(default="the final read-back", max_length=120)
    #: Hours after which a record still provisional is reported by
    #: ``deferred settle`` (an SLA alert). None: no alert before the deadline.
    alert_hours: Optional[float] = Field(default=None, gt=0, le=24 * 90)

    @model_validator(mode="after")
    def _shape(self) -> "FinalCheck":
        values = [*self.final_values, *self.pending_values, *self.rejected_values]
        if self.status_field is None and values:
            raise ValueError("ui_readback.final status values need status_field")
        if self.status_field is not None and not self.final_values:
            raise ValueError("ui_readback.final.final_values must name a final status")
        folded = [v.casefold().strip() for v in values]
        if len(set(folded)) != len(folded):
            raise ValueError("ui_readback.final status values must not repeat")
        return self

    def status_of(
        self, value: Optional[str]
    ) -> Literal["final", "pending", "rejected", "unknown"]:
        if value is None:
            return "unknown"
        folded = " ".join(str(value).split()).casefold()
        for kind, values in (
            ("final", self.final_values),
            ("pending", self.pending_values),
            ("rejected", self.rejected_values),
        ):
            if folded in {" ".join(v.split()).casefold() for v in values}:
                return kind  # type: ignore[return-value]
        return "unknown"


def record_digest_payload(
    records: list[dict[str, Any]], *, exclude: tuple[str, ...] = ()
) -> str:
    """Canonical text of the records read, for a keyed digest (no values kept)."""

    rows = [{k: v for k, v in sorted(r.items()) if k not in exclude} for r in records]
    rows.sort(key=lambda r: json.dumps(r, sort_keys=True, default=str))
    return json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
