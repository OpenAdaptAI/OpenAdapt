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

A surface is a name plus an identity check, never a live handle. The router
caches the page or backend it last bound for each surface. When that binding
is missing it binds again by identity: the one live target that passes the
check. Zero or several matches halt. A page surface rebinds only to a page
claimed on this run; only a resumed run, which claimed nothing yet, may bind a
page that was already open, and every rebind needs visual evidence. A step
never runs on a page that closed under it. The check is vision-first (the
step's anchor template and recorded screen text on the candidate's own
screenshot), with structured attributes (origin, URL and title patterns,
opener relation, the last-seen origin) added only where the target exposes
them.

Every refusal is a halt. The router never picks a window for the run, and its
messages never quote a URL or a title (they can carry application content).
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from openadapt_flow.ir import (
    ActionKind,
    Step,
    SurfaceObservation,
    SurfaceSpec,
    Workflow,
)

#: ``identity_check(backend, step)``: True when ``backend``'s live screen shows
#: the step's recorded visual identity, False when it does not, None when the
#: step carries no visual identity evidence at all.
IdentityCheck = Callable[[Any, Step], Optional[bool]]

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

    return PlaywrightBackend(page, closed_page_frame=True)


def _pump_wait(page: Any, seconds: float) -> None:
    """Wait ``seconds`` while Playwright keeps dispatching browser events.

    ``page.wait_for_timeout`` runs the sync API's event loop; ``time.sleep``
    would freeze it. Objects without it (test doubles, closed pages) sleep.
    """

    wait = getattr(page, "wait_for_timeout", None) if page is not None else None
    if callable(wait) and not _page_closed(page):
        try:
            wait(max(1.0, seconds * 1000.0))
            return
        except Exception:
            pass
    time.sleep(seconds)


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
        identity_check: Optional[IdentityCheck] = None,
        resume_active: Optional[str] = None,
        observations: Optional[Mapping[str, SurfaceObservation]] = None,
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
        self._identity_check = identity_check
        # Last-seen identity observation per surface (checkpointed; a resume
        # seeds it from the last checkpoint).
        self._observations: dict[str, SurfaceObservation] = {
            sid: obs for sid, obs in (observations or {}).items() if sid in self.specs
        }
        if resume_active is not None:
            if resume_active not in self.specs:
                raise ValueError(
                    f"the checkpoint's active surface {resume_active!r} is not "
                    "declared by this workflow"
                )
            self.active_id = resume_active
        self.backends: dict[str, Any] = {self.primary_id: primary_backend}
        # Pages claimed for page surfaces, by surface id.
        self._claimed: dict[str, Any] = {}
        # Every page claimed or bound on this run, and the surfaces bound so
        # far. A page surface rebinds only to one of these pages, except on a
        # resume before that surface is first bound: the pages the earlier
        # run opened are then only in the starting inventory.
        self._claimed_on_run: list[Any] = []
        self._bound_on_run: set[str] = set()
        self._resuming = resume_active is not None
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

    def _wait_for_new_page(self, remaining_s: float) -> None:
        """Block until a watched context opens a page, or ``remaining_s``."""

        remaining_s = max(0.0, remaining_s)
        if remaining_s <= 0:
            return
        contexts = list(self._contexts)
        if len(contexts) == 1:
            wait = getattr(contexts[0], "wait_for_event", None)
            if callable(wait):
                try:
                    wait("page", timeout=max(1.0, remaining_s * 1000.0))
                except Exception:
                    # A timeout (or a closed context): the caller re-reads
                    # the inventory and decides.
                    pass
                return
        # Several contexts, or a test double: wait one interval through a
        # live page so Playwright still dispatches, then let the caller look.
        live = next(
            (p for p in self._known_pages if not _page_closed(p)),
            None,
        )
        _pump_wait(live, min(self._poll_interval_s, remaining_s))

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
        return self.backends.get(self.active_id)

    @property
    def observations(self) -> dict[str, SurfaceObservation]:
        """The last-seen identity observation of every surface seen so far."""

        return dict(self._observations)

    def observe_active(self, after_step: Optional[str] = None) -> None:
        """Record the active surface's identity observation (no handle)."""

        backend = self.active_backend
        if backend is None:
            return
        page = _browser_page(backend)
        origin = None
        if page is not None and not _page_closed(page):
            try:
                origin = url_origin(page.url)
            except Exception:
                origin = None
        phash = None
        try:
            from io import BytesIO

            from PIL import Image

            from openadapt_flow.image_hash import perceptual_hash

            with Image.open(BytesIO(backend.screenshot())) as image:
                phash = perceptual_hash(image.convert("RGB"))
        except Exception:
            phash = None
        self._observations[self.active_id] = SurfaceObservation(
            surface=self.active_id,
            kind=self.specs[self.active_id].kind,
            observed_after_step=after_step,
            origin=origin,
            screen_phash=phash,
        )

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
        if step.action is ActionKind.SWITCH_SURFACE:
            # Leaving a page that closed itself (an OAuth or approval popup)
            # is allowed: the switch binds its target by identity and never
            # returns anywhere on its own.
            return None
        active_page = _browser_page(self.active_backend)
        if active_page is not None and _page_closed(active_page):
            # Whatever its kind: a page surface is bound to the page this run
            # claimed, and only an explicit switch_surface step moves on.
            return (
                f"Step '{step.id}' ({step.intent}): the active surface "
                f"'{self.active_id}' was closed; refusing to act on another "
                "window"
            )
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
            # Wait on the browser's own "page" event, not a sleep: with the
            # sync Playwright API a sleep dispatches nothing, so a popup the
            # click opened stays invisible to ``context.pages`` until the
            # next Playwright call, and a loaded machine misses it.
            self._wait_for_new_page(deadline - time.monotonic())
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
            # The page's URL and title update only as Playwright dispatches
            # its events; wait through the page so they do.
            _pump_wait(page, self._poll_interval_s)
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
        self._bind(spec.id, page)
        return None

    def _bind(self, sid: str, page: Any) -> None:
        self._claimed[sid] = page
        self._bound_on_run.add(sid)
        if not any(page is known for known in self._claimed_on_run):
            self._claimed_on_run.append(page)
        self._remember(page)
        self.backends[sid] = self._page_backend_factory(page)

    # -- binding by identity -------------------------------------------------

    def _bound_pages(self, *, except_id: Optional[str] = None) -> list[Any]:
        bound = [_browser_page(self.primary_backend)]
        bound.extend(page for sid, page in self._claimed.items() if sid != except_id)
        return [page for page in bound if page is not None]

    def _unbound_pages(self, sid: str) -> list[Any]:
        """Live candidate pages for ``sid`` that no other surface holds.

        Pages claimed on this run only. On a resume, before ``sid`` is first
        bound, every page Flow knows (the starting inventory) instead.
        """

        held = self._bound_pages(except_id=sid)
        cached = self._claimed.get(sid)
        if self._resuming and sid not in self._bound_on_run:
            pool = self._known_pages
        else:
            pool = self._claimed_on_run
        found: list[Any] = []
        for context in self._contexts:
            try:
                pages = list(context.pages)
            except Exception:
                continue
            for page in pages:
                if _page_closed(page) or page is cached:
                    continue
                if not any(page is known for known in pool):
                    # Undeclared (the entry check halts on it), or a page
                    # this run never claimed, such as the person's own tab.
                    continue
                if any(page is other for other in held):
                    continue
                if any(page is seen for seen in found):
                    continue
                found.append(page)
        return found

    def _structured_ok(self, page: Any, spec: SurfaceSpec) -> bool:
        """Structured attributes, used only where the page exposes them."""

        try:
            url = page.url or ""
        except Exception:
            url = ""
        if spec.origin is not None and url_origin(url) != spec.origin:
            return False
        if spec.url_pattern is not None and not re.fullmatch(spec.url_pattern, url):
            return False
        if spec.title_pattern is not None:
            try:
                title = page.title() or ""
            except Exception:
                title = ""
            if not re.fullmatch(spec.title_pattern, title):
                return False
        if spec.opener_link is not None:
            link = page_has_window_opener(page)
            if link is not None and link != (spec.opener_link == "window_opener"):
                return False
        seen = self._observations.get(spec.id)
        if seen is not None and seen.origin is not None:
            live = url_origin(url)
            if live is not None and live != seen.origin:
                return False
        return True

    def _page_passes(
        self, page: Any, spec: SurfaceSpec, step: Step, *, require_visual: bool
    ) -> bool:
        """Vision-first identity check of one candidate page for ``step``.

        The structured attributes are evaluated first only because they are
        cheap and need no screenshot; a candidate must pass both. With
        ``require_visual`` (binding a page other than the one this surface
        holds) the step must carry visual evidence and the page must show it;
        otherwise a step without visual evidence relies on the structured
        attributes alone.
        """

        if not self._structured_ok(page, spec):
            return False
        if self._identity_check is None:
            return not require_visual
        try:
            visual = self._identity_check(self._page_backend_factory(page), step)
        except Exception:
            return False
        if require_visual:
            return visual is True
        return visual is not False

    def _bind_page(
        self, sid: str, step: Step, prefix: str
    ) -> tuple[Optional[Any], Optional[str]]:
        """Bind page surface ``sid`` to the one live page that passes ``step``.

        The page this surface last used is kept while it is open and passes
        (or when no other candidate exists: the step's own identity ladder
        then checks it, as on any single page). Otherwise exactly one other
        candidate (:meth:`_unbound_pages`) must show the step's visual
        evidence; zero or several halt, and so does a held page that fails
        its check.
        """

        spec = self.specs[sid]
        cached = self._claimed.get(sid)
        cached_live = cached is not None and not _page_closed(cached)
        others = self._unbound_pages(sid)
        if cached_live and not others:
            return cached, None
        if cached_live and self._page_passes(cached, spec, step, require_visual=False):
            return cached, None
        passing = [
            page
            for page in others
            if self._page_passes(page, spec, step, require_visual=True)
        ]
        if len(passing) == 1:
            page = passing[0]
            self._bind(sid, page)
            return page, None
        if len(passing) > 1:
            return None, (
                f"{prefix}: {len(passing)} open browser pages pass the identity "
                f"check of surface '{sid}'; Flow does not choose between "
                "them, so the run halted"
            )
        if cached_live:
            return None, (
                f"{prefix}: the page surface '{sid}' holds no longer passes its "
                "identity check, and no other page this run opened does; run "
                "halted"
            )
        return None, (
            f"{prefix}: no open browser page passes the identity check of "
            f"surface '{sid}' (rebinding needs this step's visual evidence on "
            "a page this run opened); run halted"
        )

    def _bind_backend(
        self, sid: str, step: Step, prefix: str
    ) -> tuple[Optional[Any], Optional[str]]:
        """Bind backend surface ``sid``: the supplied target must pass."""

        spec = self.specs[sid]
        backend = self.backends.get(sid) or self._supplied.get(sid)
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
        return backend, None

    def route(self, step: Step) -> Optional[str]:
        """Route a non-switch step to the live target of its surface.

        Called after :meth:`entry_refusal`. In steady state this is the page
        or backend the surface already uses. After a resume (no binding yet)
        or when the surface's page closed, the target is bound again by the
        step's own identity check: exactly one match, or a halt.
        """

        sid = self.active_id
        spec = self.specs[sid]
        prefix = f"Step '{step.id}' ({step.intent})"
        if sid == self.primary_id:
            return None
        if spec.is_page:
            page, error = self._bind_page(sid, step, prefix)
            return error
        if sid in self.backends:
            return None
        backend, error = self._bind_backend(sid, step, prefix)
        if error is not None:
            return error
        visual = None
        if self._identity_check is not None:
            try:
                visual = self._identity_check(backend, step)
            except Exception:
                visual = False
        if visual is not True:
            return (
                f"{prefix}: surface '{sid}' could not be re-bound: its live "
                "target does not show this step's recorded identity"
                + (
                    " (the step carries no visual identity evidence)"
                    if visual is None
                    else ""
                )
                + "; run halted"
            )
        self.backends[sid] = backend
        self._watch(backend)
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
        if not step.expect and step.surface_origin is None:
            # Every target kind: without recorded screen text or an origin,
            # nothing would show the right page or application is in front.
            return None, (
                f"{prefix}: the switch to surface '{sid}' has no identity "
                "evidence (no recorded screen text or origin), so nothing "
                "would show the right application is in front; run halted"
            )
        spec = self.specs[sid]
        backend: Any
        if sid == self.primary_id:
            backend = self.primary_backend
            primary_page = _browser_page(backend)
            if primary_page is not None and _page_closed(primary_page):
                return None, f"{prefix}: surface '{sid}' was closed; run halted"
        elif spec.is_page:
            cached = self._claimed.get(sid)
            if cached is None and not self._unbound_pages(sid):
                return None, (
                    f"{prefix}: surface '{sid}' was not opened on this run; run halted"
                )
            page, error = self._bind_page(sid, step, prefix)
            if error is not None:
                return None, error
            if page is None or _page_closed(page):
                return None, f"{prefix}: surface '{sid}' was closed; run halted"
            backend = self.backends[sid]
        else:
            bound, error = self._bind_backend(sid, step, prefix)
            if error is not None:
                return None, error
            backend = bound
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
