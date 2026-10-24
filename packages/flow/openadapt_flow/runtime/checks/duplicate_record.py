"""Is this new patient already in the system of record? Asked of the record.

The screen check (``no_duplicate_candidate``) reads the app's possible-
duplicate list and stops on any row that shares the last name or the date of
birth. Every new patient who shares either with an existing chart stops for a
person, and that is most of the operator time in the production soak.

This check asks the system of record instead (FHIR ``Patient`` search, the
app's REST API, or, as a documented fallback, a read-only SQL query) and
decides from whole records:

- ``exact``: same first name, last name and date of birth, or the same MRN.
  Stop: the chart exists.
- ``close``: a record that may be this person. Stop for a person, with both
  records shown. Close is any of:
  - the same name with a date of birth one field off (a typo) or with day and
    month swapped;
  - the same date of birth with a similar first name and a similar or
    hyphen-sharing last name;
  - the same date of birth and the same first name, whatever the last name
    (a married or changed name).
- ``none``: no record is this person. Proceed.
- ``unreadable``: the channel was unreachable, refused the credentials,
  returned something that is not a result set, or could not be read to the
  last page. Stop (fail closed).

Sharing only a last name, or only a date of birth, is not a candidate: those
are the ordinary coincidences a practice sees.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Mapping, Optional, Protocol
from urllib.parse import urljoin

from openadapt_flow.runtime.checks import CheckSource

Decision = Literal["none", "exact", "close", "unreadable"]

#: A paging chain longer than this is read as unreadable, not truncated.
MAX_PAGES = 50


@dataclass(frozen=True)
class PatientRecord:
    """One chart as the record channel returns it."""

    record_id: str
    fname: str
    lname: str
    dob: str  # ISO YYYY-MM-DD, or "" when the record has none
    mrn: str = ""


@dataclass
class Candidate:
    record: PatientRecord
    match: Literal["exact", "close"]
    why: str


@dataclass
class DuplicateCheckResult:
    decision: Decision
    source: CheckSource = "record"
    channel: str = ""
    reason: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    #: Search requests made and pages read, for the receipt.
    searches: int = 0
    pages: int = 0

    @property
    def proceed(self) -> bool:
        return self.decision == "none"


class Unreadable(Exception):
    """The record channel could not give a complete answer."""


class PatientSource(Protocol):
    """A read-only patient search on the system of record."""

    channel: str

    def by_birthdate(self, dob: str) -> list[PatientRecord]: ...

    def by_family(self, family: str) -> list[PatientRecord]: ...

    def by_identifier(self, mrn: str) -> list[PatientRecord]: ...


# -- matching -------------------------------------------------------------------


def norm_name(value: str) -> str:
    """Case, accents, spaces and punctuation removed (hyphens kept)."""

    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"[^a-z\- ]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def name_parts(value: str) -> set[str]:
    return {p for p in re.split(r"[\s\-]+", norm_name(value)) if p}


def edit_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein (optimal string alignment)."""

    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def soundex(value: str) -> str:
    letters = re.sub(r"[^a-z]", "", norm_name(value))
    if not letters:
        return ""
    codes = {
        c: str(n)
        for n, group in enumerate(("bfpv", "cgjkqsxz", "dt", "l", "mn", "r"), start=1)
        for c in group
    }
    out, last = letters[0].upper(), codes.get(letters[0], "")
    for c in letters[1:]:
        code = codes.get(c, "")
        if code and code != last:
            out += code
        if c not in "hw":
            last = code
    return (out + "000")[:4]


def similar_name(a: str, b: str) -> bool:
    """Equal, one or two edits apart (by length), or the same Soundex code."""

    x, y = norm_name(a).replace("-", " "), norm_name(b).replace("-", " ")
    if not x or not y:
        return False
    if x == y:
        return True
    limit = 1 if min(len(x), len(y)) <= 5 else 2
    if edit_distance(x, y) <= limit:
        return True
    return len(x) > 3 and len(y) > 3 and soundex(x) == soundex(y)


def swapped(dob: str) -> Optional[str]:
    """The date with day and month swapped, when that is another valid date."""

    y, m, d = dob.split("-")
    if m == d or int(d) > 12:
        return None
    return f"{y}-{d}-{m}"


def one_field_off(a: str, b: str) -> bool:
    """Two ISO dates that differ in exactly one of year, month and day."""

    pa, pb = a.split("-"), b.split("-")
    return sum(x != y for x, y in zip(pa, pb)) == 1


def classify(want: Mapping[str, str], record: PatientRecord) -> Optional[Candidate]:
    """Is ``record`` this new patient (``want``: fname, lname, DOB, mrn)?"""

    mrn = (want.get("mrn") or "").strip()
    if mrn and record.mrn and mrn == record.mrn.strip():
        return Candidate(record, "exact", "same MRN")
    fname, lname, dob = want["fname"], want["lname"], want["DOB"]
    same_first = norm_name(fname) == norm_name(record.fname)
    same_last = norm_name(lname) == norm_name(record.lname)
    if same_first and same_last and dob == record.dob:
        return Candidate(
            record, "exact", "same first name, last name and date of birth"
        )
    first_like = similar_name(fname, record.fname)
    last_like = similar_name(lname, record.lname) or bool(
        name_parts(lname) & name_parts(record.lname)
    )
    if first_like and last_like:
        if not record.dob:
            return Candidate(
                record, "close", "same name; the record has no date of birth"
            )
        if record.dob == dob:
            return Candidate(record, "close", "same date of birth, a similar name")
        if record.dob == swapped(dob):
            return Candidate(record, "close", "same name, day and month swapped")
        if same_first and same_last and one_field_off(dob, record.dob):
            return Candidate(record, "close", "same name, date of birth one field off")
    if record.dob == dob and same_first:
        return Candidate(
            record, "close", "same date of birth and first name, another last name"
        )
    return None


ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def check_duplicate(
    source: PatientSource, want: Mapping[str, str]
) -> DuplicateCheckResult:
    """Ask ``source`` whether the patient in ``want`` already has a chart."""

    result = DuplicateCheckResult("unreadable", channel=source.channel)
    fname, lname = (want.get("fname") or "").strip(), (want.get("lname") or "").strip()
    dob = (want.get("DOB") or "").strip()
    if not fname or not lname or not ISO_DATE.fullmatch(dob):
        result.reason = (
            "the run's first name, last name or ISO date of birth is missing"
        )
        return result
    queries: list[tuple[Callable[[str], list[PatientRecord]], str]] = [
        (source.by_birthdate, dob)
    ]
    if swapped(dob):
        queries.append((source.by_birthdate, swapped(dob)))  # type: ignore[arg-type]
    queries += [(source.by_family, part) for part in sorted(name_parts(lname))]
    if (want.get("mrn") or "").strip():
        queries.append((source.by_identifier, want["mrn"].strip()))
    seen: dict[str, PatientRecord] = {}
    try:
        for query, value in queries:
            for record in query(value):
                seen.setdefault(record.record_id, record)
            result.searches += 1
    except Unreadable as exc:
        result.reason = str(exc)
        return result
    result.pages = getattr(source, "pages_read", 0)
    found = [c for c in (classify(want, r) for r in seen.values()) if c is not None]
    result.candidates = sorted(
        found, key=lambda c: (c.match != "exact", c.record.record_id)
    )
    if any(c.match == "exact" for c in found):
        result.decision = "exact"
        result.reason = "a chart for this patient exists"
    elif found:
        result.decision = "close"
        result.reason = "a chart may be this patient; a person decides"
    else:
        result.decision = "none"
        result.reason = f"no chart matches among {len(seen)} record(s) read"
    return result


# -- channels -------------------------------------------------------------------


def _get_json(
    session: Any, url: str, params: Optional[dict], headers: dict, timeout_s: float
) -> tuple[Any, str]:
    try:
        resp = session.get(url, params=params, headers=headers, timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - transport failure is unreadable
        raise Unreadable(f"the record channel is unreachable ({type(exc).__name__})")
    if resp.status_code in (401, 403):
        raise Unreadable(
            f"the record channel refused the credentials (HTTP {resp.status_code})"
        )
    if resp.status_code // 100 != 2:
        raise Unreadable(f"the record channel returned HTTP {resp.status_code}")
    try:
        return resp.json(), str(getattr(resp, "url", url))
    except Exception:  # noqa: BLE001 - unparseable body is unreadable
        raise Unreadable("the record channel returned a body that is not JSON")


class FhirPatientSource:
    """FHIR R4 ``Patient`` search, every page (``Bundle.link[next]``).

    A page chain longer than :data:`MAX_PAGES`, a ``Bundle.total`` that does
    not equal the entries read, or an entry that is not a ``Patient`` makes
    the answer unreadable.
    """

    channel = "fhir"

    def __init__(
        self,
        base_url: str,
        *,
        token: Optional[Callable[[], Optional[str]]] = None,
        session: Any = None,
        timeout_s: float = 10.0,
        verify_tls: bool = True,
        mrn_system: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._session = session
        self.timeout_s = timeout_s
        self.verify_tls = verify_tls
        self.mrn_system = mrn_system
        self.pages_read = 0

    def _client(self) -> Any:
        if self._session is None:
            import httpx

            self._session = httpx.Client(verify=self.verify_tls)
        return self._session

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/fhir+json"}
        if self._token is not None:
            token = self._token()
            if not token:
                raise Unreadable("no access token for the record channel")
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def search(self, params: dict[str, str]) -> list[PatientRecord]:
        url: Optional[str] = f"{self.base_url}/Patient"
        query: Optional[dict[str, str]] = params
        total: Optional[int] = None
        out: list[PatientRecord] = []
        pages = 0
        while url is not None:
            if pages >= MAX_PAGES:
                raise Unreadable(
                    f"more than {MAX_PAGES} pages; the search was not read to the end"
                )
            body, at = _get_json(
                self._client(), url, query, self._headers(), self.timeout_s
            )
            pages += 1
            self.pages_read += 1
            if not isinstance(body, dict) or body.get("resourceType") != "Bundle":
                raise Unreadable("the record channel did not return a FHIR Bundle")
            if total is None and isinstance(body.get("total"), int):
                total = body["total"]
            entries = body.get("entry", [])
            if not isinstance(entries, list):
                raise Unreadable("the Bundle's entries are not a list")
            for entry in entries:
                resource = entry.get("resource") if isinstance(entry, dict) else None
                if (
                    not isinstance(resource, dict)
                    or resource.get("resourceType") != "Patient"
                ):
                    raise Unreadable("a Bundle entry is not a Patient")
                out.append(self._record(resource))
            url, query = None, None
            for link in body.get("link", []) or []:
                if isinstance(link, dict) and link.get("relation") == "next":
                    raw = link.get("url")
                    if not isinstance(raw, str) or not raw.strip():
                        raise Unreadable("a next-page link has no URL")
                    url = urljoin(at, raw.strip())
        if total is not None and total != len(out):
            raise Unreadable(
                f"the search reported {total} record(s) and {len(out)} were read"
            )
        return out

    def _record(self, resource: dict[str, Any]) -> PatientRecord:
        names = resource.get("name") or []
        official = next(
            (n for n in names if isinstance(n, dict) and n.get("use") == "official"),
            names[0] if names and isinstance(names[0], dict) else {},
        )
        given = official.get("given") or []
        mrn = ""
        for ident in resource.get("identifier") or []:
            if not isinstance(ident, dict):
                continue
            codes = [
                c.get("code") for c in (ident.get("type") or {}).get("coding", []) or []
            ]
            if (self.mrn_system and ident.get("system") == self.mrn_system) or (
                not self.mrn_system and ("MR" in codes or "PT" in codes)
            ):
                mrn = str(ident.get("value") or "")
                break
        return PatientRecord(
            record_id=str(resource.get("id") or ""),
            fname=str(given[0]) if given else "",
            lname=str(official.get("family") or ""),
            dob=str(resource.get("birthDate") or ""),
            mrn=mrn,
        )

    def by_birthdate(self, dob: str) -> list[PatientRecord]:
        return self.search({"birthdate": dob})

    def by_family(self, family: str) -> list[PatientRecord]:
        return self.search({"family": family})

    def by_identifier(self, mrn: str) -> list[PatientRecord]:
        return self.search({"identifier": mrn})


class SqlPatientSource:
    """Fallback: a read-only query on OpenEMR's ``patient_data``.

    For a deployment with no FHIR or REST read. ``connect`` returns a DB-API
    connection with a SELECT-only account. Uses ``qmark`` parameters.
    """

    channel = "sql"

    def __init__(
        self, connect: Callable[[], Any], *, table: str = "patient_data"
    ) -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,63}", table):
            raise ValueError("table must be a plain identifier")
        self._connect = connect
        self.table = table
        self.pages_read = 0

    def _query(self, where: str, value: str) -> list[PatientRecord]:
        try:
            conn = self._connect()
        except Exception as exc:  # noqa: BLE001
            raise Unreadable(f"the database is unreachable ({type(exc).__name__})")
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT pid, fname, lname, DOB, pubpid FROM {self.table} WHERE {where}",
                (value,),
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
        return [
            PatientRecord(
                str(pid),
                str(f or ""),
                str(lname or ""),
                str(d or "")[:10],
                str(mrn or ""),
            )
            for pid, f, lname, d, mrn in rows
        ]

    def by_birthdate(self, dob: str) -> list[PatientRecord]:
        return self._query("DOB = ?", dob)

    def by_family(self, family: str) -> list[PatientRecord]:
        # FHIR's string search is a case-insensitive prefix; match it.
        return self._query("LOWER(lname) LIKE LOWER(?) || '%'", family)

    def by_identifier(self, mrn: str) -> list[PatientRecord]:
        return self._query("pubpid = ?", mrn)


class TableSource:
    """An in-memory patient table with FHIR search semantics (offline use)."""

    channel = "table"

    def __init__(self, records: Iterable[PatientRecord]) -> None:
        self.records = list(records)
        self.pages_read = 0

    def by_birthdate(self, dob: str) -> list[PatientRecord]:
        self.pages_read += 1
        return [r for r in self.records if r.dob == dob]

    def by_family(self, family: str) -> list[PatientRecord]:
        self.pages_read += 1
        prefix = norm_name(family)
        return [r for r in self.records if norm_name(r.lname).startswith(prefix)]

    def by_identifier(self, mrn: str) -> list[PatientRecord]:
        self.pages_read += 1
        return [r for r in self.records if r.mrn and r.mrn == mrn]


class RestPatientSource:
    """OpenEMR's standard REST API: ``GET .../api/patient?DOB=...``.

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
            rows = body.get("data") if isinstance(body, dict) else None
            if (
                rows is None
                and isinstance(body, dict)
                and not body.get("validationErrors")
            ):
                rows = []  # OpenEMR returns no "data" for an empty search
            if not isinstance(rows, list):
                raise Unreadable("the patient search did not return a list")
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
                        dob=str(row.get("DOB") or "")[:10],
                        mrn=str(row.get("pubpid") or ""),
                    )
                )
            links = body.get("links") if isinstance(body, dict) else None
            nxt = links.get("next") if isinstance(links, dict) else None
            if isinstance(nxt, str) and nxt.strip():
                url, query = urljoin(at, nxt.strip()), None
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


def source_for_verifier(verifier: Any) -> Optional[PatientSource]:
    """The patient search on the same channel as a deployment's record check.

    A FHIR verifier gives a FHIR ``Patient`` search at its base URL with its
    credentials; an OpenEMR REST verifier on ``.../api/patient`` gives the
    REST search. Anything else gives None, and the screen check stays.
    """

    from openadapt_flow.runtime.effects import FhirEffectVerifier
    from openadapt_flow.runtime.effects.rest import RestRecordVerifier

    if isinstance(verifier, FhirEffectVerifier):

        def token() -> Optional[str]:
            if verifier.token_source is not None:
                return verifier.token_source.access_token()
            return verifier.access_token

        return FhirPatientSource(
            verifier.base_url,
            token=token if (verifier.access_token or verifier.token_source) else None,
            verify_tls=verifier.verify_tls,
            timeout_s=max(verifier.timeout_s, 10.0),
        )
    if isinstance(verifier, RestRecordVerifier) and "/api/patient" in (
        verifier.records_path or ""
    ):
        return RestPatientSource(
            verifier.base_url,
            path=verifier.records_path,
            headers=verifier.headers,
            session=verifier._get_session(),
            timeout_s=max(verifier.timeout_s, 10.0),
        )
    return None
