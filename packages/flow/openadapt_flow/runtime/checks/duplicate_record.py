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
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Callable, Iterable, Literal, Mapping, Optional, Protocol
from urllib.parse import urljoin, urlsplit

from openadapt_flow.runtime.checks import CheckSource

Decision = Literal["none", "exact", "close", "unreadable"]

#: The fields the check reads, and the run parameter that fills each one by
#: default: a parameter of the same name. A program maps its own names with
#: ``candidate_record_fields``; with no mapping, a missing first name, last
#: name or date of birth makes the check unreadable.
RECORD_FIELDS: dict[str, str] = {
    "fname": "fname",
    "lname": "lname",
    "DOB": "DOB",
    "mrn": "mrn",
    "phone_home": "phone_home",
    "email": "email",
    "street": "street",
}

#: A paging chain longer than this is read as unreadable, not truncated.
MAX_PAGES = 50
# Rows asked for per FHIR page (``_count``).
FHIR_PAGE_SIZE = 200


@dataclass(frozen=True)
class PatientRecord:
    """One chart as the record channel returns it."""

    record_id: str
    fname: str
    lname: str
    dob: str  # ISO YYYY-MM-DD, or "" when the record has none
    mrn: str = ""
    phone: str = ""
    street: str = ""
    email: str = ""
    #: Earlier names, (first, last): a birth or maiden name, a previous name.
    prior_names: tuple[tuple[str, str], ...] = ()


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

    def by_given(self, given: str) -> list[PatientRecord]: ...

    def by_phone(self, phone: str) -> list[PatientRecord]: ...

    def by_email(self, email: str) -> list[PatientRecord]: ...


# -- matching -------------------------------------------------------------------


def norm_name(value: str) -> str:
    """Case, accents, spaces and punctuation removed (hyphens kept)."""

    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"[^a-z\- ]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def name_parts(value: str) -> set[str]:
    return {p for p in re.split(r"[\s\-]+", norm_name(value)) if p}


def raw_name_terms(*names: str) -> set[str]:
    """Each name whole and each of its parts, as typed (apostrophes, accents
    and case kept), for channels that search the stored text literally."""

    terms: set[str] = set()
    for name in names:
        name = re.sub(r"\s+", " ", name or "").strip()
        if name:
            terms.add(name)
            terms |= {p for p in re.split(r"[\s\-]+", name) if p}
    return terms


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


#: Common English given-name equivalents. Generic, not drawn from any test
#: workload; a deployment can extend it (``NICKNAMES.update``).
_NICKNAME_GROUPS = (
    "robert bob bobby rob robbie",
    "william bill billy will willie liam",
    "elizabeth liz beth betty eliza lizzie libby",
    "katherine kate kathy katie kat",
    "catherine cathy cate kate",
    "margaret maggie meg peggy marge greta",
    "james jim jimmy jamie",
    "john jack johnny jon",
    "richard rick ricky dick rich",
    "michael mike mikey mick",
    "thomas tom tommy",
    "joseph joe joey",
    "charles charlie chuck chas",
    "edward ed eddie ted ned",
    "anthony tony",
    "daniel dan danny",
    "christopher chris kit",
    "alexander alex sasha xander",
    "alexandra alex sandra sasha lexi",
    "patricia pat patty trish tricia",
    "jennifer jen jenny",
    "rebecca becky becca",
    "susan sue susie suzy",
    "deborah deb debbie",
    "nicholas nick nicky",
    "benjamin ben benny",
    "samuel sam sammy",
    "samantha sam sammie",
    "matthew matt matty",
    "andrew andy drew",
    "stephen steve stevie",
    "steven steve stevie",
    "jonathan jon jonny",
    "timothy tim timmy",
    "theodore theo ted teddy",
    "dorothy dot dottie dolly",
    "victoria vicky tori",
    "abigail abby gail",
    "gregory greg",
    "kenneth ken kenny",
    "ronald ron ronnie",
    "donald don donnie",
    "lawrence larry laurie",
    "frederick fred freddie",
    "isabella bella izzy isabel",
    "jacqueline jackie",
    "josephine jo josie",
    "eleanor ellie nora nell",
    "francis frank frankie",
    "frances fran frankie",
    "henry hank harry hal",
    "harold harry hal",
    "raymond ray",
    "rafael rafa",
    "dmitri dima",
    "anastasia ana stacy",
    "ana anna annie",
    "ines ine",
    "nadia nadya",
    "mateo mat teo",
)
NICKNAMES: dict[str, set[str]] = {}
for _group in _NICKNAME_GROUPS:
    _names = set(_group.split())
    for _name in _names:
        NICKNAMES.setdefault(_name, set()).update(_names)


def same_given(a: str, b: str) -> bool:
    """The same first name: similar spelling, an initial, or a known nickname."""

    if similar_name(a, b):
        return True
    x, y = norm_name(a), norm_name(b)
    # An initial ("J." for James) stands for any name with that first letter.
    if x and y and (len(x) == 1 or len(y) == 1) and x[0] == y[0]:
        return True
    return bool(x and y and y in NICKNAMES.get(x, ()))


def dob_near(a: str, b: str) -> bool:
    """A mistyped date of birth: one field off, day and month swapped, or two
    digits of one field transposed (1984 and 1948)."""

    if not a or not b or a == b:
        return False
    return one_field_off(a, b) or swapped(a) == b


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _street(value: str) -> str:
    return re.sub(
        r"\s+", " ", re.sub(r"[^0-9a-z ]", "", (value or "").casefold())
    ).strip()


def contacts_shared(want: Mapping[str, str], record: PatientRecord) -> list[str]:
    """Every contact detail the referral and the chart share."""

    out = []
    phone = _digits(want.get("phone_home") or "")
    if len(phone) >= 7 and phone == _digits(record.phone):
        out.append("same phone")
    email = (want.get("email") or "").strip().casefold()
    if email and email == record.email.strip().casefold():
        out.append("same email")
    street = _street(want.get("street") or "")
    if street and street == _street(record.street):
        out.append("same street address")
    return out


def tie_breaker(want: Mapping[str, str], record: PatientRecord) -> Optional[str]:
    """A contact detail the referral and the chart share, if any."""

    phone = _digits(want.get("phone_home") or "")
    if len(phone) >= 7 and phone == _digits(record.phone):
        return "same phone"
    email = (want.get("email") or "").strip().casefold()
    if email and email == record.email.strip().casefold():
        return "same email"
    street = _street(want.get("street") or "")
    if street and street == _street(record.street):
        return "same street address"
    return None


def classify(want: Mapping[str, str], record: PatientRecord) -> Optional[Candidate]:
    """Is ``record`` this new patient?

    ``want``: fname, lname, DOB, and when known mrn, phone_home, email,
    street. The record's prior names are tried as well as its current one.
    """

    if record.dob and full_date(record.dob) != record.dob:
        # Whatever source built it: a date that is no full real date is no
        # date, so a name match stays close.
        record = replace(record, dob="")
    mrn = (want.get("mrn") or "").strip()
    if mrn and record.mrn and mrn == record.mrn.strip():
        return Candidate(record, "exact", "same MRN")
    names = [(record.fname, record.lname), *record.prior_names]
    for i, (first, last) in enumerate(names):
        found = _classify_name(want, record, first, last)
        if found is not None:
            if i:
                found.why += " (a prior name on the chart)"
                if found.match == "exact":
                    found.match = "close"
            return found
    # A full name change: only the contact details tie the two together.
    shared = contacts_shared(want, record)
    if (
        shared
        and record.dob
        and (
            record.dob == want["DOB"]
            or (len(shared) >= 2 and dob_near(want["DOB"], record.dob))
        )
    ):
        return Candidate(
            record,
            "close",
            f"another name; {', '.join(shared)}; "
            + ("the same" if record.dob == want["DOB"] else "a near")
            + " date of birth",
        )
    return None


def _classify_name(
    want: Mapping[str, str], record: PatientRecord, first: str, last: str
) -> Optional[Candidate]:
    fname, lname, dob = want["fname"], want["lname"], want["DOB"]
    same_first = norm_name(fname) == norm_name(first)
    same_last = norm_name(lname) == norm_name(last)
    if same_first and same_last and dob == record.dob:
        return Candidate(
            record, "exact", "same first name, last name and date of birth"
        )
    first_like = same_given(fname, first)
    last_like = similar_name(lname, last) or bool(name_parts(lname) & name_parts(last))
    near = dob_near(dob, record.dob)
    if first_like and last_like:
        if not record.dob:
            return Candidate(
                record, "close", "same name; the record has no date of birth"
            )
        if record.dob == dob:
            return Candidate(record, "close", "same date of birth, a similar name")
        if record.dob == swapped(dob):
            return Candidate(record, "close", "same name, day and month swapped")
        # A mistyped date of birth with a name that differs in at most one
        # part (a nickname, an initial or a typo in the other), or with a
        # shared contact detail.
        if near and (same_first or same_last or tie_breaker(want, record)):
            return Candidate(
                record, "close", "same or similar name, a mistyped date of birth"
            )
    swapped_names = norm_name(fname) == norm_name(last) and norm_name(
        lname
    ) == norm_name(first)
    if swapped_names and (record.dob == dob or near):
        return Candidate(record, "close", "first and last name swapped")
    if record.dob == dob and first_like:
        return Candidate(
            record, "close", "same date of birth and first name, another last name"
        )
    if near and first_like and tie_breaker(want, record):
        return Candidate(
            record,
            "close",
            f"same first name, another last name, {tie_breaker(want, record)}, "
            "a mistyped date of birth",
        )
    return None


ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def full_date(value: Any) -> str:
    """A record's date of birth as full ISO, or "" when it has none, only a
    year or a year and month, or no real date (MySQL's ``0000-00-00``,
    ``1980-02-30``). Such a record is judged as one with no date of birth,
    so a name match is close, never a non-match."""

    text = str(value or "").strip()[:10]
    if not ISO_DATE.fullmatch(text):
        return ""
    try:
        date.fromisoformat(text)
    except ValueError:
        return ""
    return text


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
    # Each part of the last name, and of the first name (a swapped name),
    # both as typed and normalized: a channel compares the stored text, so
    # "obrien" alone never finds "O'Brien", nor "jose" "José".
    queries += [
        (source.by_family, part)
        for part in sorted(
            name_parts(lname) | name_parts(fname) | raw_name_terms(lname, fname)
        )
    ]
    queries += [
        (source.by_given, given) for given in sorted({fname, norm_name(fname)} - {""})
    ]
    if (want.get("mrn") or "").strip():
        queries.append((source.by_identifier, want["mrn"].strip()))
    if len(_digits(want.get("phone_home") or "")) >= 7:
        queries.append((source.by_phone, want["phone_home"].strip()))
    if (want.get("email") or "").strip():
        queries.append((source.by_email, want["email"].strip()))
    seen: dict[str, PatientRecord] = {}
    try:
        for query, value in queries:
            for record in query(value):
                if not record.record_id.strip():
                    raise Unreadable("a record came back with no id")
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


def _same_origin(base: str, url: str) -> str:
    """``url`` if it is on ``base``'s scheme, host and port, else unreadable:
    a next-page link to another host must not receive the credentials."""

    a, b = urlsplit(base), urlsplit(url)
    if (a.scheme, a.netloc) != (b.scheme, b.netloc):
        raise Unreadable("a next-page link points to another host")
    return url


class FhirPatientSource:
    """Generic FHIR R4: ``Patient`` search, every page (``Bundle.link[next]``).

    Uses only standard R4 search parameters (``birthdate``, ``family``,
    ``given``, ``identifier``, ``phone``, ``email``), so it serves any R4
    server, OpenEMR's included.

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
        # An explicit page size, so a full page with no next link shows the
        # server cut the answer short (OpenEMR's FHIR gives no next link,
        # and a Bundle.total equal to the rows it returned).
        query: Optional[dict[str, str]] = {**params, "_count": str(FHIR_PAGE_SIZE)}
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
            full_page = len(entries) >= FHIR_PAGE_SIZE
            url, query = None, None
            for link in body.get("link", []) or []:
                if isinstance(link, dict) and link.get("relation") == "next":
                    raw = link.get("url")
                    if not isinstance(raw, str) or not raw.strip():
                        raise Unreadable("a next-page link has no URL")
                    url = _same_origin(self.base_url, urljoin(at, raw.strip()))
            if url is None and full_page:
                raise Unreadable(
                    "a full page came back with no next link; the server may "
                    "have cut the answer short"
                )
        if total is not None and total != len(out):
            raise Unreadable(
                f"the search reported {total} record(s) and {len(out)} were read"
            )
        return out

    def _record(self, resource: dict[str, Any]) -> PatientRecord:
        names = [n for n in resource.get("name") or [] if isinstance(n, dict)]
        current = [n for n in names if n.get("use") not in ("old", "maiden")]
        official = next(
            (n for n in current if n.get("use") == "official"),
            current[0] if current else {},
        )

        def pair(n: dict[str, Any]) -> tuple[str, str]:
            given = n.get("given") or []
            return (str(given[0]) if given else "", str(n.get("family") or ""))

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
        telecom = [t for t in resource.get("telecom") or [] if isinstance(t, dict)]
        phone = next(
            (
                str(t.get("value") or "")
                for t in telecom
                if t.get("system") == "phone" and t.get("use") in (None, "home")
            ),
            "",
        )
        email = next(
            (str(t.get("value") or "") for t in telecom if t.get("system") == "email"),
            "",
        )
        address = next(
            (a for a in resource.get("address") or [] if isinstance(a, dict)), {}
        )
        lines = address.get("line") or []
        first, last = pair(official)
        prior = tuple(
            pair(n) for n in names if n is not official and pair(n) != (first, last)
        )
        return PatientRecord(
            record_id=str(resource.get("id") or ""),
            fname=first,
            lname=last,
            dob=full_date(resource.get("birthDate")),
            mrn=mrn,
            phone=phone,
            street=str(lines[0]) if lines else "",
            email=email,
            prior_names=prior,
        )

    def by_birthdate(self, dob: str) -> list[PatientRecord]:
        return self.search({"birthdate": dob})

    def by_family(self, family: str) -> list[PatientRecord]:
        return self.search({"family": family})

    def by_identifier(self, mrn: str) -> list[PatientRecord]:
        return self.search({"identifier": mrn})

    def by_given(self, given: str) -> list[PatientRecord]:
        return self.search({"given": given})

    def by_phone(self, phone: str) -> list[PatientRecord]:
        return self.search({"phone": phone})

    def by_email(self, email: str) -> list[PatientRecord]:
        return self.search({"email": email})


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
        # As a server compares: the stored text, case aside, by prefix.
        prefix = family.casefold()
        return [
            r
            for r in self.records
            if any(
                last.casefold().startswith(prefix)
                for last in (r.lname, *(n[1] for n in r.prior_names))
            )
        ]

    def by_identifier(self, mrn: str) -> list[PatientRecord]:
        self.pages_read += 1
        return [r for r in self.records if r.mrn and r.mrn == mrn]

    def by_given(self, given: str) -> list[PatientRecord]:
        self.pages_read += 1
        prefix = given.casefold()
        return [r for r in self.records if r.fname.casefold().startswith(prefix)]

    def by_phone(self, phone: str) -> list[PatientRecord]:
        self.pages_read += 1
        return [
            r for r in self.records if r.phone and _digits(r.phone) == _digits(phone)
        ]

    def by_email(self, email: str) -> list[PatientRecord]:
        self.pages_read += 1
        return [r for r in self.records if r.email.casefold() == email.casefold()]


def _unwrap(verifier: Any) -> Any:
    """The record verifier inside a known wrapper, else ``verifier``."""

    seen = 0
    while verifier is not None and seen < 4:
        inner = None
        for name in ("inner", "_inner", "wrapped", "_wrapped", "verifier", "_verifier"):
            candidate = getattr(verifier, name, None)
            if candidate is not None and candidate is not verifier:
                inner = candidate
                break
        if inner is None:
            return verifier
        verifier, seen = inner, seen + 1
    return verifier


def source_for_verifier(
    verifier: Any,
    *,
    allow_rest: bool = False,
    channel: Optional[Literal["fhir", "rest"]] = None,
) -> Optional[PatientSource]:
    """The patient search on the same channel as a run's record check.

    A FHIR verifier gives a generic FHIR R4 ``Patient`` search at its base URL
    with its credentials. A REST record verifier goes to the app's adapter
    (:mod:`openadapt_flow.runtime.checks.openemr_patient` is the only one),
    which uses its REST channel only with ``allow_rest`` (a program pinned to
    the record check). Anything else, or a verifier missing the attributes
    these need, gives None. ``channel`` (the program's ``candidate_channel``)
    chooses ``fhir`` or ``rest``.
    """

    from openadapt_flow.runtime.effects import FhirEffectVerifier
    from openadapt_flow.runtime.effects.rest import RestRecordVerifier

    verifier = _unwrap(verifier)
    try:
        if channel == "rest" and not isinstance(verifier, RestRecordVerifier):
            return None
        if isinstance(verifier, FhirEffectVerifier):
            access_token = getattr(verifier, "access_token", None)
            token_source = getattr(verifier, "token_source", None)

            def token() -> Optional[str]:
                if token_source is not None:
                    return token_source.access_token()
                return access_token

            return FhirPatientSource(
                verifier.base_url,
                token=token if (access_token or token_source) else None,
                verify_tls=getattr(verifier, "verify_tls", True),
                timeout_s=max(float(getattr(verifier, "timeout_s", 10.0)), 10.0),
            )
        if isinstance(verifier, RestRecordVerifier):
            # An app's REST record check: its adapter decides, if it has one.
            from openadapt_flow.runtime.checks.openemr_patient import (
                source_for_rest_verifier,
            )

            return source_for_rest_verifier(
                verifier, allow_rest=allow_rest, channel=channel
            )
    except Exception:  # noqa: BLE001 - an unusable verifier gives no source
        return None
    return None


#: ``Replayer(duplicate_record_source=...)``: derive the source from the run's
#: own effect verifier (the default). Only tests and embedders pass a source.
AUTO = "auto"


def resolve_duplicate_source(
    effect_verifier: Any,
    requested: Any = AUTO,
    *,
    pinned_record: bool = False,
    channel: Optional[Literal["fhir", "rest"]] = None,
) -> Any:
    """The one place a run's duplicate-check source is decided.

    Every ``Replayer`` resolves its source here from the effect verifier it is
    given, so qualification, a run, a resume and a proof that read the same
    system of record use the same check. With no pin, only a FHIR channel is
    used; a program pinned to the record check (``candidate_source:
    record``) also accepts OpenEMR's REST channel. ``requested`` is ``AUTO``
    or a source object (tests, embedding).
    """

    if requested is None:
        return None
    if requested != AUTO:
        return requested
    if effect_verifier is None:
        return None
    return source_for_verifier(
        effect_verifier, allow_rest=pinned_record, channel=channel
    )
