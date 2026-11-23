"""Bind a one-time code to its field in a browser page, and hide it there.

:class:`PlaywrightOneTimeField` is the browser side of
:mod:`openadapt_flow.runtime.second_factor_step`. It never reads a field's
value, only its length:

- :meth:`~PlaywrightOneTimeField.bind` takes the focused element, requires it
  to be an ``autocomplete="one-time-code"`` input, and remembers that exact
  element under a fresh nonce in its own window.
- The field is masked in the page itself (``-webkit-text-security``) before
  any code reaches it, on the element and in a stylesheet that every new
  document in the context gets. A screenshot taken afterwards, by the
  recorder, a model-facing harness, a gate or a transition check, shows dots,
  never the code.
- :meth:`~PlaywrightOneTimeField.type_once` fills the bound element handle,
  not the page keyboard, so the whole code goes to that one element: not to
  a dialog that took focus, and not to the next box of a field split into
  one box per character (which the handoff refuses before asking anyway).
"""

from __future__ import annotations

import secrets
from typing import Any, Optional

__all__ = ["MASK_CSS", "PlaywrightOneTimeField", "one_time_field_for"]

#: Masks every one-time-code input in a document.
MASK_CSS = (
    'input[autocomplete~="one-time-code" i] { -webkit-text-security: disc !important; }'
)

#: Installed in every new document of the context (and its frames).
_MASK_INIT_JS = (
    "(() => { const add = () => { if (document.getElementById("
    "'__openadapt_one_time_mask')) return; const s = document.createElement("
    "'style'); s.id = '__openadapt_one_time_mask'; s.textContent = "
    + repr(MASK_CSS)
    + "; (document.head || document.documentElement).appendChild(s); };"
    " if (document.documentElement) add();"
    " else document.addEventListener('DOMContentLoaded', add); })();"
)

_SLOT = "__openadaptOneTimeField"

#: The state of an element: whether it is a one-time-code input, whether it
#: is empty, whether it renders masked, and its value's length. Never the
#: value itself.
_STATE_JS = r"""
const oaState = (el) => {
    if (!el || el.tagName !== 'INPUT') return {kind: 'none'};
    const tokens = (el.getAttribute('autocomplete') || '').toLowerCase()
        .split(/\s+/);
    const style = el.ownerDocument.defaultView.getComputedStyle(el);
    const security = style.getPropertyValue('-webkit-text-security') ||
        style.webkitTextSecurity || '';
    // Only plain text-entry types draw -webkit-text-security; a number or
    // date input would show the code whatever its computed style says.
    const plain = ['text', 'tel', 'password', 'search'].includes(el.type);
    return {
        kind: tokens.includes('one-time-code') && plain ? 'one-time-code' : 'other',
        empty: el.value === '',
        length: el.value.length,
        max_length: el.maxLength,
        masked: plain && ['disc', 'circle', 'square'].includes(security),
        connected: el.isConnected,
    };
};
"""

_BIND_JS = (
    "([el, nonce, slot]) => {"
    + _STATE_JS
    + r"""
    while (el && el.shadowRoot && el.shadowRoot.activeElement) {
        el = el.shadowRoot.activeElement;
    }
    const state = oaState(el);
    if (state.kind !== 'one-time-code') return state;
    el.style.setProperty('-webkit-text-security', 'disc', 'important');
    el.ownerDocument.defaultView[slot] = {nonce: nonce, el: el};
    return oaState(el);
}"""
)

#: The bound element's state, and whether it still has focus.
_CHECK_JS = (
    "([nonce, slot]) => {"
    + _STATE_JS
    + r"""
    const held = window[slot];
    // A new document (the page submitted itself) holds no binding.
    if (!held || held.nonce !== nonce) return {kind: 'gone', connected: false};
    const el = held.el;
    let active = document.activeElement;
    while (active && active.shadowRoot && active.shadowRoot.activeElement) {
        active = active.shadowRoot.activeElement;
    }
    return Object.assign(oaState(el), {focused: active === el});
}"""
)

_HANDLE_JS = (
    "([nonce, slot]) => { const held = window[slot];"
    " return held && held.nonce === nonce ? held.el : null; }"
)


class PlaywrightOneTimeField:
    """The :class:`~openadapt_flow.runtime.second_factor_step.FieldBinding`
    of a :class:`~openadapt_flow.backends.playwright_backend.PlaywrightBackend`.

    Every read returns None on any failure, which the handoff treats as not
    checked, so it refuses.
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self._nonce = secrets.token_hex(16)
        self._frame: Any = None

    # -- masking ------------------------------------------------------------

    def mask(self) -> None:
        """Mask one-time-code inputs in this page and every later document."""

        page = self._backend.page
        if not getattr(self._backend, "_one_time_mask_installed", False):
            page.context.add_init_script(script=_MASK_INIT_JS)
            self._backend._one_time_mask_installed = True
        for frame in page.frames:
            try:
                frame.evaluate(_MASK_INIT_JS)
            except Exception:  # noqa: BLE001 - the bound field is re-checked
                continue

    # -- binding ------------------------------------------------------------

    def bind(self) -> Optional[dict[str, Any]]:
        """Bind the focused element and return its state, or None."""

        state = self._backend._focused_element_call(
            "(el, arg) => (" + _BIND_JS + ")([el, arg.nonce, arg.slot])",
            arg={"nonce": self._nonce, "slot": _SLOT},
            with_arg=True,
        )
        if not isinstance(state, dict):
            return None
        if state.get("kind") == "one-time-code":
            self._frame = self._find_frame()
            if self._frame is None:
                return None
        return state

    def check(self) -> Optional[dict[str, Any]]:
        """The bound element's state now, with ``focused``; None if unread.

        A frame mid-navigation is read again once its load settles.
        """

        if self._frame is None:
            return None
        for attempt in range(2):
            try:
                state = self._frame.evaluate(_CHECK_JS, [self._nonce, _SLOT])
            except Exception:  # noqa: BLE001 - not checked, so refused
                if attempt:
                    return None
                try:
                    self._frame.wait_for_load_state(timeout=5000)
                except Exception:  # noqa: BLE001 - the second read decides
                    pass
                continue
            return state if isinstance(state, dict) else None
        return None

    def type_once(self, code: str) -> None:
        """Fill ``code`` into the bound element, once (input events fire)."""

        handle = self._frame.evaluate_handle(_HANDLE_JS, [self._nonce, _SLOT])
        element = handle.as_element()
        if element is None:
            raise RuntimeError("the bound one-time-code field is gone")
        self._backend._assert_qualification_environment_current()
        element.fill(code)

    def _find_frame(self) -> Any:
        """The one frame whose window holds this binding's nonce."""

        found = []
        for frame in self._backend.page.frames:
            try:
                held = frame.evaluate(
                    "([n, s]) => !!window[s] && window[s].nonce === n",
                    [self._nonce, _SLOT],
                )
            except Exception:  # noqa: BLE001 - a frame we cannot read
                continue
            if held is True:
                found.append(frame)
        return found[0] if len(found) == 1 else None


def one_time_field_for(backend: Any) -> Any:
    """The field binding for ``backend``, or None when it cannot bind one.

    A local browser backend can establish which element a code goes to, that
    it is empty and that it renders masked. Another backend opts in by
    offering ``one_time_field()``, returning its own binding; every other
    backend refuses a second-factor code step.
    """

    seam = getattr(backend, "one_time_field", None)
    if callable(seam):
        try:
            return seam()
        except Exception:  # noqa: BLE001 - no binding, so refused
            return None
    try:
        from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    except Exception:  # noqa: BLE001 - no browser extra installed
        return None
    if type(backend) is not PlaywrightBackend and not isinstance(
        backend, PlaywrightBackend
    ):
        return None
    if getattr(backend, "page", None) is None:
        return None
    return PlaywrightOneTimeField(backend)
