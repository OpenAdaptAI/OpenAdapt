"""Windows backend over the WAA (Windows Agent Arena) HTTP API.

Implements the `openadapt_flow.backend.Backend` protocol against the WAA
Flask server that runs inside the Windows VM (the WAADirect pattern from
openadapt-evals — plain HTTP, no adapter layer):

    GET  /screenshot        -> raw PNG bytes (Flask send_file, NOT base64 JSON)
    POST /execute_windows   -> server does exec(command, ...) with pyautogui
                               importable. The payload is
                               ``{"command": "<bare Python statements>"}`` —
                               NOT wrapped in ``python -c "..."``.

The backend is vision-only by construction for RESOLUTION: PNG frames in,
pixel-coordinate input out. It deliberately does NOT implement the optional
`StructuralBackend` observations (url/title/page count) — native Windows has
no cheap equivalent, so those steps stay honestly unverified (docs/LIMITS.md).

It DOES implement the optional `IdentityBackend.structured_text_at`: identity
verification (unlike resolution) can use a higher-fidelity signal than OCR
where one exists. On native Windows that signal is the UI Automation tree --
the element under a point exposes ``Name``/``Value``/text even when it has no
stable ``AutomationId`` (the Phase-2 "no AutomationId" finding does not block
UIA *text* extraction). The read runs a UIA ``ElementFromPoint`` snippet on the
VM and returns the row-like element's real characters when the WAA execute
channel echoes them back, or None when it cannot (older WAA server that does
not return command output, UIA unavailable, pixel-only session) -- in which
case the identity ladder falls back to the OCR name+DOB-primary tier.

Typed text is embedded into the command via ``repr()`` (a valid Python
literal, immune to quoting bugs). Non-ASCII text cannot be typed by
pyautogui.write (it silently drops unknown characters — a wrong-write mode),
so it is routed through the Windows clipboard instead: the value travels
base64-encoded to PowerShell ``Set-Clipboard`` and is pasted with Ctrl+V.
"""

from __future__ import annotations

import base64
import struct
import warnings
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import requests

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.ir import (
    ActionDeliveryReceipt,
    StructuralHandle,
    StructuralLocator,
)

DEFAULT_SERVER_URL = "http://localhost:5001"

# Hosts treated as loopback: plaintext HTTP to these never leaves the machine,
# so it is permitted in dev (with a warning) even when TLS is not required.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})

# Screenshot retry defaults (mirrors WAADirect in openadapt-evals).
SCREENSHOT_MAX_RETRIES = 3
SCREENSHOT_RETRY_DELAY_S = 2.0

# The Backend protocol expresses scroll in pixels (a Playwright wheel
# gesture); pyautogui expresses it in wheel notches. One notch scrolls
# roughly this many pixels in common Windows apps. Approximate by design —
# to be tuned against the real VM (Phase 2); replay's closed-loop scroll
# re-resolves after each gesture, so the exact ratio is not load-bearing.
SCROLL_PIXELS_PER_NOTCH = 100

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class _TypedResponse:
    """Result of a tolerant typed observation request."""

    available: bool
    payload: Optional[dict]


class _TypedRouteUnavailable(RuntimeError):
    """An explicitly legacy-enabled dev agent lacks the typed endpoint."""


# Playwright-style modifier names (as recorded / emitted by the replayer,
# e.g. 'ControlOrMeta+a') -> pyautogui key names. On Windows the Meta/Command
# key maps to the Windows key and ControlOrMeta resolves to Ctrl.
_MODIFIER_MAP = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "controlormeta": "ctrl",
    "meta": "win",
    "cmd": "win",
    "command": "win",
    "win": "win",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
}

# Playwright-style named keys -> pyautogui key names.
_NAMED_KEY_MAP = {
    "enter": "enter",
    "return": "enter",
    "tab": "tab",
    "escape": "esc",
    "esc": "esc",
    "backspace": "backspace",
    "delete": "delete",
    "space": "space",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
}


def _png_size(png: bytes) -> tuple[int, int]:
    """Return (width, height) parsed from a PNG's IHDR chunk.

    Args:
        png: PNG file bytes.

    Returns:
        (width, height) in pixels.

    Raises:
        ValueError: If the bytes are not a PNG.
    """
    if len(png) < 24 or not png.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG frame")
    width, height = struct.unpack(">II", png[16:24])
    return int(width), int(height)


def _pyautogui_key(part: str) -> str:
    """Map one Playwright-style key/modifier name to pyautogui's name."""
    lower = part.lower()
    if lower in _MODIFIER_MAP:
        return _MODIFIER_MAP[lower]
    if lower in _NAMED_KEY_MAP:
        return _NAMED_KEY_MAP[lower]
    # Single characters and F-keys: pyautogui uses lowercase names.
    return lower


def normalize_chord(key: str) -> list[str]:
    """Normalize a key or ``+``-joined chord to pyautogui key names.

    Args:
        key: e.g. ``'Enter'``, ``'ControlOrMeta+a'``, ``'Meta+d'``.

    Returns:
        List of pyautogui key names, modifiers first (input order kept).

    Raises:
        ValueError: If the chord is empty.
    """
    parts = [p for p in key.split("+") if p]
    if not parts:
        raise ValueError(f"empty key chord: {key!r}")
    return [_pyautogui_key(p) for p in parts]


class WindowsBackend:
    """`Backend` implementation over the WAA HTTP API.

    Args:
        server_url: WAA Flask server base URL (default matches the standard
            SSH tunnel ``localhost:5001`` -> VM ``:5000``).
        viewport: Optional (width, height) override. When omitted it is
            derived once from the first screenshot's PNG header and cached.
        type_interval_s: Inter-key delay for ``pyautogui.write``.
        timeout_s: HTTP timeout per request.
        screenshot_max_retries: Screenshot retry attempts (server may be
            momentarily unready).
        screenshot_retry_delay_s: Delay between screenshot retries.
        auth_token: Optional bearer token. When the in-guest agent
            (``openadapt_flow.backends.win_agent``) is started with a token,
            every request must carry ``Authorization: Bearer <token>``; set it
            here to talk to an authenticated agent. None (default) sends no
            auth header (the loopback-only / legacy unauthenticated shim).
        pin_fingerprint: SHA-256 fingerprint of the agent's per-run certificate,
            as provisioned by the control plane. When ``server_url`` is
            ``https://`` and this is set, the client **pins** it: the TLS
            session is accepted only if the server presents exactly that
            certificate (see ``win_agent.tls`` for the trust model). A
            wrong/unpinned cert is rejected at handshake. This is the
            PHI-in-transit control -- encryption + server identity; the bearer
            token remains the independent authorization factor.
        require_tls: Fail-closed switch. When True the client REFUSES a
            plaintext ``http://`` ``server_url`` (raises rather than silently
            downgrade). When None (default) it is inferred: **required** for a
            non-loopback host, **relaxed** for loopback (dev may use plaintext
            with a warning). No silent downgrade in any case.
        session: Optional ``requests.Session`` (injected in tests). When a
            ``pin_fingerprint`` is set the pinning adapter is mounted onto it.
    """

    def __init__(
        self,
        server_url: str = DEFAULT_SERVER_URL,
        *,
        viewport: Optional[tuple[int, int]] = None,
        type_interval_s: float = 0.05,
        timeout_s: float = 30.0,
        screenshot_max_retries: int = SCREENSHOT_MAX_RETRIES,
        screenshot_retry_delay_s: float = SCREENSHOT_RETRY_DELAY_S,
        auth_token: Optional[str] = None,
        pin_fingerprint: Optional[str] = None,
        require_tls: Optional[bool] = None,
        session: Optional[requests.Session] = None,
        allow_legacy_exec: bool = False,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self._viewport = viewport
        self._type_interval_s = type_interval_s
        self._timeout_s = timeout_s
        self._screenshot_max_retries = max(1, int(screenshot_max_retries))
        self._screenshot_retry_delay_s = screenshot_retry_delay_s
        self._auth_token = auth_token or None
        self._pin_fingerprint = pin_fingerprint or None
        self._allow_legacy_exec = bool(allow_legacy_exec)

        scheme = urlparse(self.server_url).scheme.lower()
        host = urlparse(self.server_url).hostname or ""
        is_loopback = host in _LOOPBACK_HOSTS
        self._require_tls = (not is_loopback) if require_tls is None else require_tls
        self._tls = scheme == "https"

        # Fail closed: a required-TLS channel must NOT run over plaintext. No
        # silent downgrade -- refuse at construction so a misconfigured PHI lane
        # cannot send a single cleartext screenshot/command.
        if self._require_tls and not self._tls:
            raise ValueError(
                f"require_tls: refusing plaintext {scheme or 'http'}:// to "
                f"non-loopback host {host!r}; use https:// with a pinned "
                "per-run certificate (win_agent.tls). Set require_tls=False "
                "only for a loopback dev channel."
            )
        if not self._tls and not is_loopback:
            # Reachable only when require_tls was explicitly forced False.
            warnings.warn(
                f"win_agent channel to {host!r} is PLAINTEXT (PHI in the "
                "clear); TLS explicitly disabled. Do not use for real PHI.",
                stacklevel=2,
            )
        elif not self._tls:
            warnings.warn(
                "win_agent channel is plaintext HTTP (loopback dev). Provision "
                "a per-run cert + https:// before carrying PHI off-host.",
                stacklevel=2,
            )

        self._session = session if session is not None else requests.Session()
        if self._tls and self._pin_fingerprint:
            from openadapt_flow.backends.win_agent.tls import pinned_session

            pinned_session(self._pin_fingerprint, session=self._session)
        elif self._tls and not self._pin_fingerprint:
            # HTTPS with no pin: still encrypted, but a per-run self-signed cert
            # will not validate against system CAs -- surface the gap loudly
            # rather than let requests raise an opaque SSLError at first call.
            warnings.warn(
                "win_agent https:// channel has no pin_fingerprint; server "
                "identity falls back to system-CA validation, which a per-run "
                "self-signed agent cert will fail. Provide the fingerprint the "
                "control plane minted.",
                stacklevel=2,
            )

    def _request_kwargs(self) -> dict:
        """Per-request kwargs: always ``timeout``, plus ``headers`` IFF a token
        is set.

        The bearer header is added ONLY when authenticating so the
        unauthenticated call shape is byte-for-byte the legacy one
        (``timeout`` only) -- a caller/mocked session that predates the
        ``auth_token`` option is never handed an unexpected ``headers`` kwarg.
        """
        kwargs: dict = {"timeout": self._timeout_s}
        if self._auth_token:
            kwargs["headers"] = {"Authorization": f"Bearer {self._auth_token}"}
        return kwargs

    # -- Backend protocol ----------------------------------------------------

    @property
    def viewport(self) -> tuple[int, int]:
        """(width, height) of the VM screen, derived from a screenshot."""
        if self._viewport is None:
            self._viewport = _png_size(self.screenshot())
        return self._viewport

    def screenshot(self) -> bytes:
        """Return the current frame as PNG bytes (with retries).

        WAA's ``/screenshot`` returns raw PNG via Flask ``send_file()`` —
        read ``resp.content``, never ``resp.json()``.

        Raises:
            RuntimeError: If all attempts fail or return a non-PNG payload.
        """
        import time

        last_error: Optional[Exception] = None
        for attempt in range(1, self._screenshot_max_retries + 1):
            try:
                resp = self._session.get(
                    f"{self.server_url}/screenshot",
                    **self._request_kwargs(),
                )
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"screenshot HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                png = resp.content
                _png_size(png)  # validates signature and header
                return png
            except Exception as e:  # noqa: BLE001 - retried, then re-raised
                last_error = e
                if attempt < self._screenshot_max_retries:
                    time.sleep(self._screenshot_retry_delay_s)
        raise RuntimeError(
            f"screenshot failed after {self._screenshot_max_retries} attempts"
        ) from last_error

    def _post_typed_read(self, path: str, payload: dict) -> _TypedResponse:
        """POST a typed observation without turning absence into an action.

        A 404 means an older development agent lacks the typed contract. It is
        distinguishable from a successful ``None`` observation so explicitly
        legacy-enabled callers can use the compatibility path. Every other
        error is an unavailable observation and therefore falls through to the
        visual/OCR ladder; it never fabricates a structural result.
        """
        try:
            response = self._session.post(
                f"{self.server_url}{path}",
                json=payload,
                **self._request_kwargs(),
            )
        except Exception:
            return _TypedResponse(False, None)
        if response.status_code == 404:
            return _TypedResponse(False, None)
        if response.status_code != 200:
            return _TypedResponse(True, None)
        try:
            data = response.json()
        except Exception:
            return _TypedResponse(True, None)
        if not isinstance(data, dict) or data.get("status") != "ok":
            return _TypedResponse(True, None)
        return _TypedResponse(True, data)

    def _post_typed_action(self, path: str, payload: dict) -> dict:
        """POST a bounded typed action and fail loudly on non-delivery."""
        try:
            response = self._session.post(
                f"{self.server_url}{path}",
                json=payload,
                **self._request_kwargs(),
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"typed win-agent action {path} unreachable: {exc}"
            ) from exc
        if response.status_code != 200:
            if response.status_code == 404:
                raise _TypedRouteUnavailable(
                    f"typed win-agent route {path} unavailable"
                )
            code: Optional[str] = None
            message: Optional[str] = None
            try:
                error = response.json()
            except Exception:
                error = None
            if isinstance(error, dict):
                code = error.get("code") if isinstance(error.get("code"), str) else None
                message = (
                    error.get("error") if isinstance(error.get("error"), str) else None
                )
            if (
                path == "/uia/act"
                and response.status_code == 409
                and code
                in {
                    "ambiguous_target",
                    "native_action_failed",
                    "native_action_unavailable",
                    "stale_target",
                    "target_not_found",
                }
            ):
                raise StructuralResolutionRefused(
                    f"UIA actuation refused ({code}): {message or 'target changed'}"
                )
            raise RuntimeError(
                f"typed win-agent action {path} HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(
                f"typed win-agent action {path} returned invalid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(
                f"typed win-agent action {path} returned invalid payload"
            )
        return data

    @staticmethod
    def _validate_physical_receipt(payload: dict, operation: str) -> None:
        """Require exact input-delivery evidence before treating HTTP 200 as success."""
        try:
            receipt = ActionDeliveryReceipt.model_validate(payload)
        except Exception as exc:
            raise RuntimeError(
                "typed input returned an invalid delivery receipt"
            ) from exc
        if (
            receipt.operation != operation
            or receipt.native
            or receipt.target_fingerprint is not None
            or receipt.outcome_verified is not False
        ):
            raise RuntimeError("typed input returned a mismatched delivery receipt")

    # -- structured-text identity (openadapt_flow.backend.IdentityBackend) --

    def structured_text_at(self, x: int, y: int) -> Optional[str]:
        """Return the UI Automation text of the element/row under (x, y).

        Identity verification prefers STRUCTURED text over OCR where the
        backend can provide it (see :class:`IdentityBackend`): UIA hands back
        the REAL characters of the control under the point, so the
        same-name/same-DOB glyph-collapse that defeats OCR (an MRN whose only
        difference is an O/0 or l/1 glyph) cannot occur -- the two rows are
        different strings in the a11y tree.

        Runs a ``uiautomation.ControlFromPoint`` snippet on the VM: it walks
        up to the enclosing ROW / list-item control (so identity is judged on
        the whole record row, not one cell), EXCLUDES the clicked control's own
        cell (its label is mutable evidence the ladder heals through -- mirror
        of the OCR band excluding the target's own crop), and prints the
        remaining cells' ``Name`` text between sentinel markers. Returns None when there
        is no row-like ancestor (a standalone control whose own text is a
        mutable, healable label -- identity stays on the OCR / heal path),
        when the WAA server does not echo command output, when UIA is
        unavailable, or when nothing is under the point (never raises) -- the
        identity ladder then falls back to the OCR tier.
        """
        typed = self._post_typed_read("/uia/text-at-point", {"x": int(x), "y": int(y)})
        if typed.available:
            value = typed.payload.get("text") if typed.payload is not None else None
            return str(value) if isinstance(value, str) and value else None
        if not self._allow_legacy_exec:
            return None

        snippet = (
            "import json\n"
            "def _oaflow_structured_text_at(px, py):\n"
            "    try:\n"
            "        import uiautomation as auto\n"
            "    except Exception:\n"
            "        return None\n"
            "    try:\n"
            "        el = auto.ControlFromPoint(px, py)\n"
            "    except Exception:\n"
            "        return None\n"
            "    if el is None:\n"
            "        return None\n"
            "    row = el\n"
            "    found_row = False\n"
            "    for _ in range(6):\n"
            "        try:\n"
            "            ct = row.ControlTypeName\n"
            "        except Exception:\n"
            "            ct = ''\n"
            "        if ct in ('DataItemControl', 'ListItemControl',\n"
            "                  'TreeItemControl', 'TableRowControl'):\n"
            "            found_row = True\n"
            "            break\n"
            "        parent = getattr(row, 'GetParentControl', None)\n"
            "        nxt = parent() if parent else None\n"
            "        if nxt is None:\n"
            "            break\n"
            "        row = nxt\n"
            "    if not found_row:\n"
            "        return None\n"
            "    own = el\n"
            "    for _ in range(6):\n"
            "        p2 = getattr(own, 'GetParentControl', None)\n"
            "        par = p2() if p2 else None\n"
            "        if par is None:\n"
            "            own = None\n"
            "            break\n"
            "        if par is row:\n"
            "            break\n"
            "        own = par\n"
            "    parts = []\n"
            "    got_child = False\n"
            "    try:\n"
            "        for c in row.GetChildren():\n"
            "            if own is not None and c is own:\n"
            "                continue\n"
            "            nm = getattr(c, 'Name', '') or ''\n"
            "            if nm:\n"
            "                parts.append(str(nm))\n"
            "                got_child = True\n"
            "    except Exception:\n"
            "        pass\n"
            "    if not got_child:\n"
            "        v = getattr(row, 'Name', '') or ''\n"
            "        if v:\n"
            "            parts.append(str(v))\n"
            "    text = ' '.join(parts).split()\n"
            "    return ' '.join(text) if text else None\n"
            "print('<<OAFLOW_STRUCTURED>>' + json.dumps("
            f"_oaflow_structured_text_at({int(x)}, {int(y)})) "
            "+ '<<END_OAFLOW_STRUCTURED>>')\n"
        )
        body = self._execute_read(snippet)
        if not body:
            return None
        import json as _json

        start = body.find("<<OAFLOW_STRUCTURED>>")
        end = body.find("<<END_OAFLOW_STRUCTURED>>")
        if start == -1 or end == -1 or end <= start:
            return None
        payload = body[start + len("<<OAFLOW_STRUCTURED>>") : end]
        try:
            value = _json.loads(payload)
        except Exception:
            return None
        if not value:
            return None
        return str(value)

    def _execute_read(self, command: str) -> Optional[str]:
        """POST bare Python to WAA and return the response body if any.

        Unlike :meth:`_execute` (which discards the body), this returns the
        server's textual output so a UIA read can travel back. Tolerant of the
        WAA server's exact response shape (raw stdout, or a JSON envelope with
        an ``output``/``stdout``/``result`` field); returns None on any
        non-200, missing output, or transport failure -- identity then falls
        back to OCR.
        """
        if not self._allow_legacy_exec:
            return None
        try:
            resp = self._session.post(
                f"{self.server_url}/execute_windows",
                json={"command": command},
                **self._request_kwargs(),
            )
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        text = resp.text or ""
        if "<<OAFLOW_STRUCTURED>>" in text:
            return text
        try:
            data = resp.json()
        except Exception:
            return text or None
        if isinstance(data, dict):
            for key in ("output", "stdout", "result", "data"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    return val
        return None

    # -- structural action (openadapt_flow.backend.StructuralActionBackend) --

    def _read_structured_json(self, snippet: str) -> object:
        """Run ``snippet`` on the VM and decode its sentinel-wrapped JSON.

        The snippet must ``print`` its result as
        ``<<OAFLOW_STRUCTURED>>` + json.dumps(value) + `<<END_OAFLOW_STRUCTURED>>``.
        Returns the decoded value (which may itself be None), or None when the
        server echoes nothing, the markers are absent, or decoding fails --
        callers then fall back to the visual ladder.
        """
        body = self._execute_read(snippet)
        if not body:
            return None
        import json as _json

        start = body.find("<<OAFLOW_STRUCTURED>>")
        end = body.find("<<END_OAFLOW_STRUCTURED>>")
        if start == -1 or end == -1 or end <= start:
            return None
        payload = body[start + len("<<OAFLOW_STRUCTURED>>") : end]
        try:
            return _json.loads(payload)
        except Exception:
            return None

    def structural_locator_at(self, x: int, y: int) -> Optional[StructuralLocator]:
        """Return a stable UIA locator for the control under (x, y).

        Runs a ``uiautomation.ControlFromPoint`` snippet on the VM: it climbs to
        the nearest ACTIONABLE control (button / hyperlink / menu-item / ...) and
        reads its ``AutomationId``, ``ControlType`` (mapped to an ARIA-style
        role) and ``Name``. Returns None when there is no actionable control,
        neither an AutomationId nor a usable role+name exists, UIA is
        unavailable, or the WAA server does not echo output (never raises) --
        the step then relies on the visual anchor.
        """
        typed = self._post_typed_read("/uia/locator-at", {"x": int(x), "y": int(y)})
        if typed.available:
            value = typed.payload.get("locator") if typed.payload is not None else None
            if not isinstance(value, dict):
                return None
            try:
                return StructuralLocator.model_validate(value)
            except Exception:
                return None
        if not self._allow_legacy_exec:
            return None

        snippet = (
            "import json\n"
            "def _oaflow_locator_at(px, py):\n"
            "    try:\n"
            "        import uiautomation as auto\n"
            "    except Exception:\n"
            "        return None\n"
            "    try:\n"
            "        el = auto.ControlFromPoint(px, py)\n"
            "    except Exception:\n"
            "        return None\n"
            "    if el is None:\n"
            "        return None\n"
            "    actionable = None\n"
            "    node = el\n"
            "    actionable_types = ('ButtonControl', 'HyperlinkControl',\n"
            "        'MenuItemControl', 'TabItemControl', 'ListItemControl',\n"
            "        'CheckBoxControl', 'RadioButtonControl',\n"
            "        'SplitButtonControl', 'EditControl')\n"
            "    for _ in range(6):\n"
            "        try:\n"
            "            ct = node.ControlTypeName\n"
            "        except Exception:\n"
            "            ct = ''\n"
            "        if ct in actionable_types:\n"
            "            actionable = node\n"
            "            break\n"
            "        p = getattr(node, 'GetParentControl', None)\n"
            "        nxt = p() if p else None\n"
            "        if nxt is None:\n"
            "            break\n"
            "        node = nxt\n"
            "    if actionable is None:\n"
            "        actionable = el\n"
            "    try:\n"
            "        ct = actionable.ControlTypeName\n"
            "    except Exception:\n"
            "        ct = ''\n"
            "    role_map = {'ButtonControl': 'button',\n"
            "        'HyperlinkControl': 'link', 'MenuItemControl': 'menuitem',\n"
            "        'TabItemControl': 'tab', 'ListItemControl': 'listitem',\n"
            "        'CheckBoxControl': 'checkbox',\n"
            "        'RadioButtonControl': 'radio', 'EditControl': 'textbox',\n"
            "        'SplitButtonControl': 'button'}\n"
            "    role = role_map.get(ct)\n"
            "    aid = str(getattr(actionable, 'AutomationId', '') or '')\n"
            "    name = ' '.join(str(getattr(actionable, 'Name', '') or ''"
            ").split())\n"
            "    aid = aid or None\n"
            "    name = name or None\n"
            "    if not aid and not (role and name):\n"
            "        return None\n"
            "    return {'automation_id': aid, 'role': role, 'name': name}\n"
            "print('<<OAFLOW_STRUCTURED>>' + json.dumps("
            f"_oaflow_locator_at({int(x)}, {int(y)})) "
            "+ '<<END_OAFLOW_STRUCTURED>>')\n"
        )
        value = self._read_structured_json(snippet)
        if not isinstance(value, dict):
            return None
        return StructuralLocator(
            automation_id=value.get("automation_id"),
            role=value.get("role"),
            name=value.get("name"),
            window_name=value.get("window_name"),
        )

    def locate_structural(
        self, locator: StructuralLocator
    ) -> Optional[StructuralHandle]:
        """Locate ``locator``'s control via UIA; return its center point.

        Searches the UIA tree from the root by ``AutomationId`` first, else by
        the mapped ``ControlType`` + ``Name``, and returns the matched control's
        ``BoundingRectangle`` center in :meth:`click` coordinate space. Returns
        None on no/ambiguous match, an empty rectangle, unavailable UIA, or no
        echoed output (never raises) -- the resolver then uses the visual ladder.
        """
        aid = locator.automation_id or ""
        role = locator.role or ""
        name = locator.name or ""
        if not aid and not (role and name):
            return None
        typed = self._post_typed_read(
            "/uia/find",
            {"locator": locator.model_dump(mode="json", exclude_none=True)},
        )
        if typed.available:
            payload = typed.payload
            if payload is None:
                return None
            match = payload.get("match")
            candidate_count = payload.get("candidate_count")
            truncated = payload.get("truncated") is True
            candidates = payload.get("candidates")
            if match == "not_found":
                return None
            if match == "ambiguous" or truncated:
                raise StructuralResolutionRefused(
                    "UIA locator is ambiguous: "
                    f"candidate_count={candidate_count!r}, truncated={truncated}"
                )
            if (
                match != "unique"
                or candidate_count != 1
                or not isinstance(candidates, list)
                or len(candidates) != 1
                or not isinstance(candidates[0], dict)
            ):
                return None
            candidate = candidates[0]
            point = candidate.get("point")
            bounds = candidate.get("bounds")
            fingerprint = candidate.get("fingerprint")
            operations = candidate.get("supported_operations", [])
            if (
                not isinstance(point, list)
                or len(point) != 2
                or not all(
                    isinstance(item, int) and not isinstance(item, bool)
                    for item in point
                )
                or not isinstance(fingerprint, str)
                or len(fingerprint) != 64
                or not isinstance(bounds, list)
                or len(bounds) != 4
                or not all(
                    isinstance(item, int) and not isinstance(item, bool)
                    for item in bounds
                )
                or bounds[2] <= bounds[0]
                or bounds[3] <= bounds[1]
                or not isinstance(operations, list)
                or any(not isinstance(item, str) for item in operations)
            ):
                return None
            return StructuralHandle(
                point=(point[0], point[1]),
                region=(
                    bounds[0],
                    bounds[1],
                    bounds[2] - bounds[0],
                    bounds[3] - bounds[1],
                ),
                target_fingerprint=fingerprint,
                candidate_count=1,
                supported_operations=operations,
            )
        if not self._allow_legacy_exec:
            return None

        snippet = (
            "import json\n"
            "def _oaflow_locate(aid, role, name):\n"
            "    try:\n"
            "        import uiautomation as auto\n"
            "    except Exception:\n"
            "        return None\n"
            "    try:\n"
            "        root = auto.GetRootControl()\n"
            "    except Exception:\n"
            "        return None\n"
            "    ctrl_map = {'button': 'ButtonControl',\n"
            "        'link': 'HyperlinkControl', 'menuitem': 'MenuItemControl',\n"
            "        'tab': 'TabItemControl', 'listitem': 'ListItemControl',\n"
            "        'checkbox': 'CheckBoxControl',\n"
            "        'radio': 'RadioButtonControl', 'textbox': 'EditControl'}\n"
            "    el = None\n"
            "    if aid:\n"
            "        try:\n"
            "            cand = auto.Control(searchFromControl=root,\n"
            "                AutomationId=aid)\n"
            "            if cand.Exists(0, 0):\n"
            "                el = cand\n"
            "        except Exception:\n"
            "            el = None\n"
            "    if el is None and role and name:\n"
            "        cls = getattr(auto, ctrl_map.get(role, ''), None)\n"
            "        if cls is not None:\n"
            "            try:\n"
            "                cand = cls(searchFromControl=root, Name=name)\n"
            "                if cand.Exists(0, 0):\n"
            "                    el = cand\n"
            "            except Exception:\n"
            "                el = None\n"
            "    if el is None:\n"
            "        return None\n"
            "    try:\n"
            "        r = el.BoundingRectangle\n"
            "    except Exception:\n"
            "        return None\n"
            "    if r is None or r.right <= r.left or r.bottom <= r.top:\n"
            "        return None\n"
            "    cx = int((r.left + r.right) / 2)\n"
            "    cy = int((r.top + r.bottom) / 2)\n"
            "    return [cx, cy]\n"
            "print('<<OAFLOW_STRUCTURED>>' + json.dumps(_oaflow_locate("
            f"{aid!r}, {role!r}, {name!r})) + '<<END_OAFLOW_STRUCTURED>>')\n"
        )
        value = self._read_structured_json(snippet)
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(isinstance(v, (int, float)) for v in value)
        ):
            return None
        return StructuralHandle(point=(int(value[0]), int(value[1])))

    def act_structural(
        self,
        locator: StructuralLocator,
        handle: StructuralHandle,
        *,
        double: bool = False,
    ) -> ActionDeliveryReceipt:
        """Deliver a native UIA action to the same uniquely resolved target.

        The receipt is intentionally an input-delivery artifact only. Runtime
        postconditions and system-of-record effects still decide whether the
        workflow step succeeded.
        """
        fingerprint = handle.target_fingerprint
        if not fingerprint:
            raise RuntimeError("native UIA actuation requires a target fingerprint")
        response = self._post_typed_action(
            "/uia/act",
            {
                "locator": locator.model_dump(mode="json", exclude_none=True),
                "expected_fingerprint": fingerprint,
                "operation": "double_click" if double else "click",
            },
        )
        if response.get("candidate_count") != 1:
            raise RuntimeError("typed UIA action omitted unique-candidate evidence")
        receipt = response.get("receipt")
        try:
            parsed = ActionDeliveryReceipt.model_validate(receipt)
        except Exception as exc:
            raise RuntimeError(
                "typed UIA action returned an invalid delivery receipt"
            ) from exc
        if (
            not parsed.native
            or parsed.target_fingerprint != fingerprint
            or not parsed.operation.startswith("uia_")
            or parsed.outcome_verified is not False
        ):
            raise RuntimeError(
                "typed UIA action returned a mismatched delivery receipt"
            )
        return parsed

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        """Click (or double-click) through the bounded typed input contract."""
        try:
            response = self._post_typed_action(
                "/input",
                {
                    "action": "click",
                    "x": int(x),
                    "y": int(y),
                    "double": bool(double),
                },
            )
            self._validate_physical_receipt(
                response, "physical_double_click" if double else "physical_click"
            )
            return
        except _TypedRouteUnavailable:
            if not self._allow_legacy_exec:
                raise
        fn = "doubleClick" if double else "click"
        self._execute(f"import pyautogui; pyautogui.{fn}({int(x)}, {int(y)})")

    def type_text(self, text: str) -> None:
        """Type text into the currently focused element.

        ASCII text goes through ``pyautogui.write``. Text containing
        characters pyautogui cannot type (it silently drops them — a
        wrong-write failure mode) goes through the Windows clipboard:
        base64 -> PowerShell ``Set-Clipboard`` -> Ctrl+V.
        """
        if not text:
            return
        try:
            response = self._post_typed_action(
                "/input",
                {
                    "action": "type_text",
                    "text": text,
                    "interval_s": self._type_interval_s,
                },
            )
            self._validate_physical_receipt(response, "physical_type_text")
            return
        except _TypedRouteUnavailable:
            if not self._allow_legacy_exec:
                raise
        if text.isascii():
            self._execute(
                "import pyautogui; "
                f"pyautogui.write({text!r}, interval={self._type_interval_s})"
            )
            return
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        ps = (
            "Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{b64}')))"
        )
        self._execute(
            "\n".join(
                [
                    "import subprocess; import time; import pyautogui",
                    "subprocess.run(['powershell', '-NoProfile', '-Command', "
                    f"{ps!r}], capture_output=True)",
                    "time.sleep(0.2)",
                    "pyautogui.hotkey('ctrl', 'v')",
                ]
            )
        )

    def press(self, key: str) -> None:
        """Press a key or chord, e.g. ``'Enter'`` or ``'ControlOrMeta+a'``."""
        keys = normalize_chord(key)
        try:
            response = self._post_typed_action(
                "/input", {"action": "press", "keys": keys}
            )
            self._validate_physical_receipt(response, "physical_press")
            return
        except _TypedRouteUnavailable:
            if not self._allow_legacy_exec:
                raise
        if len(keys) == 1:
            self._execute(f"import pyautogui; pyautogui.press({keys[0]!r})")
        else:
            args = ", ".join(repr(k) for k in keys)
            self._execute(f"import pyautogui; pyautogui.hotkey({args})")

    def scroll(self, dx: int, dy: int) -> None:
        """Dispatch a wheel gesture (Backend convention: positive ``dy``
        scrolls content up / view down).

        pyautogui's convention is inverted for vertical scrolling (positive
        = view up), so ``dy`` changes sign; horizontal signs agree.
        """
        v = self._notches(dy)
        h = self._notches(dx)
        if not v and not h:
            return
        try:
            response = self._post_typed_action(
                "/input",
                {
                    "action": "scroll",
                    "horizontal_notches": h,
                    "vertical_notches": -v,
                },
            )
            self._validate_physical_receipt(response, "physical_scroll")
            return
        except _TypedRouteUnavailable:
            if not self._allow_legacy_exec:
                raise
        parts: list[str] = ["import pyautogui"]
        if v:
            parts.append(f"pyautogui.scroll({-v})")
        if h:
            parts.append(f"pyautogui.hscroll({h})")
        if len(parts) == 1:
            return
        self._execute("; ".join(parts))

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _notches(pixels: int) -> int:
        """Convert a pixel delta to wheel notches (>=1 notch when nonzero)."""
        if pixels == 0:
            return 0
        notches = round(abs(pixels) / SCROLL_PIXELS_PER_NOTCH)
        return max(1, notches) * (1 if pixels > 0 else -1)

    def _execute(self, command: str) -> None:
        """POST bare Python statements to WAA's ``/execute_windows``.

        This is the ACTION path (click/type/press/scroll): unlike the tolerant
        read path (:meth:`_execute_read`, which returns None so resolution
        falls through the ladder), an action that cannot be confirmed to have
        run must FAIL LOUDLY -- a silently dropped click/keystroke is a silent
        wrong action. So a transport failure (agent unreachable, timeout,
        connection reset) and any non-200 (a 401 from a token mismatch, a 500
        carrying the in-guest traceback) both raise ``RuntimeError``; the
        replayer treats the raise as a halt, never a no-op success.

        Raises:
            RuntimeError: On a transport failure or a non-200 response.
        """
        if not self._allow_legacy_exec:
            raise RuntimeError(
                "legacy arbitrary-exec is disabled; the typed win-agent route is required"
            )
        try:
            resp = self._session.post(
                f"{self.server_url}/execute_windows",
                json={"command": command},
                **self._request_kwargs(),
            )
        except requests.RequestException as e:
            raise RuntimeError(f"execute_windows unreachable: {e}") from e
        if resp.status_code != 200:
            raise RuntimeError(
                f"execute_windows HTTP {resp.status_code}: {resp.text[:200]}"
            )

    def probe(self) -> bool:
        """True if the WAA server answers with a valid PNG screenshot."""
        try:
            self.screenshot()
            return True
        except RuntimeError:
            return False

    def agent_capabilities(self) -> frozenset[str]:
        """Return the agent's declared bounded capability set.

        Production qualification uses this to prove the live guest is on the
        typed contract and did not expose the development-only arbitrary-exec
        route. Invalid or unreachable health metadata fails closed.
        """
        try:
            response = self._session.get(
                f"{self.server_url}/health", **self._request_kwargs()
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"win-agent health unreachable: {exc}") from exc
        if response.status_code != 200:
            raise RuntimeError(f"win-agent health HTTP {response.status_code}")
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError("win-agent health returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise RuntimeError("win-agent health returned a non-ok payload")
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) for item in capabilities
        ):
            raise RuntimeError("win-agent health omitted valid capabilities")
        return frozenset(capabilities)
