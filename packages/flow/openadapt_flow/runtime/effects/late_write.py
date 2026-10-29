"""Late writes: judge the database again some time after a verified save.

The whole-database frame (``frame.database``) reads the database twice after
the save, ``settle_s`` apart, and judges what moved. A write that lands later
(a background job, a trigger-driven integration, a delayed duplicate side
record) lands after that verdict, and nothing sees it. With
``frame.database.recheck_after_s`` set, a VERIFIED write also schedules a
recheck:

- the receipt says when it is due (``recheck_due_at``), and its evidence
  says what it compares and binds a salted digest of the state it compares
  with (``evidence.frame.recheck``), so a caller can schedule it without
  blocking;
- the caller keeps a private recheck state: the settled post-save snapshot
  (keyed MACs, never values, as the baseline keeps them since #1599), the
  audit entries seen then (keyed hashes), the salt, the receipt and its
  evidence;
- at or after the due time the database is read again (twice, ``settle_s``
  apart, as after the save) and every change since the post-save read is
  judged by the frame's own rules: the profile's bookkeeping, another actor's
  write the audit log attributes, and what later writes under the same
  contract by the same actor account for (``excused_by``, any outcome):
  their declared effects and side records, and, where their own frame was
  judged, every row and audit entry in their own window, which they already
  explained (VERIFIED) or reported against themselves (REFUTED). Anything
  else unexplained is collateral, and the earlier verdict becomes REFUTED
  (late).

This module holds the parts that need no read: the state's shape, the due
time and the outcome vocabulary. ``verify_run.recheck_run`` does the read and
the judgment, and ``openadapt_flow.verifier.recheck`` is the public entry.

Fail-closed: a state that is malformed, doesn't match its receipt, or was
taken under another contract, and a database that can't be read or is still
moving, give UNCERTAIN, never CONFIRMED.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

#: The private recheck state's schema.
RECHECK_SCHEMA = "openadapt.verify-recheck/v1"
#: Exactly these keys: nothing rides along unbound.
STATE_KEYS: tuple[str, ...] = (
    "schema_version",
    "execution_id",
    "salt",
    "verifier_identity",
    "baseline",
    "receipt",
    "evidence",
    "post_state",
)
#: The key under ``evidence.frame`` that says a recheck is due.
EVIDENCE_KEY = "recheck"
#: What the recheck compares: the whole database now against the settled
#: post-save read.
COMPARES = "database_since_post_save"
#: The salted-digest label of the post-save state.
POST_STATE_LABEL = "recheck_post_state"
#: The rule name a change gets when a later write's own window covers it.
LATER_WRITE_RULE = "later_write_window"
#: The same, when the change may be what refuted that later write.
LATER_WRITE_DISPUTED = "later_write_window_disputed"

#: ``CONFIRMED``: nothing unexplained moved since the post-save read.
#: ``REFUTED``: something did (late collateral). ``UNCERTAIN``: the database,
#: or the state, couldn't be read or doesn't match.
LateOutcome = Literal["CONFIRMED", "REFUTED", "UNCERTAIN"]

_ISO_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def parse_time(value: str) -> datetime:
    """Whole-second UTC (``2026-10-29T12:00:00Z``)."""

    if not isinstance(value, str) or not _ISO_SECONDS.match(value):
        raise ValueError(f"{value!r} is not whole-second UTC")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def format_time(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def due_at(read_at: str, after_s: float) -> str:
    """When the recheck is due: ``after_s`` after the post-save read, never
    early. ``read_at`` is truncated to the second, so the read happened up
    to a second after it: one second is added before rounding up."""

    return format_time(parse_time(read_at) + timedelta(seconds=math.ceil(after_s + 1)))


def seconds_until(due: str, now: Optional[datetime] = None) -> float:
    """Seconds until ``due`` (0 or less when it is due)."""

    current = now or datetime.now(timezone.utc)
    return (parse_time(due) - current).total_seconds()


def schedule(
    post_state: Mapping[str, Any],
    after_s: float,
    post_state_sha256: str,
    *,
    due: bool,
) -> dict[str, Any]:
    """The ``evidence.frame.recheck`` block: closed fields only. ``due``: the
    write was verified, so a recheck is due (``due_at``); otherwise
    ``due_at`` is None and the block only binds the post-save state."""

    read_at = str(post_state["read_at"])
    return {
        "compares": COMPARES,
        "after_s": after_s,
        "post_save_read_at": read_at,
        "due_at": due_at(read_at, after_s) if due else None,
        "post_state_sha256": post_state_sha256,
    }


def state_problem(payload: Any) -> Optional[str]:
    """A plain sentence when ``payload`` is not a recheck state's shape."""

    if not isinstance(payload, Mapping):
        return "the recheck state is not a JSON object"
    if payload.get("schema_version") != RECHECK_SCHEMA:
        return f"the recheck state is not an {RECHECK_SCHEMA} document"
    if set(payload) != set(STATE_KEYS):
        return "the recheck state must hold exactly " + ", ".join(STATE_KEYS)
    try:
        salt = bytes.fromhex(str(payload["salt"]))
    except ValueError:
        return "the recheck state's salt is not hex"
    if len(salt) != 32:
        return "the recheck state's salt is not 32 bytes"
    for name in ("baseline", "receipt", "evidence", "post_state"):
        if not isinstance(payload[name], Mapping):
            return f"the recheck state's {name} is not an object"
    post = payload["post_state"]
    if not isinstance(post.get("snapshot"), Mapping):
        return "the recheck state holds no post-save snapshot"
    try:
        parse_time(str(post.get("read_at")))
    except ValueError:
        return "the recheck state's post-save read time is not whole-second UTC"
    audit = post.get("audit")
    if audit is not None and not (
        isinstance(audit, Mapping)
        and isinstance(audit.get("reachable"), bool)
        and isinstance(audit.get("ids"), list)
    ):
        return "the recheck state's audit entries are malformed"
    try:
        from openadapt_flow.runtime.effects.db_snapshot import Snapshot

        Snapshot.from_json(post["snapshot"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return "the recheck state's post-save snapshot can't be read"
    return None


def outcome_of(frame_status: str) -> LateOutcome:
    """The frame's status since the post-save read, as the recheck's outcome."""

    if frame_status == "held":
        return "CONFIRMED"
    if frame_status == "refuted":
        return "REFUTED"
    return "UNCERTAIN"


class LaterWindow:
    """What one later write's own frame already judged: every row that
    changed between its baseline and its settled post-save read, and every
    audit entry logged in that window.

    A change there was judged by that write's frame: explained, if that
    write was verified; otherwise it may be what refuted it. An earlier
    write's recheck therefore treats a change in a later write's window as
    that write's (``clean``) only when the later write was verified, or when
    the change was neither its reported collateral nor possibly behind a
    failed delta. Otherwise the change is ``disputed``: it doesn't refute the
    earlier write, but it doesn't confirm it either, since the harm may be
    the earlier write's.

    Only the window counts. A row that matches the later write's selector
    but lands after its post-save read, or a row changed again since, is
    judged like any other. A row counts while it still holds the value the
    post-save read saw (matched by key and contents MAC under the later
    write's own key); a removal counts when that write's window recorded it
    (matched by the row's link MAC, ``db_snapshot.link_key``). A table
    without a primary key (rows keyed by a content fingerprint) is never
    excused this way.
    """

    def __init__(
        self,
        key: bytes,
        baseline: Mapping[str, Any],
        post: Mapping[str, Any],
        *,
        verified: bool,
        delta_held: bool,
        collateral_rows: set[tuple[str, str]],
        collateral_entries: set[str],
        unmapped: bool,
        audit_before: Optional[set[str]] = None,
        audit_after: Optional[set[str]] = None,
        entry_id: Optional[Any] = None,
    ) -> None:
        from openadapt_flow.runtime.effects.db_snapshot import Snapshot

        self.key = key
        self.baseline = Snapshot.from_json(baseline)
        self.post = Snapshot.from_json(post)
        self.verified = verified
        self.delta_held = delta_held
        self.collateral_rows = collateral_rows
        self.collateral_entries = collateral_entries
        self.unmapped = unmapped
        self.audit_before = audit_before
        self.audit_after = audit_after
        self.entry_id = entry_id
        self._base_links = {
            name: {link: k for k, link in t.links.items()}
            for name, t in self.baseline.tables.items()
        }
        self._post_links = {
            name: set(t.links.values()) for name, t in self.post.tables.items()
        }

    def _status(self, collateral: bool) -> str:
        if self.verified:
            return "clean"
        if collateral or self.unmapped or not self.delta_held:
            return "disputed"
        return "clean"

    def row(
        self,
        table: str,
        before: Optional[Mapping[str, Any]],
        after: Optional[Mapping[str, Any]],
        skip: list[str],
        links: Mapping[str, str],
    ) -> Optional[str]:
        """``clean`` or ``disputed`` when this row reached its present state
        (or its removal) inside the window; None when it didn't.

        ``before``/``after``: the row as the recheck's judge sees it (the
        earlier write's post-save MACs with ``KEY``, the plain row now).
        ``links``: the earlier post-save snapshot's link MACs of ``table``.
        """

        from openadapt_flow.runtime.effects.db_snapshot import (
            KEY,
            ROW,
            canonical,
            row_hash,
            row_key_hash,
        )

        now = self.post.tables.get(table)
        if now is None or now.mode != "compare" or not now.primary_key:
            return None  # no key to look the row up by: judged as usual
        old = self.baseline.tables.get(table)
        if after is not None:
            row = {c: v for c, v in after.items() if c not in (KEY, ROW)}
            text = "|".join(canonical(row.get(c)) for c in now.primary_key)
            k = row_key_hash(self.key, text, table)
            stored = now.rows.get(k)
            if stored is None or stored[0] != row_hash(self.key, table, row, skip):
                return None  # not what that write's post-save read saw
            was = None if old is None else old.rows.get(k)
            if was is not None and was[0] == stored[0]:
                return None  # unchanged in that window
        else:
            if before is None:
                return None
            link = links.get(str(before.get(KEY)))
            found = self._base_links.get(table, {}).get(link) if link else None
            if found is None or link in self._post_links.get(table, set()):
                return None  # that window didn't record this removal
            k = found
        return self._status((table, k) in self.collateral_rows)

    def entry(self, entry: Mapping[str, Any]) -> Optional[str]:
        """``clean`` or ``disputed`` when this audit entry was logged inside
        the window; None when it wasn't (or the log wasn't read there)."""

        if self.audit_before is None or self.audit_after is None:
            return None
        if self.entry_id is None:
            return None
        h = self.entry_id(entry, self.key)
        if h not in self.audit_after or h in self.audit_before:
            return None
        return self._status(h in self.collateral_entries)
