"""Run-time surface routing for multi-surface workflows.

A multi-surface workflow (``Workflow.surfaces``, docs/MULTI_SURFACE.md) runs on
several surfaces: browser tabs and popups its own steps open, and other
backends the caller supplies. :class:`SurfaceRouter` owns the map from surface
id to live backend and answers three questions for the replayer, all without a
model call:

- may this step run now (``entry_refusal``)? It refuses when the browser holds
  a page no step declared, when the active page has closed, or when the step
  names a surface other than the active one;
- did the step open exactly the pages it declared (``after_action_refusal``)?
  Each new page is claimed in creation order and must match its recorded
  opener relation, origin, URL pattern, and title pattern;
- may execution switch to this surface (``switch``)? The target must exist on
  this run, be the exact page claimed earlier (or a supplied backend of the
  declared kind), and show the origin the demonstration saw.

Every refusal is a halt. The router never picks a window for the run, and its
messages never quote a URL or a title (they can carry application content).
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from openadapt_flow.ir import ActionKind, Step, SurfaceSpec, Workflow

#: Backend class name -> the surface kinds it can serve. Read through the MRO,
#: most specific class first. A backend may instead declare ``surface_kind``.
_CLASS_SURFACE_KINDS: dict[str, frozenset[str]] = {
    "PlaywrightBackend": frozenset({"web"}),
    "WindowsBackend": frozenset({"windows"}),
    "MacOSBackend": frozenset({"macos"}),
    "LinuxBackend": frozenset({"linux"}),
    "FreeRDPBackend": frozenset({"rdp"}),
    "CitrixWorkspaceBackend": frozenset({"citrix"}),
    # The generic local client-window backend serves both RDP and Citrix.
    "RemoteDisplayBackend": frozenset({"rdp", "citrix"}),
}


def backend_surface_kinds(backend: Any) -> frozenset[str]:
    """Surface kinds ``backend`` can serve; empty when it cannot be told."""

    declared = getattr(backend, "surface_kind", None)
    if isinstance(declared, str) and declared:
        return frozenset({declared})
    for cls in type(backend).__mro__:
        kinds = _CLASS_SURFACE_KINDS.get(cls.__name__)
        if kinds:
            return kinds
    return frozenset()


def url_origin(url: Optional[str]) -> Optional[str]:
    """``scheme://host[:port]`` of an http(s) URL, lower-cased; else None."""

    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def _browser_page(backend: Any) -> Any:
    """The Playwright page a browser backend wraps, or None."""

    page = getattr(backend, "page", None)
    if page is None or not hasattr(page, "context"):
        return None
    return page


def page_has_window_opener(page: Any) -> Optional[bool]:
    """The page's own ``window.opener`` relation; None if unreadable.

    ``window.open`` keeps the opener; a ``target=_blank`` link (implicit
    ``noopener``) or ``rel=noopener`` clears it. The browser still reports the
    page that created it (``page.opener()``), which is checked separately.
    """

    try:
        return bool(page.evaluate("() => window.opener !== null"))
    except Exception:
        return None


def _page_closed(page: Any) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:
        return True


def _default_page_backend(page: Any) -> Any:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    return PlaywrightBackend(page)


class SurfaceRouter:
    """Live surface map for one run of a multi-surface workflow."""

    def __init__(
        self,
        workflow: Workflow,
        primary_backend: Any,
        supplied_backends: Optional[Mapping[str, Any]] = None,
        *,
        page_backend_factory: Callable[[Any], Any] = _default_page_backend,
        poll_interval_s: float = 0.05,
    ) -> None:
        if not workflow.surfaces:
            raise ValueError("SurfaceRouter needs a workflow that declares surfaces")
        self.specs: dict[str, SurfaceSpec] = {s.id: s for s in workflow.surfaces}
        self.primary_id = workflow.surfaces[0].id
        self.primary_backend = primary_backend
        self.active_id = self.primary_id
        self._supplied = dict(supplied_backends or {})
        unknown = sorted(set(self._supplied) - set(self.specs))
        if unknown:
            raise ValueError(
                "surface_backends names surfaces the workflow does not declare: "
                + ", ".join(unknown)
            )
        for sid in self._supplied:
            if self.specs[sid].is_page or sid == self.primary_id:
                raise ValueError(
                    f"surface {sid!r} is not a backend surface; only backend "
                    "surfaces are supplied by the caller"
                )
        self._page_backend_factory = page_backend_factory
        self._poll_interval_s = max(0.01, float(poll_interval_s))
        self.backends: dict[str, Any] = {self.primary_id: primary_backend}
        # Pages claimed for page surfaces, by surface id.
        self._claimed: dict[str, Any] = {}
        # Every page that is accounted for: the starting inventory of each
        # watched browser context, plus every claimed page.
        self._known_pages: list[Any] = []
        self._contexts: list[Any] = []
        self._watch(primary_backend)

    # -- inventory --------------------------------------------------------

    def _watch(self, backend: Any) -> None:
        """Start watching the browser context of a browser backend."""

        page = _browser_page(backend)
        if page is None:
            return
        try:
            context = page.context
        except Exception:
            return
        if any(context is known for known in self._contexts):
            self._remember(page)
            return
        self._contexts.append(context)
        try:
            pages = list(context.pages)
        except Exception:
            pages = [page]
        for existing in pages:
            self._remember(existing)
        self._remember(page)

    def _remember(self, page: Any) -> None:
        if not any(page is known for known in self._known_pages):
            self._known_pages.append(page)

    def undeclared_pages(self) -> list[Any]:
        """Open pages, in creation order, that no surface accounts for."""

        found: list[Any] = []
        for context in self._contexts:
            try:
                pages = list(context.pages)
            except Exception:
                continue
            for page in pages:
                if _page_closed(page):
                    continue
                if any(page is known for known in self._known_pages):
                    continue
                if any(page is seen for seen in found):
                    continue
                found.append(page)
        return found

    @property
    def active_backend(self) -> Any:
        return self.backends[self.active_id]

    def active_kind(self) -> str:
        return self.specs[self.active_id].kind

    # -- per-step checks ----------------------------------------------------

    def _undeclared_refusal(self, step: Step, pages: list[Any], when: str) -> str:
        return (
            f"Step '{step.id}' ({step.intent}): {len(pages)} browser page(s) "
            f"opened {when} that no step of this workflow declares (an "
            "unexpected popup, tab, or window); Flow does not choose a window "
            f"by itself, so the run halted on surface '{self.active_id}'"
        )

    def entry_refusal(self, step: Step) -> Optional[str]:
        """Why ``step`` may not start now, or None."""

        pages = self.undeclared_pages()
        if pages:
            return self._undeclared_refusal(step, pages, "before this step")
        active_page = _browser_page(self.active_backend)
        if active_page is not None and _page_closed(active_page):
            return (
                f"Step '{step.id}' ({step.intent}): the active surface "
                f"'{self.active_id}' was closed; refusing to act on another "
                "window"
            )
        if step.action is ActionKind.SWITCH_SURFACE:
            return None
        wanted = step.surface or self.primary_id
        if wanted != self.active_id:
            return (
                f"Step '{step.id}' ({step.intent}) runs on surface '{wanted}', "
                f"but the active surface is '{self.active_id}'; a surface "
                "change must be an explicit switch_surface step"
            )
        return None

    def after_action_refusal(self, step: Step, *, timeout_s: float) -> Optional[str]:
        """Claim the pages ``step`` declared; refuse anything else."""

        expected = list(step.opens)
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        pages = self.undeclared_pages()
        while len(pages) < len(expected) and time.monotonic() < deadline:
            time.sleep(self._poll_interval_s)
            pages = self.undeclared_pages()
        if not expected:
            if pages:
                return self._undeclared_refusal(step, pages, "during this step")
            return None
        if len(pages) > len(expected):
            return (
                f"Step '{step.id}' ({step.intent}) opened {len(pages)} browser "
                f"page(s) but the workflow declares {len(expected)}; Flow does "
                "not choose a window by itself, so the run halted"
            )
        if len(pages) < len(expected):
            return (
                f"Step '{step.id}' ({step.intent}) was expected to open "
                f"{len(expected)} browser page(s) ({', '.join(expected)}) but "
                f"{len(pages)} opened within {timeout_s:g}s; run halted"
            )
        for page, sid in zip(pages, expected):
            error = self._claim(step, page, self.specs[sid], deadline)
            if error is not None:
                return error
        return None

    def _claim(
        self, step: Step, page: Any, spec: SurfaceSpec, deadline: float
    ) -> Optional[str]:
        """Bind ``page`` to ``spec`` after checking that it is that page."""

        prefix = (
            f"Step '{step.id}' ({step.intent}) opened a page for surface '{spec.id}'"
        )
        opener_page = _browser_page(self.backends.get(spec.opener or "", None))
        try:
            creator = page.opener()
        except Exception:
            creator = None
        if creator is not None and creator is not opener_page:
            return (
                f"{prefix}, but the browser reports it was opened by a page "
                f"other than surface '{spec.opener}'; run halted"
            )
        assert spec.url_pattern is not None
        url_re = re.compile(spec.url_pattern)
        title_re = (
            re.compile(spec.title_pattern) if spec.title_pattern is not None else None
        )
        url_ok = origin_ok = title_ok = False
        while True:
            try:
                url = page.url
            except Exception:
                url = ""
            origin_ok = url_origin(url) == spec.origin
            url_ok = bool(url_re.fullmatch(url or ""))
            if title_re is None:
                title_ok = True
            else:
                try:
                    title = page.title()
                except Exception:
                    title = ""
                title_ok = bool(title_re.fullmatch(title or ""))
            if (origin_ok and url_ok and title_ok) or time.monotonic() >= deadline:
                break
            time.sleep(self._poll_interval_s)
        if not origin_ok:
            return (
                f"{prefix}, but it is on a different origin than recorded; run halted"
            )
        if not url_ok:
            return (
                f"{prefix}, but its URL does not match the recorded pattern; run halted"
            )
        if not title_ok:
            return (
                f"{prefix}, but its title does not match the recorded pattern; "
                "run halted"
            )
        link = page_has_window_opener(page)
        expected_link = spec.opener_link == "window_opener"
        if link is None or link != expected_link:
            return (
                f"{prefix}, but its window.opener relation differs from the "
                "recording; run halted"
            )
        self._claimed[spec.id] = page
        self._remember(page)
        self.backends[spec.id] = self._page_backend_factory(page)
        return None

    # -- switching -----------------------------------------------------------

    def switch(self, step: Step) -> tuple[Optional[Any], Optional[str]]:
        """Make ``step.surface`` active after its identity check.

        Returns ``(backend, None)`` on success and ``(None, reason)`` on a
        refusal; the active surface is unchanged on a refusal.
        """

        sid = step.surface
        prefix = f"Step '{step.id}' ({step.intent})"
        if sid is None or sid not in self.specs:
            return None, f"{prefix} switches to an undeclared surface; run halted"
        if sid == self.active_id:
            return None, f"{prefix} switches to the surface that is already active"
        spec = self.specs[sid]
        backend: Any
        if spec.is_page:
            page = self._claimed.get(sid)
            if page is None:
                return None, (
                    f"{prefix}: surface '{sid}' was not opened on this run; run halted"
                )
            if _page_closed(page):
                return None, f"{prefix}: surface '{sid}' was closed; run halted"
            backend = self.backends[sid]
        else:
            backend = self.backends.get(sid)
            if backend is None:
                backend = self._supplied.get(sid)
            if backend is None:
                return None, (
                    f"{prefix}: no backend was supplied for surface '{sid}' "
                    f"(kind {spec.kind}); bind one with --surface {sid}=KIND[:TARGET] or "
                    "the config's surfaces: map; run halted"
                )
            kinds = backend_surface_kinds(backend)
            if spec.kind not in kinds:
                told = ", ".join(sorted(kinds)) or "unknown"
                return None, (
                    f"{prefix}: surface '{sid}' is declared {spec.kind}, but the "
                    f"supplied backend serves {told}; run halted"
                )
            if not step.expect and step.surface_origin is None:
                return None, (
                    f"{prefix}: the switch to surface '{sid}' has no identity "
                    "evidence (no recorded screen text or origin), so nothing "
                    "would show the right application is in front; run halted"
                )
            self.backends[sid] = backend
            self._watch(backend)
        page = _browser_page(backend)
        if step.surface_origin is not None:
            live_origin = url_origin(getattr(page, "url", None)) if page else None
            if live_origin != step.surface_origin.lower():
                return None, (
                    f"{prefix}: surface '{sid}' is not on the origin the "
                    "demonstration switched to; run halted"
                )
        if page is not None:
            try:
                page.bring_to_front()
            except Exception as exc:
                return None, (
                    f"{prefix}: surface '{sid}' could not be brought to the "
                    f"front ({type(exc).__name__}); run halted"
                )
        self.active_id = sid
        return backend, None
