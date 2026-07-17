"""Auth provider contract -- the single shared interface + credential type.

This file is the load-bearing contract for hosted authentication. It is
OWNED by W1a; every other surface (the paste provider, the browser-PKCE
provider, the hosted push/count/ingest-report paths, and the tray) imports
from here and MUST NOT redefine these shapes.

v1 ships TWO providers behind this one interface, both writing the SAME
``Credential`` into ONE keychain store (see ``engine.auth.store``):

    - ``PasteTokenProvider``   (``engine.auth.paste``)        -- token paste / BYOC / headless
    - ``BrowserPkceProvider``  (``engine.auth.browser_pkce``) -- system-browser + loopback PKCE

Swapping or adding a provider is additive: nothing at a call site changes,
because every outbound call resolves its bearer token through
``engine.auth.store.auth_header()``.

See ``.private/desktop_tray_architecture_2026_07_14.md`` section 3a for the
canonical specification.
"""

from __future__ import annotations

from typing import Literal, Protocol, TypedDict, runtime_checkable


class Credential(TypedDict):
    """A stored-ready hosted credential.

    Both providers write this exact shape via
    :func:`engine.auth.store.store_credential`. The ``kind`` discriminates
    between a bare ingest token (the machine/push path) and a full Supabase
    session (the interactive browser path, which additionally mints an ingest
    token so the headless push/count path always has a bearer token).

    Attributes:
        kind: ``"ingest_token"`` for an ``oai_ingest_…`` token, or
            ``"supabase_session"`` for a Supabase access token.
        token: The ingest token (``oai_ingest_…``) OR the Supabase access token.
        refresh_token: Supabase refresh token (Supabase sessions only), else None.
        org_id: The organization the token resolves to, if known.
        host: The hosted base URL, e.g. ``https://app.openadapt.ai``.
        expires_at: POSIX timestamp when the credential expires, or None.
    """

    kind: Literal["ingest_token", "supabase_session"]
    token: str
    refresh_token: str | None
    org_id: str | None
    host: str
    expires_at: float | None


@runtime_checkable
class AuthProvider(Protocol):
    """Interface implemented by every hosted auth provider.

    Attributes:
        name: A stable provider identifier -- ``"paste"`` or ``"browser_pkce"``.
    """

    name: str

    def login(self) -> Credential:
        """Interactively authenticate and return a stored-ready credential.

        Implementations MUST persist the credential via
        :func:`engine.auth.store.store_credential` before returning it, so
        that ``auth_header()`` immediately resolves the freshly minted token.

        Returns:
            The ``Credential`` that was stored.
        """
        ...

    def is_available(self) -> bool:
        """Whether this provider can run in the current environment.

        For example, :class:`~engine.auth.browser_pkce.BrowserPkceProvider`
        returns ``False`` on a headless server (no system browser / no
        loopback), so the UI falls back to token paste.
        """
        ...
