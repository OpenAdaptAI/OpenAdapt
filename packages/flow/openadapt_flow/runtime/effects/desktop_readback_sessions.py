"""Read-back sessions for ``desktop_readback``: how one session sees and
touches the screen.

Two ways to read, chosen per control:

- :class:`UiaReadSurface` reads Windows UI Automation through the in-guest
  agent's read-only ``/uia/read`` route: a control's value (edit boxes, grid
  cells) or name (labels, buttons), and the rows of a list or grid.
- :class:`PixelReadSurface` reads the screen pixels with OCR. It is the path
  for RDP, Citrix, and any application without an accessibility tree. A value
  is the text on the same line right of its label, or on the next line below
  it.

Neither surface decides what is allowed. The look-up in
:mod:`openadapt_flow.runtime.effects.desktop_readback` sends every input
through its read-only guard first.

:class:`BackendSessionDriver` opens a session from a backend factory, so the
deployment can build the read-back session from its own ``backend:``-style
config, separate from the session that types.
"""

from __future__ import annotations

import difflib
import time
from contextlib import AbstractContextManager
from typing import Any, Callable, Mapping, Optional

from openadapt_flow.runtime.effects.desktop_readback import (
    ControlRef,
    DesktopReadbackDriver,
    DesktopReadbackSessionError,
    DesktopReadbackSpec,
    DesktopReadbackVerifier,
    DesktopSessionFacts,
    Located,
    ResolvedDesktopReadback,
    Seen,
    resolve_desktop_spec,
)

#: OCR callable: PNG bytes -> lines with ``text``, ``region`` and ``confidence``.
OcrFn = Callable[[bytes], list[Any]]

#: Pixel path: how far right of a label a same-line value may start.
_MAX_RIGHT_GAP_PX = 700
#: Pixel path: how many line heights below a label its value may start.
_MAX_BELOW_LINES = 3.0


def _default_ocr(png: bytes) -> list[Any]:
    from openadapt_flow.vision.ocr import ocr

    return ocr(png)


def _collapse(text: str) -> str:
    return " ".join(str(text).split())


def _inside(region: Optional[tuple[int, int, int, int]], line: Any) -> bool:
    if region is None:
        return True
    x, y, w, h = line.region
    cx, cy = x + w / 2, y + h / 2
    rx, ry, rw, rh = region
    return rx <= cx <= rx + rw and ry <= cy <= ry + rh


def _group_rows(lines: list[Any]) -> list[list[Any]]:
    """Group OCR lines that share a screen row, top to bottom, left to right."""

    rows: list[list[Any]] = []
    for line in sorted(lines, key=lambda item: (item.region[1], item.region[0])):
        _x, y, _w, h = line.region
        center = y + h / 2
        for row in rows:
            ry, rh = row[0].region[1], row[0].region[3]
            if ry - rh * 0.25 <= center <= ry + rh * 1.25:
                row.append(line)
                break
        else:
            rows.append([line])
    return [sorted(row, key=lambda item: item.region[0]) for row in rows]


def _seen_from(lines: list[Any]) -> Seen:
    x0 = min(line.region[0] for line in lines)
    y0 = min(line.region[1] for line in lines)
    x1 = max(line.region[0] + line.region[2] for line in lines)
    y1 = max(line.region[1] + line.region[3] for line in lines)
    return Seen(
        text=" ".join(_collapse(line.text) for line in lines),
        point=(int((x0 + x1) / 2), int((y0 + y1) / 2)),
        confidence=min(float(line.confidence) for line in lines),
        ocr=True,
        parts=tuple(
            (
                _collapse(line.text),
                (
                    int(line.region[0] + line.region[2] / 2),
                    int(line.region[1] + line.region[3] / 2),
                ),
            )
            for line in lines
        ),
    )


class PixelReadSurface:
    """Read and act on a session through its screen pixels (OCR)."""

    def __init__(
        self,
        backend: Any,
        *,
        facts: DesktopSessionFacts,
        settle_s: float = 0.3,
        ocr: Optional[OcrFn] = None,
    ) -> None:
        self._backend = backend
        self.facts = facts
        self._settle_s = settle_s
        self._ocr = ocr or _default_ocr

    # -- what is on screen -------------------------------------------------------
    def _lines(self) -> list[Any]:
        return [
            line
            for line in self._ocr(self._backend.screenshot())
            if _collapse(line.text)
        ]

    def _anchors(self, ref: ControlRef, lines: list[Any]) -> list[tuple[Any, str]]:
        """Lines showing ``ref.text``, with any text after it on the same line."""

        label = _collapse(ref.text or "")
        want = label.casefold()
        found: list[tuple[Any, str]] = []
        for line in lines:
            if not _inside(ref.region, line):
                continue
            shown = _collapse(line.text)
            folded = shown.casefold()
            if folded == want or (
                difflib.SequenceMatcher(None, folded, want, autojunk=False).ratio()
                >= ref.min_ratio
            ):
                found.append((line, ""))
                continue
            # Slice the shown text by the label's own length, so casefolding
            # that changes length (``ß`` -> ``ss``) can't shift the value.
            if ref.at != "self" and len(shown) > len(label):
                head = shown[: len(label)].casefold()
                boundary = not shown[len(label)].isalnum()
                if boundary and (
                    head == want
                    or difflib.SequenceMatcher(None, head, want, autojunk=False).ratio()
                    >= ref.min_ratio
                ):
                    found.append((line, shown[len(label) :].lstrip(" :·")))
        return found

    def _value_lines(self, ref: ControlRef, anchor: Any, lines: list[Any]) -> list[Any]:
        ax, ay, aw, ah = anchor.region
        picked: list[Any] = []
        if ref.at == "right":
            for line in lines:
                if line is anchor:
                    continue
                x, y, _w, h = line.region
                center = y + h / 2
                if (
                    ay - ah * 0.5 <= center <= ay + ah * 1.5
                    and x >= ax + aw - 2
                    and x <= ax + aw + _MAX_RIGHT_GAP_PX
                    and _inside(ref.region, line)
                ):
                    picked.append(line)
            # Keep only the run of text that starts next to the label: a
            # wide gap is the next column, not more of this value.
            run: list[Any] = []
            edge = ax + aw
            for line in sorted(picked, key=lambda item: item.region[0]):
                if line.region[0] - edge > max(3 * ah, 40):
                    break
                run.append(line)
                edge = line.region[0] + line.region[2]
            return run
        below = []
        for line in lines:
            if line is anchor:
                continue
            x, y, w, _h = line.region
            # A value below a label starts under the label itself.
            if (
                ay + ah * 0.5 <= y <= ay + ah * (1 + _MAX_BELOW_LINES)
                and x + w >= ax - 20
                and x <= ax + aw
                and _inside(ref.region, line)
            ):
                below.append(line)
        if not below:
            return []
        first = min(below, key=lambda item: item.region[1])
        top, height = first.region[1], first.region[3]
        return sorted(
            [line for line in below if line.region[1] <= top + height * 0.6],
            key=lambda item: item.region[0],
        )

    # -- DesktopReadSurface ---------------------------------------------------------
    def locate(self, ref: ControlRef) -> Located:
        if ref.structured:
            return Located("unavailable")
        lines = self._lines()
        if not ref.text:
            assert ref.region is not None
            x, y, w, h = ref.region
            inside = [line for line in lines if _inside(ref.region, line)]
            text = " ".join(_collapse(line.text) for line in inside)
            return Located(
                "ok", Seen(text=text, point=(x + w // 2, y + h // 2), ocr=True)
            )
        anchors = self._anchors(ref, lines)
        if not anchors:
            return Located("missing")
        if len(anchors) > 1:
            return Located("ambiguous")
        line, _rest = anchors[0]
        x, y, w, h = line.region
        if ref.at == "right":
            point = (x + w + ref.offset_px, y + h // 2)
        elif ref.at == "below":
            point = (x + min(w // 2, 40), y + h + ref.offset_px)
        else:
            point = (x + w // 2, y + h // 2)
        return Located(
            "ok",
            Seen(
                text=_collapse(line.text),
                point=point,
                confidence=float(line.confidence),
                ocr=True,
            ),
        )

    def read(self, ref: ControlRef) -> Located:
        if ref.structured:
            return Located("unavailable")
        lines = self._lines()
        if not ref.text:
            inside = [line for line in lines if _inside(ref.region, line)]
            if not inside:
                assert ref.region is not None
                x, y, w, h = ref.region
                # Nothing read in the area: empty, or missed by OCR.
                return Located(
                    "ok",
                    Seen(
                        text="",
                        point=(x + w // 2, y + h // 2),
                        confidence=0.0,
                        ocr=True,
                    ),
                )
            rows = _group_rows(inside)
            return Located("ok", _seen_from([line for row in rows for line in row]))
        anchors = self._anchors(ref, lines)
        if not anchors:
            return Located("missing")
        if len(anchors) > 1:
            return Located("ambiguous")
        anchor, rest = anchors[0]
        if ref.at == "self":
            return Located(
                "ok",
                Seen(
                    text=_collapse(anchor.text),
                    point=(
                        anchor.region[0] + anchor.region[2] // 2,
                        anchor.region[1] + anchor.region[3] // 2,
                    ),
                    confidence=float(anchor.confidence),
                    ocr=True,
                ),
            )
        if rest:
            more = self._value_lines(ref, anchor, lines) if ref.at == "right" else []
            text = " ".join([rest] + [_collapse(line.text) for line in more])
            return Located(
                "ok",
                Seen(
                    text=text,
                    point=(anchor.region[0], anchor.region[1]),
                    confidence=min(
                        [float(anchor.confidence)]
                        + [float(line.confidence) for line in more]
                    ),
                    ocr=True,
                ),
            )
        values = self._value_lines(ref, anchor, lines)
        if not values:
            x, y, w, h = anchor.region
            # The label is there and nothing is next to it. That may be an
            # empty field or a value OCR missed, so it is never certain.
            return Located(
                "ok",
                Seen(text="", point=(x + w, y + h // 2), confidence=0.0, ocr=True),
            )
        return Located("ok", _seen_from(values))

    def rows(self, ref: ControlRef) -> Optional[list[Seen]]:
        if ref.structured:
            return None
        lines = self._lines()
        if ref.text:
            anchors = self._anchors(ref, lines)
            if len(anchors) != 1:
                return None
            anchor = anchors[0][0]
            top = anchor.region[1] + anchor.region[3]
            lines = [
                line
                for line in lines
                if line.region[1] >= top - 1 and line is not anchor
            ]
        inside = [line for line in lines if _inside(ref.region, line)]
        return [_seen_from(row) for row in _group_rows(inside)]

    def text_at(self, point: tuple[int, int]) -> Optional[str]:
        x, y = point
        hits = [
            line
            for line in self._lines()
            if line.region[0] - 4 <= x <= line.region[0] + line.region[2] + 4
            and line.region[1] - 4 <= y <= line.region[1] + line.region[3] + 4
        ]
        return " ".join(_collapse(line.text) for line in hits) or None

    def type_into(self, target: Seen, text: str) -> None:
        self._backend.click(*target.point)
        self.settle()
        self._backend.type_text(text)

    def select_all_in(self, target: Seen) -> None:
        self._backend.click(*target.point)
        self.settle()
        self._backend.press("Control+a")

    def click(self, target: Seen, *, double: bool = False) -> None:
        if double:
            self._backend.click(*target.point, double=True)
        else:
            self._backend.click(*target.point)

    def press_enter(self) -> None:
        self._backend.press("Enter")

    def settle(self) -> None:
        if self._settle_s:
            time.sleep(self._settle_s)


class UiaReadSurface(PixelReadSurface):
    """Read a Windows session through UI Automation, with OCR for controls
    declared by visible text.

    ``backend`` is a :class:`~openadapt_flow.backends.windows_backend.WindowsBackend`
    connected to an agent in the read-back session.
    """

    def _read_structural(
        self, ref: ControlRef, *, rows: bool = False
    ) -> Optional[dict]:
        from openadapt_flow.ir import StructuralLocator

        locator = StructuralLocator(
            automation_id=ref.automation_id, role=ref.role, name=ref.name
        )
        return self._backend.read_structural(locator, rows=rows)

    @staticmethod
    def _located(payload: Optional[dict], *, need_text: bool) -> Located:
        if payload is None:
            return Located("unavailable")
        match = payload.get("match")
        if match == "not_found":
            return Located("missing")
        if match != "unique":
            return Located("ambiguous")
        text = payload.get("text")
        if text is None and need_text:
            # The control is there but exposes no value or name: unreadable,
            # never an empty value.
            return Located("unavailable")
        point = payload.get("point") or [0, 0]
        return Located(
            "ok",
            Seen(text=_collapse(text or ""), point=(int(point[0]), int(point[1]))),
        )

    def locate(self, ref: ControlRef) -> Located:
        if not ref.structured:
            return super().locate(ref)
        return self._located(self._read_structural(ref), need_text=False)

    def read(self, ref: ControlRef) -> Located:
        if not ref.structured:
            return super().read(ref)
        return self._located(self._read_structural(ref), need_text=True)

    def rows(self, ref: ControlRef) -> Optional[list[Seen]]:
        if not ref.structured:
            return super().rows(ref)
        payload = self._read_structural(ref, rows=True)
        if payload is None or payload.get("match") != "unique":
            return None
        if payload.get("rows_truncated"):
            return None
        return [
            Seen(
                text=_collapse(item["text"]),
                point=(item["point"][0], item["point"][1]),
                parts=tuple(
                    (_collapse(cell["text"]), (cell["point"][0], cell["point"][1]))
                    for cell in item.get("cells") or []
                ),
            )
            for item in payload.get("rows") or []
        ]


class BackendSessionDriver:
    """Open a read-back session from a backend factory.

    ``open_backend(username, password)`` returns ``(backend, facts)``: a new
    backend for the read-back session, never the replay's, and what it can
    attest. ``uia`` picks :class:`UiaReadSurface` (the backend must offer
    ``read_structural``) over :class:`PixelReadSurface`.
    """

    def __init__(
        self,
        open_backend: Callable[[str, str], tuple[Any, DesktopSessionFacts]],
        *,
        uia: bool = False,
        settle_s: float = 0.3,
        ocr: Optional[OcrFn] = None,
    ) -> None:
        self._open_backend = open_backend
        self._uia = uia
        self._settle_s = settle_s
        self._ocr = ocr

    def open(
        self, target: ResolvedDesktopReadback, username: str, password: str
    ) -> AbstractContextManager[PixelReadSurface]:
        return _BackendSession(self, username, password)


class _BackendSession:
    def __init__(
        self, driver: BackendSessionDriver, username: str, password: str
    ) -> None:
        self._driver = driver
        self._username = username
        self._password = password
        self._backend: Any = None

    def __enter__(self) -> PixelReadSurface:
        driver = self._driver
        try:
            self._backend, facts = driver._open_backend(self._username, self._password)
        except DesktopReadbackSessionError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure means no session
            raise DesktopReadbackSessionError(
                f"could not open the read-back session ({type(exc).__name__})"
            ) from None
        finally:
            self._username = self._password = ""
        surface_type = UiaReadSurface if driver._uia else PixelReadSurface
        return surface_type(
            self._backend, facts=facts, settle_s=driver._settle_s, ocr=driver._ocr
        )

    def __exit__(self, *exc: object) -> None:
        closer = getattr(self._backend, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        self._backend = None


def session_driver_for(spec: DesktopReadbackSpec) -> BackendSessionDriver:
    """The default driver: build the read-back session from ``spec.session``.

    A network RDP session logs on with the read-back credentials, so it is a
    fresh, signed-in session. A Windows session is read through UI
    Automation; every other kind through OCR.
    """

    from openadapt_flow.backends.factory import _normalize_kind, build_backend
    from openadapt_flow.deployment import BackendConfig

    session = BackendConfig.model_validate(spec.session)
    kind = _normalize_kind(session.kind)
    network_rdp = kind == "rdp" and bool(session.rdp_host)
    uia = kind == "windows"

    def open_backend(username: str, password: str) -> tuple[Any, DesktopSessionFacts]:
        cfg = session
        if network_rdp and username:
            cfg = session.model_copy(
                update={"rdp_username": username, "rdp_password": password}
            )
        backend = build_backend(cfg)
        facts = DesktopSessionFacts(
            own_session=True,
            fresh_session=network_rdp,
            signed_in=network_rdp and bool(username),
            path="uia+ocr" if uia else "ocr",
        )
        return backend, facts

    return BackendSessionDriver(open_backend, uia=uia, settle_s=spec.settle_s)


def build_desktop_readback_verifier(
    spec: DesktopReadbackSpec,
    params: Optional[Mapping[str, object]] = None,
    *,
    timeout_s: float = 5.0,
    poll_interval_s: float = 0.5,
    driver: Optional[DesktopReadbackDriver] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> DesktopReadbackVerifier:
    """Build the verifier for one run (the ``effects.kind: desktop_readback``
    path). ``timeout_s`` is the deployment's ``effects.timeout_s``."""

    target = resolve_desktop_spec(spec, params)
    return DesktopReadbackVerifier(
        target,
        driver=driver or session_driver_for(spec),
        poll_interval_s=poll_interval_s,
        session_timeout_s=6 * spec.navigation_timeout_s + max(0.0, timeout_s),
        environ=environ,
    )


__all__ = [
    "BackendSessionDriver",
    "build_desktop_readback_verifier",
    "session_driver_for",
    "PixelReadSurface",
    "UiaReadSurface",
]
