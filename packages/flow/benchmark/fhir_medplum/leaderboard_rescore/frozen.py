"""A read-only Medplum FHIR server frozen at a moment, served from its own history.

The write leaderboard's verifier lost its read token an hour into each run
and judged most runs on nothing (HTTP 401; see README.md). The server's
database outlived the runs: every version of every record (Medplum keeps
deleted versions in ``<Type>_History``) and every audit event. This module
answers the verifier's reads from that dump *as the server would have at a
given moment*, so ``ensure_record.verify_ensure`` runs unchanged on the
frozen runs, through its injectable ``get``.

What it serves, each as of the reader's clock (``now``):

- ``GET metadata``: a ``Date`` header, whole seconds, as Medplum sends it.
- ``GET <Type>/<id>``: the latest version at ``now``; 410 if that version is
  a delete, 404 if there is none yet.
- ``GET <Type>/<id>/_history``: the versions up to ``now``, newest first; a
  delete is an entry with ``request.method: DELETE`` and no resource
  (Medplum 5.2.8's ``readHistory``).
- ``GET <Type>?...``: the records live at ``now``, filtered by the search
  parameters the contracts use, with R4's semantics (below).
- ``GET AuditEvent?date=...&entity=...&action=...``: the events recorded up
  to ``now``.

Medplum logs a read by id and a history read as an ``AuditEvent`` (the
dumps hold the live verifier's own reads among them). So each read by id this
reader serves is logged as an event by ``reader_ref`` at ``now``, as the
server would: the verifier's heartbeat looks for its own read. Searches are
not logged, as on Medplum.

Search semantics (FHIR R4), for the parameters the three contracts use:

- string (``given``, ``family``): case-insensitive, and a match anywhere in
  the value, which is wider than R4's starts-with. A wider candidate set is
  safe: the contract's own comparison decides who is the person, and more
  candidates can only find more duplicates.
- date (``birthdate``): the same calendar day.
- token (``email``): ``telecom`` with ``system=email``, case-insensitive.
- reference (``patient``): ``AllergyIntolerance.patient``, or an
  ``Appointment`` participant's actor.
- ``_lastUpdated`` (``ge``/``le``) on the current version, compared as
  Medplum does: against the instant as given.

Any other parameter raises, so a contract change that needs another search
fails loudly instead of being served wrong.
"""

from __future__ import annotations

import gzip
import json
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _bound(value: str) -> tuple[str, datetime]:
    """``ge2026-11-03T02:34:19Z`` -> (``ge``, the instant)."""

    prefix, rest = value[:2], value[2:]
    if prefix not in ("ge", "le", "gt", "lt"):
        raise ValueError(f"unsupported date prefix in {value!r}")
    return prefix, _ts(rest)


def _in_bounds(when: datetime, bounds: list[str]) -> bool:
    # Medplum 5.2.8 compares the instant as given (``recorded <= '...41Z'``),
    # with no implicit range over the bound's precision.
    for raw in bounds:
        prefix, at = _bound(raw)
        if prefix == "ge" and not when >= at:
            return False
        if prefix == "gt" and not when > at:
            return False
        if prefix == "le" and not when <= at:
            return False
        if prefix == "lt" and not when < at:
            return False
    return True


class FrozenMedplum:
    """The dump of one Medplum project, read as of ``now``."""

    def __init__(self, dump: Path, base_url: str, reader_ref: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.reader_ref = reader_ref
        self.now = datetime.min.replace(tzinfo=timezone.utc)
        #: "Type/id" -> versions oldest first, as (lastUpdated, content).
        self.versions: dict[str, list[tuple[datetime, dict[str, Any]]]] = defaultdict(list)
        self.events: list[tuple[datetime, dict[str, Any]]] = []
        opener = gzip.open if dump.suffix == ".gz" else open
        with opener(dump, "rt") as fh:
            for line in fh:
                res = json.loads(line)
                if res["resourceType"] == "AuditEvent":
                    self.events.append((_ts(res["recorded"]), res))
                else:
                    ref = f"{res['resourceType']}/{res['id']}"
                    self.versions[ref].append((_ts(res["meta"]["lastUpdated"]), res))
        for vs in self.versions.values():
            vs.sort(key=lambda v: v[0])
        self.events.sort(key=lambda e: e[0])
        self._times = [t for t, _ in self.events]
        #: Reads this reader served, logged as the server would.
        self.served: list[tuple[datetime, dict[str, Any]]] = []

    # -- the clock -------------------------------------------------------------------

    def at(self, when: datetime) -> "FrozenMedplum":
        self.now = when
        self.served = []
        return self

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    # -- the store as of now ---------------------------------------------------------

    def _history(self, ref: str) -> list[tuple[datetime, dict[str, Any]]]:
        vs = self.versions.get(ref, [])
        return [v for v in vs if v[0] <= self.now]

    def current(self, ref: str) -> Optional[dict[str, Any]]:
        """The live version of ``ref`` at ``now``, None when absent or deleted."""

        hist = self._history(ref)
        if not hist or (hist[-1][1].get("meta") or {}).get("deleted"):
            return None
        return hist[-1][1]

    def live(self, rtype: str) -> list[dict[str, Any]]:
        out = []
        for ref in self.versions:
            if ref.split("/", 1)[0] == rtype:
                res = self.current(ref)
                if res is not None:
                    out.append(res)
        return out

    def _log_read(self, ref: str, subtype: str) -> None:
        self.served.append(
            (
                self.now,
                {
                    "resourceType": "AuditEvent",
                    "id": f"frozen-read-{len(self.served)}",
                    "action": "R",
                    "subtype": [{"code": subtype}],
                    "recorded": self.now.isoformat().replace("+00:00", "Z"),
                    "agent": [{"who": {"reference": self.reader_ref}}],
                    "entity": [{"what": {"reference": ref}}],
                    "outcome": "0",
                },
            )
        )

    def audit_events(self) -> list[dict[str, Any]]:
        upto = bisect_right(self._times, self.now)
        logged = [e for _, e in self.events[:upto]]
        return logged + [e for t, e in self.served if t <= self.now]

    # -- the FHIR read path ----------------------------------------------------------

    def get(
        self, url: str, params: Optional[Mapping[str, Any]]
    ) -> tuple[int, Mapping[str, str], Any]:
        if not url.startswith(self.base_url + "/"):
            raise ValueError(f"not this server: {url}")
        path = urlsplit(url).path[len(urlsplit(self.base_url).path) :].strip("/")
        parts = path.split("/")
        params = dict(params or {})
        headers = {"Date": format_datetime(self.now.replace(microsecond=0), usegmt=True)}
        if parts == ["metadata"]:
            return 200, headers, {"resourceType": "CapabilityStatement"}
        if len(parts) == 3 and parts[2] == "_history":
            ref = f"{parts[0]}/{parts[1]}"
            hist = self._history(ref)
            if not hist:
                return 404, headers, {"resourceType": "OperationOutcome"}
            self._log_read(ref, "history")
            entries = []
            for _, res in reversed(hist):
                if (res.get("meta") or {}).get("deleted"):
                    entries.append({"request": {"method": "DELETE", "url": ref}})
                else:
                    entries.append({"request": {"method": "GET"}, "resource": res})
            return 200, headers, {"resourceType": "Bundle", "type": "history", "entry": entries}
        if len(parts) == 2:
            ref = f"{parts[0]}/{parts[1]}"
            hist = self._history(ref)
            if not hist:
                return 404, headers, {"resourceType": "OperationOutcome"}
            if (hist[-1][1].get("meta") or {}).get("deleted"):
                return 410, headers, {"resourceType": "OperationOutcome"}
            self._log_read(ref, "read")
            return 200, headers, hist[-1][1]
        if len(parts) == 1:
            found = self.search(parts[0], params)
            return 200, headers, {
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [{"resource": r} for r in found],
            }
        raise ValueError(f"unsupported read: {url}")

    def search(self, rtype: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        params = dict(params)
        params.pop("_count", None)
        if rtype == "AuditEvent":
            rows = self.audit_events()
            date = params.pop("date", None)
            entity = params.pop("entity", None)
            action = params.pop("action", None)
            if params:
                raise ValueError(f"unsupported AuditEvent search: {sorted(params)}")
            out = []
            for e in rows:
                if date is not None:
                    bounds = [date] if isinstance(date, str) else list(date)
                    if not _in_bounds(_ts(e["recorded"]), bounds):
                        continue
                if action is not None and e.get("action") != action:
                    continue
                if entity is not None and not any(
                    (x.get("what") or {}).get("reference") == entity
                    for x in e.get("entity") or []
                ):
                    continue
                out.append(e)
            return out
        out = []
        for res in self.live(rtype):
            if all(self._matches(rtype, res, k, v) for k, v in params.items()):
                out.append(res)
        return out

    @staticmethod
    def _matches(rtype: str, res: Mapping[str, Any], name: str, value: Any) -> bool:
        if name == "_lastUpdated":
            bounds = [value] if isinstance(value, str) else list(value)
            return _in_bounds(_ts(res["meta"]["lastUpdated"]), bounds)
        want = str(value).strip()
        if rtype == "Patient" and name in ("given", "family"):
            got = []
            for n in res.get("name") or []:
                if name == "given":
                    got += n.get("given") or []
                else:
                    got.append(n.get("family") or "")
            return any(want.casefold() in str(g).casefold() for g in got)
        if rtype == "Patient" and name == "birthdate":
            return str(res.get("birthDate") or "")[:10] == want[:10]
        if rtype == "Patient" and name == "email":
            return any(
                t.get("system") == "email"
                and str(t.get("value") or "").casefold() == want.casefold()
                for t in res.get("telecom") or []
            )
        if name == "patient" and rtype == "AllergyIntolerance":
            got = str((res.get("patient") or {}).get("reference") or "")
            return got in (want, f"Patient/{want}")
        if name == "patient" and rtype == "Appointment":
            return any(
                str((p.get("actor") or {}).get("reference") or "") in (want, f"Patient/{want}")
                for p in res.get("participant") or []
            )
        raise ValueError(f"unsupported search parameter {rtype}?{name}")
