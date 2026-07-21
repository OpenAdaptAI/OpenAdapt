"""Executable capability matrix: which OPTIONAL protocols each backend exposes.

The runtime touches a GUI only through :class:`openadapt_flow.backend.Backend`,
but several *optional* capabilities are advertised structurally -- the resolver
and identity ladder call ``isinstance(backend, StructuralActionBackend)`` /
``IdentityBackend`` / ``SystemOfRecordBackend`` and fall through UNCHANGED when a
substrate does not implement them (``docs/LIMITS.md``, ``backend.py`` protocol
docstrings). Because that conformance is what decides whether a backend gets a
deterministic structural rung, a structured-text identity check, or an
independent system-of-record effect oracle, it is a load-bearing part of each
backend's *maturity*, and the published maturity map
(``docs/PRODUCT_STATUS.md`` / ``docs/LIMITS.md``) is only honest if it matches
the code.

This test pins that map in executable form. Each backend's exposed optional
protocols are asserted against an EXPECTED matrix; a drift in either direction
fails loudly:

* Adding a capability to a backend (e.g. teaching macOS AX identity) without
  updating this matrix -- and, by the review it forces, ``PRODUCT_STATUS.md`` --
  fails.
* Silently DROPPING a capability (a refactor that stops a backend implementing a
  protocol the runtime relies on) fails.

It also guards two specific honesty facts the audit surfaced:

* No backend advertises :class:`SystemOfRecordBackend`. System-of-record effect
  verification is provided by a configured external verifier
  (``deployment.build_effect_verifier``), never fabricated by a backend claiming
  to read the app's authoritative store.
* The pixel-only substrates (RDP network + local remote-display/Citrix analog)
  advertise NO structured/identity capability, so identity honestly falls
  through to the OCR ladder rather than a structured check that substrate cannot
  back. The four backends that DO own a structured layer -- the browser DOM,
  Windows UIA, Linux AT-SPI, and native macOS AX -- each expose both the
  structured-text identity read and the deterministic structural action rung.

Membership is derived from each protocol's own declared members (via the
supported ``get_protocol_members`` accessor) rather than hard-coded, so
extending a protocol's surface is reflected automatically and a partial
(some-but-not-all-members) implementation cannot masquerade as full conformance.
"""

from __future__ import annotations

try:  # Python 3.13+ exposes the accessor in the stdlib.
    from typing import get_protocol_members
except ImportError:  # < 3.13: the backport (always installed via pydantic et al.).
    from typing_extensions import get_protocol_members

from openadapt_flow.backend import (
    Backend,
    IdentityBackend,
    StructuralActionBackend,
    StructuralBackend,
    SystemOfRecordBackend,
)
from openadapt_flow.backends.linux_backend import LinuxBackend
from openadapt_flow.backends.macos_backend import MacOSBackend
from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.backends.rdp_backend import FreeRDPBackend
from openadapt_flow.backends.remote_display import RemoteDisplayBackend
from openadapt_flow.backends.windows_backend import WindowsBackend

# The optional capabilities a backend MAY expose, keyed by a short label.
OPTIONAL_PROTOCOLS = {
    "structural": StructuralBackend,  # url / title / page-count observations
    "identity": IdentityBackend,  # structured (DOM/a11y) text under a point
    "structural_action": StructuralActionBackend,  # deterministic element re-find
    "system_of_record": SystemOfRecordBackend,  # authoritative-store effect oracle
}

# Expected conformance per backend KIND, ordered browser (reference) -> native
# -> pixel-only. This is the executable twin of the maturity map in
# docs/PRODUCT_STATUS.md; keep the two in lockstep.
#
#   web    : the browser reference bar -- DOM gives structural observations,
#            structured identity text, and a deterministic structural rung.
#   windows: WAA/UIA exposes structured identity text + a UIA structural rung;
#            it has no cheap url/title/page-count equivalent (Experimental).
#   linux  : AT-SPI exposes structured identity text + a structural rung
#            (scoped, ambiguity-refusing); no browser-style page observations.
#   macos  : native AX exposes structured identity text + a structural rung
#            (scoped, ambiguity-refusing); no browser-style page observations.
#            It does NOT claim native AXPress actuation, so a resolved element
#            is acted on through the fully gated physical click.
#   rdp    : pixel-only network RDP -- no structured layer at all.
#   rdp-win: pixel-only local remote-display window (the Citrix analog) -- ditto.
EXPECTED: dict[str, dict[str, bool]] = {
    "web": {
        "structural": True,
        "identity": True,
        "structural_action": True,
        "system_of_record": False,
    },
    "windows": {
        "structural": False,
        "identity": True,
        "structural_action": True,
        "system_of_record": False,
    },
    "linux": {
        "structural": False,
        "identity": True,
        "structural_action": True,
        "system_of_record": False,
    },
    "macos": {
        "structural": False,
        "identity": True,
        "structural_action": True,
        "system_of_record": False,
    },
    "rdp": {
        "structural": False,
        "identity": False,
        "structural_action": False,
        "system_of_record": False,
    },
    "rdp-window": {
        "structural": False,
        "identity": False,
        "structural_action": False,
        "system_of_record": False,
    },
}

BACKEND_CLASSES = {
    "web": PlaywrightBackend,
    "windows": WindowsBackend,
    "linux": LinuxBackend,
    "macos": MacOSBackend,
    "rdp": FreeRDPBackend,
    "rdp-window": RemoteDisplayBackend,
}


def _protocol_members(protocol: type) -> frozenset[str]:
    """The attribute names a runtime_checkable protocol requires.

    Derived from the protocol itself so a member added to the protocol is
    enforced here automatically (a partial implementation cannot pass).

    Uses ``get_protocol_members`` -- the supported public accessor -- rather than
    the private ``__protocol_attrs__`` class attribute, which CPython only sets
    natively on Python 3.12+. Reading the dunder directly returned an empty/absent
    value on the 3.10 and 3.11 matrix legs, silently disabling this guarantee;
    the accessor returns the identical member set on every supported interpreter.
    """
    members = get_protocol_members(protocol)
    assert members, f"{protocol.__name__} exposes no protocol members"
    return frozenset(members)


def _implements(cls: type, protocol: type) -> bool:
    """True iff ``cls`` exposes EVERY member of ``protocol``.

    Uses the same attribute-presence criterion ``isinstance`` applies for
    runtime_checkable protocols, but at the class level so no live backend
    (and no optional native dependency) needs constructing.
    """
    return all(hasattr(cls, member) for member in _protocol_members(protocol))


def test_every_backend_class_is_in_the_matrix() -> None:
    """The matrix must cover exactly the buildable backend kinds -- if a new
    backend class is added, it has to declare its capabilities here (and, by the
    review that forces, in PRODUCT_STATUS.md) rather than land unmapped."""
    assert set(BACKEND_CLASSES) == set(EXPECTED)


def test_all_backends_implement_the_base_protocol() -> None:
    """Every backend is a full :class:`Backend`; the base surface is not
    optional -- a backend missing a base method could not be driven at all."""
    for kind, cls in BACKEND_CLASSES.items():
        missing = sorted(m for m in _protocol_members(Backend) if not hasattr(cls, m))
        assert not missing, (
            f"{kind} ({cls.__name__}) is missing base Backend members: {missing}"
        )


def test_optional_capability_matrix_matches_expected() -> None:
    """Each backend exposes EXACTLY the optional protocols the maturity map
    claims -- no silently-added and no silently-dropped capability."""
    actual = {
        kind: {
            label: _implements(cls, proto)
            for label, proto in OPTIONAL_PROTOCOLS.items()
        }
        for kind, cls in BACKEND_CLASSES.items()
    }
    assert actual == EXPECTED, (
        "backend capability drift vs docs/PRODUCT_STATUS.md; update BOTH the "
        f"matrix and the maturity docs.\nexpected={EXPECTED}\nactual={actual}"
    )


def test_no_backend_fabricates_a_system_of_record_oracle() -> None:
    """System-of-record effect verification comes from a configured external
    verifier (deployment.build_effect_verifier), never from a backend claiming
    to read the app's authoritative store. Guard against a backend quietly
    advertising the oracle protocol (which would let the effect miner bind a
    fabricated ``record_written`` from an unverified source)."""
    for kind, cls in BACKEND_CLASSES.items():
        assert not _implements(cls, SystemOfRecordBackend), (
            f"{kind} ({cls.__name__}) now advertises SystemOfRecordBackend; "
            "effect oracles must be external + configured, not backend-claimed"
        )


def test_pixel_substrates_advertise_no_structured_capability() -> None:
    """The substrates with no structured layer (pixel-only RDP / remote-display,
    the Citrix analog) must expose no structural or identity protocol, so
    identity honestly falls through to the OCR ladder instead of a structured
    check the substrate cannot back."""
    for kind in ("rdp", "rdp-window"):
        cls = BACKEND_CLASSES[kind]
        assert not _implements(cls, StructuralBackend)
        assert not _implements(cls, IdentityBackend)
        assert not _implements(cls, StructuralActionBackend)


def test_native_and_browser_structural_backends_expose_identity() -> None:
    """Wherever a backend DOES own a structured layer (browser DOM, Windows UIA,
    Linux AT-SPI, native macOS AX) it must expose both the identity read and the
    deterministic structural action rung -- structure that can locate an element
    must also be able to prove the element's identity."""
    for kind in ("web", "windows", "linux", "macos"):
        cls = BACKEND_CLASSES[kind]
        assert _implements(cls, IdentityBackend)
        assert _implements(cls, StructuralActionBackend)
