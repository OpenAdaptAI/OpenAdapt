"""Citrix Workspace-window pixel backend: drive a Citrix Workspace/Receiver
SESSION WINDOW as a no-DOM pixel surface.

Over Citrix, the local machine holds a **Citrix Workspace/Viewer window that
paints the pixels of a remote ICA/HDX session**; there is no in-guest agent on
our side of the ICA boundary and **UIA/MSAA/DOM do not cross ICA**. So the
Citrix execution wire is a **local-OS backend that screenshots the Workspace
window and injects OS-level input into it** -- pixel-only, no
structural/identity layer, resolution on the visual floor (template ->
template_global -> ocr -> geometry), with record-specific pixel/OCR identity
evidence supplied by the compiled workflow.

That backend already exists: :class:`~openadapt_flow.backends.remote_display.RemoteDisplayBackend`
(window-scoped ``CGWindowListCreateImage`` / ``PrintWindow`` capture + ``CGEvent``
/ ``SendInput`` injection, per-monitor-v2 DPI, fail-loud when input trust is
missing). It is target-window-agnostic: point it at the Citrix Workspace window
by owner/title and the *same* capture+inject code runs. This module is the thin,
**Citrix-specific preset** over it:

* :data:`CITRIX_WINDOW_OWNERS` -- the per-platform Workspace/Viewer window owner
  names (the "magic strings" a caller would otherwise have to know).
* :func:`default_citrix_owner` -- pick the host's Citrix owner.
* :class:`CitrixWorkspaceBackend` -- a ``RemoteDisplayBackend`` that defaults its
  target owner to the host's Citrix Workspace window and documents the ICA
  constraints. It conforms to the base :class:`~openadapt_flow.backend.Backend`
  protocol UNCHANGED, so the resolver ladder + effect verification run over it
  exactly as over any other backend, and -- like ``RemoteDisplayBackend`` -- it
  deliberately implements ONLY the base protocol (NOT ``StructuralBackend`` /
  ``IdentityBackend`` / ``StructuralActionBackend``), so the structural rung is
  unavailable by construction: the Citrix pixel floor.

Validation status (see ``benchmark/citrix_workspace/README.md``):
* DONE: window-scoped pixel capture + OS input injection inherit the
  ``RemoteDisplayBackend`` frame-freshness, occlusion, input-trust, and
  DPI-consistency gates. The healthy gate path is exercised end-to-end by the
  record->compile->replay + drift contract over the no-DOM canvas stand-in
  (``benchmark/canvas_ladder`` fixture); the base backend's unit suite owns the
  corresponding refusal cases.
* PENDING: validation against a REAL Citrix Workspace window on a live CVAD/DaaS
  ICA/HDX session (needs the trial lab + a GUI host with Screen-Recording /
  Accessibility trust). Everything upstream of the window is already proven; the
  ICA-specific delta is HDX codec artifacts, ICA compression, and the exact
  Workspace-window owner/title + a session-lock readiness marker. See the
  README's "Real ICA/HDX release gate" runbook.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional

from openadapt_flow.backends.remote_display import (
    RemoteDisplayBackend,
    RemoteDisplayError,
    WindowClient,
)

# Per-platform Citrix Workspace / Receiver *client window* owner names. These are
# the on-screen owner-app identities of the window that paints the ICA session
# (NOT the ICA server). Verified against Citrix Workspace app naming; confirm the
# exact string on the target host with the window inventory (see README) before a
# production run -- Citrix has renamed this across versions/platforms.
CITRIX_WINDOW_OWNERS: dict[str, tuple[str, ...]] = {
    # macOS: the session window is owned by "Citrix Viewer".
    "darwin": ("Citrix Viewer",),
    # Windows native matching is against the exact owning PROCESS basename
    # (``.exe`` optional), not a display label. The normal ICA session process
    # is wfica32.exe; CDViewer is retained as a documented alternate that an
    # operator can select explicitly after exact window inventory.
    "win32": ("wfica32", "CDViewer"),
    # Linux: the native client (wfica / "Citrix Viewer").
    "linux": ("Citrix Viewer", "wfica"),
}

# Default coarse readiness marker text: a stable in-session chrome/word that,
# when absent, means the frame is a lock/login/disconnect screen rather than the
# app -- fail closed. Left None by default (deployment-specific); the CVAD runbook
# sets it to a marker unique to the published app.
DEFAULT_CITRIX_READINESS_TEXT: Optional[str] = None


def default_citrix_owner(platform: Optional[str] = None) -> str:
    """Return the most likely Citrix Workspace window owner for ``platform``.

    ``platform`` defaults to ``sys.platform``. Returns the first (canonical)
    owner for the host; a caller can override with an exact owner if their
    Workspace build differs (confirm with the window inventory in the README).
    """
    plat = platform or sys.platform
    key = (
        "win32"
        if plat.startswith("win")
        else ("darwin" if plat == "darwin" else "linux")
    )
    return CITRIX_WINDOW_OWNERS[key][0]


class CitrixWorkspaceBackend(RemoteDisplayBackend):
    """``RemoteDisplayBackend`` preset targeting the Citrix Workspace window.

    Identical capture/inject/safety behavior as ``RemoteDisplayBackend`` (it IS
    one); it only defaults ``owner_substr`` to the host's Citrix Workspace/Viewer
    window owner so a caller does not have to know the per-platform string, and
    it carries the Citrix scope note. Pixel-only by construction: it inherits the
    base-protocol-only surface, so the resolver's structural rung stays
    unavailable (the ICA floor).

    Args:
        client: Host-OS ``WindowClient`` (real Mac/Win client, or a stand-in
            such as the canvas ``WindowClient`` used in the fixture test). When
            None, the host's native macOS or Windows client is used.
        owner_substr: Override the Citrix owner name (defaults to the host's
            canonical Citrix Workspace window owner). Use this if your Workspace
            build reports a different owner.
        window_title: Optional exact window title to disambiguate when multiple
            Citrix session windows are open. Zero/multiple matches fail closed.
        readiness_text: Coarse in-session OCR marker; when set, a frame missing
            it is refused as a lock/login/disconnect screen.
        **kwargs: Passed through to ``RemoteDisplayBackend`` (``require_input_trust``,
            ``activate_before_input``, ``settle_s``, ``max_frame_age_s``,
            ``readiness_probe`` ...).
    """

    def __init__(
        self,
        client: Optional[WindowClient] = None,
        *,
        owner_substr: Optional[str] = None,
        window_title: Optional[str] = None,
        readiness_text: Optional[str] = DEFAULT_CITRIX_READINESS_TEXT,
        readiness_probe: Optional[Callable[[bytes], bool]] = None,
        **kwargs: object,
    ) -> None:
        if client is None and sys.platform.startswith("linux"):
            raise RemoteDisplayError(
                "Citrix Workspace window replay on Linux requires an injected "
                "WindowClient; the built-in default WindowClient is available "
                "only on macOS (Quartz) and Windows (Win32)"
            )
        owner = owner_substr or default_citrix_owner()
        if readiness_probe is None and readiness_text:
            readiness_probe = _ocr_marker_probe(readiness_text)
        super().__init__(
            client,
            owner_substr=owner,
            title_substr=window_title,
            readiness_probe=readiness_probe,
            **kwargs,  # type: ignore[arg-type]
        )
        self._citrix_owner = owner


def _ocr_marker_probe(
    marker: str, *, min_ratio: float = 0.8
) -> Callable[[bytes], bool]:
    """Build a coarse OCR readiness predicate (import stays lazy)."""
    text = marker.strip()

    def _probe(png: bytes) -> bool:
        from openadapt_flow import vision

        return vision.find_text(png, text, min_ratio=min_ratio) is not None

    return _probe
