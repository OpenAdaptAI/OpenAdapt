"""App adapters for the duplicate check: an app's own REST API or database.

The generic check (:mod:`openadapt_flow.runtime.checks.duplicate_record`)
searches FHIR R4. Some apps also have their own patient search, or only a
database. What each app's search looks like (paths, query parameters, field
names, the table and its columns) is data, in
``app_profiles/record_search/<app>.yaml``, read by :func:`load_record_search`.
These adapters only follow it, and :func:`source_for_rest_verifier` picks one
for a REST record check whose records path an app's file names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Literal, Optional
from urllib.parse import urljoin

import yaml

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

#: Where each app's record-search file lives.
SEARCH_DIR = Path(__file__).resolve().parents[2] / "app_profiles" / "record_search"

#: The searches the check makes, and the record fields it reads.
SEARCHES = ("birthdate", "family", "identifier", "given", "phone", "email")
FIELDS = (
    "record_id",
    "fname",
    "lname",
    "dob",
    "mrn",
    "phone",
    "street",
    "email",
    "birth_fname",
    "birth_lname",
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,63}")


@dataclass(frozen=True)
class RestSearch:
    """An app's REST patient search, as its record-search file declares it."""

    rows_key: str
    params: dict[str, str]
    fields: dict[str, tuple[str, ...]]
    next_link: tuple[str, ...] = ()
    page_size_param: Optional[str] = None
    error_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        missing = set(SEARCHES) - set(self.params)
        if missing:
            raise ValueError(f"rest.params lacks {sorted(missing)}")
        unknown = set(self.fields) - set(FIELDS)
        if unknown or not self.fields.get("record_id"):
            raise ValueError("rest.fields needs record_id and only known fields")


@dataclass(frozen=True)
class SqlSearch:
    """An app's patient table, as its record-search file declares it."""

    table: str
    columns: dict[str, str]

    def __post_init__(self) -> None:
        names = [self.table, *self.columns.values()]
        if not all(_IDENT.fullmatch(n) for n in names):
            raise ValueError("sql table and columns must be plain identifiers")
        if set(self.columns) != set(FIELDS):
            raise ValueError(f"sql.columns needs exactly {list(FIELDS)}")


@dataclass(frozen=True)
class RecordSearch:
    """One app's record-search file."""

    name: str
    rest_records_path_marker: Optional[str] = None
    fhir_beside_rest: Optional[str] = None
    rest: Optional[RestSearch] = None
    sql: Optional[SqlSearch] = None


def _parse(doc: dict[str, Any]) -> RecordSearch:
    rest = doc.get("rest")
    sql = doc.get("sql")
    return RecordSearch(
        name=str(doc["name"]),
        rest_records_path_marker=doc.get("rest_records_path_marker"),
        fhir_beside_rest=doc.get("fhir_beside_rest"),
        rest=None
        if rest is None
        else RestSearch(
            rows_key=str(rest["rows_key"]),
            params={str(k): str(v) for k, v in rest["params"].items()},
            fields={
                str(k): tuple(str(x) for x in v) for k, v in rest["fields"].items()
            },
            next_link=tuple(str(x) for x in rest.get("next_link") or ()),
            page_size_param=rest.get("page_size_param"),
            error_keys=tuple(str(x) for x in rest.get("error_keys") or ()),
        ),
        sql=None
        if sql is None
        else SqlSearch(
            table=str(sql["table"]),
            columns={str(k): str(v) for k, v in sql["columns"].items()},
        ),
    )


def load_record_search(name: str) -> RecordSearch:
    """The record-search file of one app, by name."""

    if not _IDENT.fullmatch(name) or "." in name:
        raise ValueError("app name must be a plain identifier")
    path = SEARCH_DIR / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"no record-search file for {name!r}")
    return _parse(yaml.safe_load(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def _all_searches() -> tuple[RecordSearch, ...]:
    return tuple(
        _parse(yaml.safe_load(p.read_text(encoding="utf-8")))
        for p in sorted(SEARCH_DIR.glob("*.yaml"))
    )


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if value:
            return str(value)
    return ""


class SqlPatientSource:
    """An app's patient table, read with a SELECT-only account.

    For a deployment with no FHIR or REST read. ``connect`` returns a DB-API
    connection. Parameters default to ``%s`` (``paramstyle="format"``, MySQL
    and MariaDB); pass ``paramstyle="qmark"`` for SQLite. The table and
    columns come from the app's record-search file; values are bound, never
    concatenated.
    """

    channel = "sql"

    def __init__(
        self,
        connect: Callable[[], Any],
        search: SqlSearch,
        *,
        paramstyle: Literal["format", "qmark"] = "format",
    ) -> None:
        self._connect = connect
        self.search = search
        self.mark = "%s" if paramstyle == "format" else "?"
        self.pages_read = 0

    def _query(self, where: str, *values: str) -> list[PatientRecord]:
        c = self.search.columns
        try:
            conn = self._connect()
        except Exception as exc:  # noqa: BLE001
            raise Unreadable(f"the database is unreachable ({type(exc).__name__})")
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT " + ", ".join(c[f] for f in FIELDS) + f" FROM "
                f"{self.search.table} WHERE {where.replace('?', self.mark)}",
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
        for row in rows:
            v = dict(zip(FIELDS, row))
            bl = v["birth_lname"]
            prior = (
                ((str(v["birth_fname"] or v["fname"] or ""), str(bl)),) if bl else ()
            )
            out.append(
                PatientRecord(
                    str(v["record_id"]),
                    str(v["fname"] or ""),
                    str(v["lname"] or ""),
                    full_date(v["dob"]),
                    str(v["mrn"] or ""),
                    str(v["phone"] or ""),
                    str(v["street"] or ""),
                    str(v["email"] or ""),
                    prior,
                )
            )
        return out

    def by_birthdate(self, dob: str) -> list[PatientRecord]:
        return self._query(f"{self.search.columns['dob']} = ?", dob)

    def by_family(self, family: str) -> list[PatientRecord]:
        # FHIR's string search is a case-insensitive prefix; match it.
        c = self.search.columns
        prefix = family.replace("%", "").replace("_", "") + "%"
        return self._query(
            f"LOWER({c['lname']}) LIKE LOWER(?) OR LOWER({c['birth_lname']}) "
            "LIKE LOWER(?)",
            prefix,
            prefix,
        )

    def by_identifier(self, mrn: str) -> list[PatientRecord]:
        return self._query(f"{self.search.columns['mrn']} = ?", mrn)

    def by_given(self, given: str) -> list[PatientRecord]:
        return self._query(f"LOWER({self.search.columns['fname']}) = LOWER(?)", given)

    def by_phone(self, phone: str) -> list[PatientRecord]:
        return self._query(f"{self.search.columns['phone']} = ?", phone)

    def by_email(self, email: str) -> list[PatientRecord]:
        return self._query(f"LOWER({self.search.columns['email']}) = LOWER(?)", email)


class RestPatientSource:
    """An app's own REST patient search, as its record-search file declares it.

    The rows sit under ``rows_key``. A next-page link (``next_link``) is
    followed. A full page with no next link may be truncated, so it makes
    the answer unreadable rather than incomplete.
    """

    channel = "rest"

    def __init__(
        self,
        base_url: str,
        search: RestSearch,
        *,
        path: str,
        headers: Optional[dict[str, str]] = None,
        session: Any = None,
        timeout_s: float = 10.0,
        page_size: int = 500,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.spec = search
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

    def _record(self, row: dict[str, Any]) -> PatientRecord:
        f = self.spec.fields

        def get(name: str) -> str:
            return _first(row, f.get(name, ()))

        birth_lname = get("birth_lname")
        return PatientRecord(
            record_id=get("record_id"),
            fname=get("fname"),
            lname=get("lname"),
            dob=full_date(get("dob") or None),
            mrn=get("mrn"),
            phone=get("phone"),
            street=get("street"),
            email=get("email"),
            prior_names=((get("birth_fname"), birth_lname),) if birth_lname else (),
        )

    def search(self, params: dict[str, str]) -> list[PatientRecord]:
        url: Optional[str] = f"{self.base_url}{self.path}"
        first: dict[str, Any] = dict(params)
        if self.spec.page_size_param:
            first[self.spec.page_size_param] = self.page_size
        query: Optional[dict[str, Any]] = first
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
            errors = [k for k in self.spec.error_keys if body.get(k)]
            if errors:
                raise Unreadable("the patient search reported " + ", ".join(errors))
            rows = body.get(self.spec.rows_key)
            if not isinstance(rows, list):
                raise Unreadable("the patient search did not return a rows list")
            for row in rows:
                if not isinstance(row, dict):
                    raise Unreadable("a patient row is not an object")
                out.append(self._record(row))
            nxt: Any = body if self.spec.next_link else None
            for key in self.spec.next_link:
                nxt = nxt.get(key) if isinstance(nxt, dict) else None
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

    def _by(self, kind: str, value: str) -> list[PatientRecord]:
        return self.search({self.spec.params[kind]: value})

    def by_birthdate(self, dob: str) -> list[PatientRecord]:
        return self._by("birthdate", dob)

    def by_family(self, family: str) -> list[PatientRecord]:
        return self._by("family", family)

    def by_identifier(self, mrn: str) -> list[PatientRecord]:
        return self._by("identifier", mrn)

    def by_given(self, given: str) -> list[PatientRecord]:
        return self._by("given", given)

    def by_phone(self, phone: str) -> list[PatientRecord]:
        return self._by("phone", phone)

    def by_email(self, email: str) -> list[PatientRecord]:
        return self._by("email", email)


def source_for_rest_verifier(
    verifier: Any,
    *,
    allow_rest: bool = False,
    channel: Optional[Literal["fhir", "rest"]] = None,
) -> Optional[PatientSource]:
    """The patient search for an app's REST record check, or None.

    Only a verifier whose records path an app's record-search file names
    (``rest_records_path_marker``) qualifies. ``fhir`` searches the app's
    FHIR API beside its REST API (``fhir_beside_rest``) with the verifier's
    bearer token, which then needs the FHIR read scope. Otherwise, only with
    ``allow_rest``, the app's REST search.
    """

    from openadapt_flow.runtime.effects.rest import RestRecordVerifier

    if not isinstance(verifier, RestRecordVerifier):
        return None
    records_path = str(getattr(verifier, "records_path", "") or "")
    found = [
        s
        for s in _all_searches()
        if s.rest_records_path_marker and s.rest_records_path_marker in records_path
    ]
    if len(found) != 1:
        return None  # no app names this path, or more than one does
    app = found[0]
    timeout = max(float(getattr(verifier, "timeout_s", 10.0)), 10.0)
    if channel == "fhir":
        if not app.fhir_beside_rest:
            return None
        marker = str(app.rest_records_path_marker)
        site = records_path.split("?", 1)[0].split(marker, 1)[0]
        bearer = str(
            (getattr(verifier, "headers", None) or {}).get("Authorization", "")
        )
        token = bearer.removeprefix("Bearer ").strip()
        return FhirPatientSource(
            f"{verifier.base_url}{site}{app.fhir_beside_rest}",
            token=(lambda: token) if token else None,
            session=verifier._get_session(),
            timeout_s=timeout,
        )
    if allow_rest and app.rest is not None:
        return RestPatientSource(
            verifier.base_url,
            app.rest,
            path=records_path,
            headers=getattr(verifier, "headers", None),
            session=verifier._get_session(),
            timeout_s=timeout,
        )
    return None
