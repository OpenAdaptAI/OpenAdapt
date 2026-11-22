"""The values a guarded save's approval card shows, next to their source.

A person approving an agent's save should see, for every field the task
writes, what the record holds now, what the save writes, and what the source
document says. :func:`build_card` makes those rows for one guarded save:

* ``before``: the record's value now, read from the system of record through
  the same ``verify.yaml`` reader the guard's baseline uses, before anything
  is written. ``None`` when the save creates a record (nothing was there) or
  the current value can't be read.
* ``after``: the value the held request actually carries, read by the app's
  write adapter, the same parser the guard checks the write with
  (:meth:`openadapt_flow.teach_scope.TeachScope.request_refusal`). With no
  write adapter, or a body the adapter can't read, it is ``not read``: the
  guard does not parse request bodies itself, so it can't say what the save
  sends. It is never the task's intended value standing in for it.
* ``intended``: the value the task says to write, beside ``after``, with
  ``held_differs`` set when the two differ by the read-back's value
  comparison.
* ``screen``: with no write adapter, the value the agent's form shows at the
  moment the save is held (:mod:`.screen_values`), with ``screen_differs``
  set when it differs from ``intended``. The card's screenshot is taken at
  the same moment. With neither a value read nor a readable screenshot, no
  card is offered: the guard refuses the save with ``values_unread``.
* ``source``: the value the task's provenance cites, checked a second way
  against the source document (:func:`openadapt_flow.provenance.cross_check`).

A value that can't be resolved is shown as ``not read``, never blank, and the
card's intent says that the approval does not cover it. When any value the
save sends is not read, the card says that the values were not read and were
not checked before saving (``values_read`` false), never that it shows exactly
what will be saved.

The ``before`` and ``after`` rows go into the item-approval request as its
``fields`` (inside the request digest, local only, never sent to Cloud). The
source column is kept beside the request in ``approval_sources.json`` (0600),
bound to the request's digest; the console shows it only for that request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from openadapt_flow.runtime.identity import normalize_structured

__all__ = [
    "NOT_READ",
    "SOURCES_FILENAME",
    "SOURCES_SCHEMA",
    "ApprovalCard",
    "SourceRow",
    "build_card",
    "bound_fields",
    "card_for_request",
    "matching_records",
    "read_records",
    "read_sources_for",
    "write_sources",
]

NOT_READ = "not read"
SOURCES_FILENAME = "approval_sources.json"
SOURCES_SCHEMA = "openadapt.guard-approval-sources/v1"
_MAX_VALUE = 512


@dataclass(frozen=True)
class SourceRow:
    """One field's source column: the cited value and how it was checked."""

    label: str
    #: The source's value, ``not read`` when it can't be, or None when the
    #: task gives no source for this field.
    source: Optional[str]
    #: ``agree`` | ``disagree`` | ``missing`` | ``no_document`` |
    #: ``not_independent`` | ``instruction`` | ``no_source``
    status: str
    #: The source disagrees with the value saved, by the same value
    #: comparison the read-back uses. The phone only renders this.
    differs: bool = False
    #: The task's intended value for this field.
    intended: Optional[str] = None
    #: The value the held request sends differs from ``intended``. False when
    #: the sent value was not read (the card then says it wasn't checked).
    held_differs: bool = False
    #: The value the form showed when the save was held; None: not read.
    screen: Optional[str] = None
    #: ``screen`` differs from ``intended``.
    screen_differs: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "source": self.source,
            "status": self.status,
            "differs": self.differs,
            "intended": self.intended,
            "held_differs": self.held_differs,
            "screen": self.screen,
            "screen_differs": self.screen_differs,
        }


@dataclass
class ApprovalCard:
    """What :func:`build_card` resolved for one save."""

    fields: list[Any]
    sources: list[SourceRow]
    #: Field names whose value the card can't show (``not read``).
    not_covered: list[str] = field(default_factory=list)
    #: Every value the save sends was read from the held request.
    values_read: bool = False
    #: How the sent values were read: ``write_adapter``, or why not
    #: (``no_held_request``, ``no_write_adapter``, ``unreadable``).
    values_how: str = "no_held_request"
    #: At least one field's value was read from the screen at the hold.
    screen_read: bool = False

    def nothing_visible(self, screenshot: Optional[bytes]) -> bool:
        """No value was read (not from the request, not from the screen) and
        there is no screenshot a person could look at: nobody could see what
        would be approved."""

        from openadapt_flow.agent_guard.screen_values import readable_png

        return (
            not self.values_read
            and not self.screen_read
            and not readable_png(screenshot)
        )

    def intent_note(self) -> str:
        """The sentence the card's intent carries when something is unread."""

        note = ""
        if not self.values_read:
            note += (
                ". The values this save sends could not be read, so they were "
                "not checked before saving"
            )
            if self.screen_read:
                note += "; compare what was on screen at the save with the task"
        if self.not_covered:
            note += ". Not read, so this approval does not cover: " + ", ".join(
                sorted(self.not_covered)
            )
        return note


def _text(value: Any) -> str:
    return ("" if value is None else str(value))[:_MAX_VALUE]


def bound_fields(verify: Any) -> dict[str, str]:
    """Task parameter -> record field, from the effect contract."""

    from openadapt_flow.provenance import bound_fields as _bound

    try:
        return dict(_bound(verify.effects))
    except Exception:  # noqa: BLE001 - an unusual contract: nothing is bound
        return {}


def _creates(verify: Any) -> bool:
    """Every effect adds a record (so nothing was there before)."""

    effects = list(getattr(verify, "effects", []) or [])
    return bool(effects) and all(
        getattr(e, "kind", "") == "record_written"
        and bool(getattr(e, "count_new_only", False))
        for e in effects
    )


def read_records(verify: Any, params: Mapping[str, Any]) -> Optional[list[dict]]:
    """The records the ``verify.yaml`` reader returns now, or None.

    A fresh read of the system of record, independent of the screen. Raw
    values: kept in memory and in 0600 files only.
    """

    from openadapt_flow.deployment import build_effect_verifier

    try:
        verifier = build_effect_verifier(verify.verifier, dict(params))
        state = verifier.capture_pre_state() if verifier is not None else None
    except Exception:  # noqa: BLE001 - unreadable: the card says not read
        return None
    if state is None or state.reachable is not True:
        return None
    return [dict(r) for r in state.records if isinstance(r, Mapping)]


def matching_records(
    records: Optional[Sequence[Mapping[str, Any]]],
    expected_record: Mapping[str, str],
    bound: Mapping[str, str],
) -> Optional[list[Mapping[str, Any]]]:
    """The records whose bound identity fields equal the task's record.

    None when no identity field of the task is bound in the contract (the
    record can't be told apart) or the records weren't read.
    """

    keys = {bound[p]: v for p, v in expected_record.items() if p in bound}
    if records is None or not keys:
        return None
    return [
        r
        for r in records
        if all(
            normalize_structured(_text(r.get(f))) == normalize_structured(str(v))
            for f, v in keys.items()
        )
    ]


def _sources(task: Any) -> dict[str, SourceRow]:
    provenance = getattr(task, "provenance", None)
    if provenance is None:
        return {}
    from openadapt_flow.provenance import cross_check, default_extractor

    try:
        check = cross_check(provenance, task.documents, default_extractor(provenance))
    except Exception:  # noqa: BLE001 - the card shows the source as not read
        return {
            name: SourceRow(name, NOT_READ, "missing") for name in provenance.params
        }
    out: dict[str, SourceRow] = {}
    for item in check.fields:
        cited = provenance.params[item.name].value
        if item.result == "agree":
            value: Optional[str] = cited
        elif item.result == "disagree":
            # The second reading: what the document says, not what was cited.
            value = item.second if item.second is not None else NOT_READ
        elif item.result == "instruction":
            value = cited
        else:
            value = NOT_READ
        out[item.name] = SourceRow(item.name, _text(value), item.result)
    return out


def _source_differs(row: SourceRow, after: str) -> bool:
    if row.status == "disagree":
        return True
    if row.source is None or row.source == NOT_READ or after == NOT_READ:
        return False
    from openadapt_flow.agent_guard.readback import same_value

    return not same_value(row.source, after)


def build_card(
    task: Any,
    verify: Any,
    *,
    claim_fields: Optional[Mapping[str, Any]] = None,
    records: Optional[Sequence[Mapping[str, Any]]] = None,
    read: bool = True,
    values_how: Optional[str] = None,
    screen_fields: Optional[Mapping[str, str]] = None,
) -> ApprovalCard:
    """The approval card's rows for one guarded save. Never raises.

    ``claim_fields`` are the values the held request carries, per the app's
    write adapter, keyed by task field name; None when they weren't read
    (every ``after`` is then ``not read``). ``records`` is a read the caller
    already made; otherwise one is made now when ``read``. ``values_how``
    says why the values weren't read, for the card's sources file.
    ``screen_fields`` are the values the form showed at the hold, keyed by
    task field name (:func:`.screen_values.match_fields`).
    """

    from openadapt_flow.approval_mode.item_approval import MAX_SAVE_FIELDS, SaveField

    bound = bound_fields(verify)
    if records is None and read:
        records = read_records(verify, task.params)
    current = matching_records(records, task.expected_record, bound)
    creates = _creates(verify)
    sources = _sources(task)
    rows: list[Any] = []
    source_rows: list[SourceRow] = []
    not_covered: list[str] = []
    for name in sorted(task.intended_fields)[:MAX_SAVE_FIELDS]:
        before: Optional[str] = None
        if not creates:
            record_field = bound.get(name)
            if current is not None and len(current) == 1 and record_field:
                value = current[0].get(record_field)
                before = None if value is None else _text(value)
            else:
                # Which record, or its value, isn't known: shown, not hidden.
                before = NOT_READ
                not_covered.append(name)
        intended = _text(task.intended_fields[name])
        if (
            claim_fields is not None
            and name in claim_fields
            and claim_fields[name] is not None
        ):
            after = _text(claim_fields[name])
        else:
            # Never the task's value in its place: what is sent is unknown.
            after = NOT_READ
            if name not in not_covered:
                not_covered.append(name)
        rows.append(SaveField(label=name[:128], before=before, after=after))
        row = sources.get(name) or SourceRow(name, None, "no_source")
        on_screen = (screen_fields or {}).get(name)
        on_screen = None if on_screen is None else _text(on_screen)
        row = replace(
            row,
            differs=_source_differs(row, intended if after == NOT_READ else after),
            intended=intended,
            held_differs=after != NOT_READ and _differs(intended, after),
            screen=on_screen,
            screen_differs=on_screen is not None and _differs(intended, on_screen),
        )
        source_rows.append(row)
        if row.source == NOT_READ and name not in not_covered:
            not_covered.append(name)
    if len(task.intended_fields) > MAX_SAVE_FIELDS:
        not_covered.extend(sorted(task.intended_fields)[MAX_SAVE_FIELDS:])
    values_read = (
        claim_fields is not None
        and bool(rows)
        and all(r.after != NOT_READ for r in rows)
        and len(task.intended_fields) <= MAX_SAVE_FIELDS
    )
    if values_read:
        how = "write_adapter"
    elif values_how is not None:
        how = values_how
    else:
        how = "no_held_request" if claim_fields is None else "unreadable"
    return ApprovalCard(
        fields=rows,
        sources=source_rows,
        not_covered=not_covered,
        values_read=values_read,
        values_how=how,
        screen_read=any(r.screen is not None for r in source_rows),
    )


def _differs(intended: str, sent: str) -> bool:
    from openadapt_flow.agent_guard.readback import same_value

    return not same_value(intended, sent)


def write_sources(run_dir: Path, request: Any, card: ApprovalCard) -> Path:
    """Keep the card's source column next to the request, bound to its digest."""

    from openadapt_flow.guard import _write_json

    path = Path(run_dir) / SOURCES_FILENAME
    _write_json(
        path,
        {
            "schema": SOURCES_SCHEMA,
            "request_id": request.request_id,
            "request_sha256": request.digest(),
            "rows": [row.to_dict() for row in card.sources],
            "not_covered": sorted(card.not_covered),
            "values": {
                "read": card.values_read,
                "how": card.values_how,
                "screen_read": card.screen_read,
            },
        },
    )
    return path


def read_sources_for(run_dir: Path, request: Any) -> Optional[list[dict[str, Any]]]:
    """The source rows kept for exactly this request, or None.

    Anything else (another request's file, an edited request, a malformed
    file) shows no source column rather than a wrong one.
    """

    path = Path(run_dir) / SOURCES_FILENAME
    try:
        if path.is_symlink() or not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != SOURCES_SCHEMA
        or raw.get("request_id") != request.request_id
        or raw.get("request_sha256") != request.digest()
        or not isinstance(raw.get("rows"), list)
    ):
        return None
    rows = []
    for row in raw["rows"]:
        if not isinstance(row, dict) or not isinstance(row.get("label"), str):
            return None
        source = row.get("source")
        if source is not None and not isinstance(source, str):
            return None
        intended = row.get("intended")
        if intended is not None and not isinstance(intended, str):
            return None
        screen = row.get("screen")
        if screen is not None and not isinstance(screen, str):
            return None
        rows.append(
            {
                "label": row["label"][:128],
                "source": None if source is None else source[:_MAX_VALUE],
                "status": str(row.get("status") or "")[:32],
                "differs": row.get("differs") is True,
                "intended": None if intended is None else intended[:_MAX_VALUE],
                "held_differs": row.get("held_differs") is True,
                "screen": None if screen is None else screen[:_MAX_VALUE],
                "screen_differs": row.get("screen_differs") is True,
            }
        )
    return rows


def card_for_request(
    guard: Any,
    request: Any,
    scope: Any,
    *,
    controls: Optional[Sequence[Mapping[str, str]]] = None,
) -> Any:
    """The commit guard's card for a held save: the values the held request
    carries, read by the app's write adapter (the parser the guard checks
    writes with), beside the task's values. With no adapter, or a body it
    can't read, every sent value is ``not read`` and the card says they were
    not checked. Without an adapter, ``controls`` (the form at the hold,
    :meth:`~.cdp.CdpGuardPage.form_controls`) give each field's value on
    screen. None when no card is asked for (or it is turned off)."""

    owner = guard.owner
    asked = owner.approval == "each_item" or guard.source_check == "hold"
    if not owner.approval_fields or not asked:
        return None
    adapter = getattr(scope, "write_adapter", None) if scope is not None else None
    if adapter is None:
        from openadapt_flow.agent_guard.screen_values import match_fields

        return build_card(
            guard.task,
            owner.verify,
            claim_fields=None,
            values_how="no_write_adapter",
            screen_fields=match_fields(
                controls, sorted(guard.task.intended_fields), bound_fields(owner.verify)
            ),
        )
    try:
        body = request.post_data.encode("utf-8") if request.post_data else None
        claim = adapter.claim(request.method, request.url, body)
    except Exception:  # noqa: BLE001 - unread: every value shows not read
        claim = None
    if claim is None:
        return build_card(
            guard.task, owner.verify, claim_fields=None, values_how="unreadable"
        )
    return build_card(guard.task, owner.verify, claim_fields=dict(claim.fields))
