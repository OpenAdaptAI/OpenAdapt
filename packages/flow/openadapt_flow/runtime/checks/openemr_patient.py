"""OpenEMR adapter for the duplicate check: its REST API and its database.

The generic check (:mod:`openadapt_flow.runtime.checks.duplicate_record`)
searches FHIR R4. These sources speak OpenEMR's own interfaces, and
:func:`source_for_rest_verifier` picks one from an OpenEMR REST record check.
Everything OpenEMR-specific in the check lives here.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Literal, Optional
from urllib.parse import urljoin

from openadapt_flow.runtime.checks.duplicate_record import (
    MAX_PAGES,
    FhirPatientSource,
    PatientRecord,
    PatientSource,
    Unreadable,
    _get_json,
    _same_origin,
    full_date,
)


class SqlPatientSource:
    """OpenEMR adapter, fallback: a read-only query on ``patient_data``.

    The columns (``pid``, ``pubpid``, ``DOB``, ``birth_lname``, ...) are
    OpenEMR's.

    For a deployment with no FHIR or REST read. ``connect`` returns a DB-API
    connection with a SELECT-only account. OpenEMR runs on MySQL or MariaDB,
    so parameters default to ``%s`` (``paramstyle="format"``); pass
    ``paramstyle="qmark"`` for SQLite. The SQL itself is portable (the
    wildcard is bound, not concatenated).
    """

    channel = "sql"

    def __init__(
        self,
        connect: Callable[[], Any],
        *,
        table: str = "patient_data",
        paramstyle: Literal["format", "qmark"] = "format",
    ) -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,63}", table):
            raise ValueError("table must be a plain identifier")
        self._connect = connect
        self.table = table
        self.mark = "%s" if paramstyle == "format" else "?"
        self.pages_read = 0

    def _query(self, where: str, *values: str) -> list[PatientRecord]:
        try:
            conn = self._connect()
        except Exception as exc:  # noqa: BLE001
            raise Unreadable(f"the database is unreachable ({type(exc).__name__})")
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT pid, fname, lname, DOB, pubpid, phone_home, street, email, "
                f"birth_fname, birth_lname FROM {self.table} "
                f"WHERE {where.replace('?', self.mark)}",
                values,
            )
            rows = cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise Unreadable(f"the database query failed ({type(exc).__name__})")
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        self.pages_read += 1
        out = []
        for pid, f, ln, d, mrn, phone, street, email, bf, bl in rows:
            prior = ((str(bf or f or ""), str(bl)),) if bl else ()
            out.append(
                PatientRecord(
                    str(pid),
                    str(f or ""),
                    str(ln or ""),
                    full_date(d),
                    str(mrn or ""),
                    str(phone or ""),
                    str(street or ""),
                    str(email or ""),
                    prior,
                )
            )
        return out

    def by_birthdate(self, dob: str) -> list[PatientRecord]:
        return self._query("DOB = ?", dob)

    def by_family(self, family: str) -> list[PatientRecord]:
        # FHIR's string search is a case-insensitive prefix; match it.
        prefix = family.replace("%", "").replace("_", "") + "%"
        return self._query(
            "LOWER(lname) LIKE LOWER(?) OR LOWER(birth_lname) LIKE LOWER(?)",
            prefix,
            prefix,
        )

    def by_identifier(self, mrn: str) -> list[PatientRecord]:
        return self._query("pubpid = ?", mrn)

    def by_given(self, given: str) -> list[PatientRecord]:
        return self._query("LOWER(fname) = LOWER(?)", given)

    def by_phone(self, phone: str) -> list[PatientRecord]:
        return self._query("phone_home = ?", phone)

    def by_email(self, email: str) -> list[PatientRecord]:
        return self._query("LOWER(email) = LOWER(?)", email)


class RestPatientSource:
    """OpenEMR adapter: its standard REST API, ``GET .../api/patient?DOB=...``.

    The query parameters and fields (``fname``, ``lname``, ``DOB``,
    ``pubpid``, ``phone_home``) are OpenEMR's.

    OpenEMR answers with ``{"data": [...]}``. A next-page link
    (``links.next``) is followed. A full page with no next link may be
    truncated, so it makes the answer unreadable rather than incomplete.
    """

    channel = "rest"

    def __init__(
        self,
        base_url: str,
        *,
        path: str = "/apis/default/api/patient",
        headers: Optional[dict[str, str]] = None,
        session: Any = None,
        timeout_s: float = 10.0,
        page_size: int = 500,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.path = path.split("?", 1)[0]
        self.headers = dict(headers or {})
        self._session = session
        self.timeout_s = timeout_s
        self.page_size = page_size
        self.pages_read = 0

    def _client(self) -> Any:
        if self._session is None:
            import httpx

            self._session = httpx.Client()
        return self._session

    def search(self, params: dict[str, str]) -> list[PatientRecord]:
        url: Optional[str] = f"{self.base_url}{self.path}"
        query: Optional[dict[str, Any]] = {**params, "_limit": self.page_size}
        out: list[PatientRecord] = []
        pages = 0
        while url is not None:
            if pages >= MAX_PAGES:
                raise Unreadable(
                    f"more than {MAX_PAGES} pages; the search was not read to the end"
                )
            body, at = _get_json(
                self._client(), url, query, self.headers, self.timeout_s
            )
            pages += 1
            self.pages_read += 1
            if not isinstance(body, dict):
                raise Unreadable("the patient search did not return an object")
            errors = [
                k
                for k in ("validationErrors", "internalErrors", "errors", "error")
                if body.get(k)
            ]
            if errors:
                raise Unreadable("the patient search reported " + ", ".join(errors))
            rows = body.get("data")
            if not isinstance(rows, list):
                raise Unreadable("the patient search did not return a data list")
            for row in rows:
                if not isinstance(row, dict):
                    raise Unreadable("a patient row is not an object")
                out.append(
                    PatientRecord(
                        record_id=str(
                            row.get("uuid") or row.get("pid") or row.get("id") or ""
                        ),
                        fname=str(row.get("fname") or ""),
                        lname=str(row.get("lname") or ""),
                        dob=full_date(row.get("DOB")),
                        mrn=str(row.get("pubpid") or ""),
                        phone=str(row.get("phone_home") or ""),
                        street=str(row.get("street") or ""),
                        email=str(row.get("email") or ""),
                        prior_names=(
                            (
                                (
                                    str(
                                        row.get("birth_fname") or row.get("fname") or ""
                                    ),
                                    str(row["birth_lname"]),
                                ),
                            )
                            if row.get("birth_lname")
                            else ()
                        ),
                    )
                )
            links = body.get("links") if isinstance(body, dict) else None
            nxt = links.get("next") if isinstance(links, dict) else None
            if isinstance(nxt, str) and nxt.strip():
                url = _same_origin(self.base_url, urljoin(at, nxt.strip()))
                query = None
            elif len(rows) >= self.page_size:
                raise Unreadable(
                    "a full page with no next link; the search may be truncated"
                )
            else:
                url = None
        return out

    def by_birthdate(self, dob: str) -> list[PatientRecord]:
        return self.search({"DOB": dob})

    def by_family(self, family: str) -> list[PatientRecord]:
        # OpenEMR matches lname exactly here, so compare prefixes locally
        # against an exact read: a hyphenated or married name is still found
        # through the date of birth.
        return self.search({"lname": family})

    def by_identifier(self, mrn: str) -> list[PatientRecord]:
        return self.search({"pubpid": mrn})

    def by_given(self, given: str) -> list[PatientRecord]:
        return self.search({"fname": given})

    def by_phone(self, phone: str) -> list[PatientRecord]:
        return self.search({"phone_home": phone})

    def by_email(self, email: str) -> list[PatientRecord]:
        return self.search({"email": email})


def source_for_rest_verifier(
    verifier: Any,
    *,
    allow_rest: bool = False,
    channel: Optional[Literal["fhir", "rest"]] = None,
) -> Optional[PatientSource]:
    """The patient search for an OpenEMR REST record check, or None.

    Only a verifier on OpenEMR's ``.../api/patient`` qualifies. ``fhir``
    searches OpenEMR's FHIR API on the same server (``.../apis/<site>/fhir``)
    with the verifier's bearer token, which then needs the FHIR read scope
    (``api:fhir user/Patient.read``). Otherwise, only with ``allow_rest``,
    the REST adapter.
    """

    from openadapt_flow.runtime.effects.rest import RestRecordVerifier

    records_path = str(getattr(verifier, "records_path", "") or "")
    if (
        channel == "fhir"
        and isinstance(verifier, RestRecordVerifier)
        and "/api/patient" in records_path
    ):
        # OpenEMR serves FHIR beside its REST API: .../apis/default/fhir.
        site = records_path.split("?", 1)[0].split("/api/patient", 1)[0]
        bearer = str(
            (getattr(verifier, "headers", None) or {}).get("Authorization", "")
        )
        token = bearer.removeprefix("Bearer ").strip()
        return FhirPatientSource(
            f"{verifier.base_url}{site}/fhir",
            token=(lambda: token) if token else None,
            session=verifier._get_session(),
            timeout_s=max(float(getattr(verifier, "timeout_s", 10.0)), 10.0),
        )
    if channel == "fhir":
        return None
    if (
        allow_rest
        and isinstance(verifier, RestRecordVerifier)
        and "/api/patient" in records_path
    ):
        return RestPatientSource(
            verifier.base_url,
            path=records_path,
            headers=getattr(verifier, "headers", None),
            session=verifier._get_session(),
            timeout_s=max(float(getattr(verifier, "timeout_s", 10.0)), 10.0),
        )
    return None
