"""Scrub identifiers out of URLs and control labels before anything is stored.

A URL path segment or a button label can carry the very values shadow mining
must not keep: ``/patient-987654``, ``/u/jane-doe``, a menu item that reads
"Doe, John (12345)". Pattern rules alone (digits, hex, UUIDs) miss names, and
names can't be recognized reliably. So the rule here is an allowlist: a path
segment or label survives in plain text only when every word in it is a known
UI or URL word. Anything else becomes a placeholder:

* a URL path segment becomes ``<id>``;
* a short button or menu label becomes ``<dynamic_label>``.

Both happen in the tokenizer, before a symbol reaches the ring buffer.

This module also decides which browser activity is in scope. Only hosts on
the person's local work-domain allowlist keep a host and a path word; every
other site is reduced to one "non-work browser" marker.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

PLACEHOLDER_ID = "<id>"
PLACEHOLDER_LABEL = "<dynamic_label>"
NON_WORK_BROWSER = "non-work-browser"

#: Words a URL path segment may keep. Short, generic, and never a name.
URL_WORDS: frozenset[str] = frozenset(
    """
    about account accounts activity add admin alerts all analytics api app apps appointment
    appointments approve approvals archive assets auth billing blog board book booking browse
    calendar cart case cases catalog chart charts chat checkout claims client clients compose
    config contact contacts content create customer customers dashboard data default delete
    details docs document documents download edit editor encounter encounters events explore
    export feed file files finance form forms help history home inbox index insights invoice
    invoices item items labs list lists login logout mail manage member members message
    messages new notes notifications order orders overview page pages patient patients payment
    payments people plan plans portal post posts preferences prescription prescriptions profile
    project projects purchase queue record records referral referrals register report reports
    request requests review reviews schedule scheduling search send settings setup share shop
    sign signin signout signup start status store submit summary support task tasks team teams
    ticket tickets timeline tools upload user users vendor vendors view views wiki work
    workflow workflows workspace www
    """.split()
)

#: Words a short chrome label may keep. Generic UI verbs and nouns only.
LABEL_WORDS: frozenset[str] = frozenset(
    """
    a about accept account actions add all and apply approve archive as at attach attachment
    back bold cancel change chart check clear close compose confirm continue copy create cut
    date day delete deny details discard done download draft duplicate edit email export file
    filter find for format forward from help hide history home import in inbox insert invoice
    item list log mark menu message more move new next no note notes of off ok on open order
    options paste patient pause pdf preferences preview previous print profile record redo
    refresh reject remove rename reply report reset resume review run save search select send
    settings share show sign skip sort start stop subject submit summary tab table task to
    today tools undo update upload urgent user view week window with yes
    """.split()
)

_WORD_SPLIT = re.compile(r"[-_.+~]")
_LABEL_WORD = re.compile(r"[a-z]+")
_LABEL_ALLOWED = re.compile(r"^[A-Za-z][A-Za-z &/'-]*$")

#: Labels that mark a field whose value must be dropped, not digested.
_SECRET_LABEL = re.compile(
    r"pass(word|code|phrase)|passwd|\bpass\b|\bpwd\b|\bpin\b|\botp\b|one[- ]?time|"
    r"verification code|security code|auth(entication)? code|\bcvv\b|\bcvc\b|\bcsc\b|"
    r"\bssn\b|social security|\bsin\b|secret|api[- ]?key|access[- ]?token|\btoken\b|"
    r"\bmfa\b|\b2fa\b|\btotp\b|card number|credit card",
    re.IGNORECASE,
)


def scrub_path_segment(segment: str, extra_words: Iterable[str] = ()) -> str:
    """Return ``segment`` if every word in it is a known URL word, else ``<id>``.

    ``/patient-987654``, ``/jane-doe``, ``/3f9a0c1e``, and a UUID all become
    ``<id>``. ``/invoices`` and ``/new-order`` stay.
    """

    text = segment.strip()
    if not text or len(text) > 32 or text != text.casefold():
        return PLACEHOLDER_ID
    words = [w for w in _WORD_SPLIT.split(text) if w]
    if not words or len(words) > 3:
        return PLACEHOLDER_ID
    allowed = URL_WORDS | frozenset(extra_words)
    if all(w.isalpha() and w in allowed for w in words):
        return text
    return PLACEHOLDER_ID


def scrub_label(label: str | None, extra_words: Iterable[str] = ()) -> str | None:
    """Return a short chrome label in plain text, or ``<dynamic_label>``.

    A label survives only when it is at most four words and 32 characters, has
    only letters and simple punctuation, and every word is a known UI word.
    "Save" and "New Message" stay; "Doe, John (12345)", "Jane Doe", and
    "INV-559017" become ``<dynamic_label>``. ``None`` means no label.
    """

    if label is None:
        return None
    cleaned = " ".join(str(label).split())
    if not cleaned:
        return None
    if len(cleaned) > 32 or not _LABEL_ALLOWED.match(cleaned):
        return PLACEHOLDER_LABEL
    words = _LABEL_WORD.findall(cleaned.casefold())
    if not words or len(words) > 4:
        return PLACEHOLDER_LABEL
    allowed = LABEL_WORDS | frozenset(extra_words)
    if all(w in allowed for w in words):
        return cleaned
    return PLACEHOLDER_LABEL


def looks_secret(*texts: str | None) -> bool:
    """True when a label, selector, or id names a password-like field."""

    return any(t and _SECRET_LABEL.search(_words(str(t))) for t in texts)


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATORS = re.compile(r"[-_.#\[\]=:'\"/>]+")


def _words(text: str) -> str:
    """``txtPassword`` -> ``txt Password``; ``user-pass`` -> ``user pass``."""

    return _SEPARATORS.sub(" ", _CAMEL.sub(" ", text))


def allowlisted_domain(host: str | None, work_domains: Iterable[str]) -> str | None:
    """Return the allowlist entry that covers ``host``, or None."""

    if not host:
        return None
    host = host.casefold().rstrip(".")
    best = None
    for domain in work_domains:
        if host == domain or host.endswith("." + domain):
            if best is None or len(domain) > len(best):
                best = domain
    return best


def url_host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return (urlsplit(url).hostname or "").casefold() or None
    except ValueError:
        return None


def scrub_host(host: str, domain: str, extra_words: Iterable[str] = ()) -> str:
    """Keep the allowlisted domain; scrub each subdomain label above it."""

    if host == domain:
        return host
    prefix = host[: -(len(domain) + 1)]
    labels = [scrub_path_segment(part, extra_words) for part in prefix.split(".")]
    return ".".join(labels + [domain])
