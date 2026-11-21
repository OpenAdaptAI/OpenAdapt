"""tx.wire_tap.relay_redirect_location (C6): a relayed Location must keep a
browser on an allowed origin.

The corpus (corpus/urls.json) is read the way a browser reads it
(:func:`browser_origin`), not the way ``urlsplit`` does: on main
``///evil.example/x`` is relayed as written, and a browser goes to
``evil.example``. Fixed on #2470; the post-fix copy is in
``selftest/s10_wire_tap_relay.py``.
"""

from __future__ import annotations

from typing import Any, Callable

from tests.failopen.harness import CorpusCase, Seed, SiteSpec, browser_origin, register
from tests.failopen.mutators import load_corpus

BASE = "http://app.test/a/page"
ALLOWED = frozenset({"http://app.test"})


def _entries() -> list[Any]:
    urls = load_corpus("urls")
    return list(urls["hostile"]) + list(urls["chain"])


def _bad(ev: dict[str, Any], entry: Any, out: Any) -> bool:
    return out is not None and browser_origin(BASE, out) not in ALLOWED


def relay_spec(fn: Callable[..., Any], id: str, kind: str) -> SiteSpec:
    return SiteSpec(
        id=id,
        classes="C6",
        target="openadapt_flow.tx.wire_tap:relay_redirect_location",
        kind=kind,
        run=lambda ev: fn(ev["location"], request_url=BASE, allowed_origins=ALLOWED),
        is_pass=lambda out: out is not None,
        seeds=lambda: [
            Seed({"location": "/a/b"}, True),
            Seed({"location": "https://evil.example/"}, False),
        ],
        corpus=CorpusCase(
            "url", apply=lambda ev, entry: {**ev, "location": entry}, bad=_bad
        ),
        corpus_entries=_entries,
    )


def _main() -> None:
    from openadapt_flow.tx.wire_tap import relay_redirect_location

    register(
        relay_spec(
            relay_redirect_location, "wire_tap.relay_redirect_location", "owned"
        ),
        xfail="#2470: '///host' is relayed raw and a browser reads it as a host",
        known=[
            "M11 P3: accepted '///evil.example/x' as '///evil.example/x'",
            "M11 P3: accepted '////evil.example/x' as '////evil.example/x'",
            "M11 P3: accepted 'http:////evil.example/x' as 'http:////evil.example/x'",
        ],
    )


_main()
