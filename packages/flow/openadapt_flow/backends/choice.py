"""Choose a dropdown option by what it says, in a browser page.

The runtime half of :class:`openadapt_flow.ir.OptionChoice` (docs/CHOICES.md).
One procedure covers every dropdown family, because it relies only on what a
person relies on:

1. The control is the element at the point the runtime resolved for the step.
2. A native ``<select>`` lists its options in the DOM. Anything else opens a
   list when it is clicked; its options are the visible elements with the
   demonstrated option's shape (an ARIA role, or for a list without ARIA the
   tag and the classes the demonstrated option shared with its siblings) that
   were not already on screen before the click.
3. Options that load from the server are waited for: the list must stop
   changing before anything is decided.
4. Exactly one enabled option must show the wanted label (or carry it as its
   ``value``). None, several, or a disabled one is a refusal before the option
   is clicked.
5. The control must show the chosen label afterwards.

Nothing here names a widget library, and nothing picks an option by its
position in the list.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from openadapt_flow.backend import ActionDeliveryUncertain, ChoiceRefused
from openadapt_flow.ir import OptionChoice, choice_text

__all__ = ["BrowserChoice", "ChoiceRefused", "search_text"]

#: How long the option list must stay unchanged before it is read as final.
_STABLE_S = 0.35
_POLL_S = 0.1
#: How long the control has to show the chosen option after the click.
_READBACK_S = 3.0


def search_text(contract: OptionChoice, wanted: str) -> str:
    """What to type into the list's search box for ``wanted``."""

    if contract.query == "words" and contract.query_words is not None:
        first, count = contract.query_words
        words = wanted.split()
        if len(words) >= first + count:
            return " ".join(words[first : first + count])
    return wanted


# Shared page-side helpers. ``shape`` is ``{role, tag, classes}``.
_HELPERS = r"""
const norm = (t) => String(t || '').replace(/\s+/g, ' ').trim().toLowerCase();
const visible = (el) => {
  if (!el || !el.isConnected) return false;
  const style = getComputedStyle(el);
  if (style.visibility === 'hidden' || style.display === 'none'
      || Number(style.opacity) === 0) return false;
  const r = el.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return false;
  return r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight;
};
const matchesShape = (el, shape) => {
  if (shape.role) return el.getAttribute('role') === shape.role;
  if (el.tagName.toLowerCase() !== shape.tag) return false;
  return shape.classes.every((c) => el.classList.contains(c));
};
const queryShape = (shape) => shape.role
  ? '[role="' + shape.role + '"]'
  : shape.tag + shape.classes.map((c) => '.' + CSS.escape(c)).join('');
const optionLabel = (el) => norm(el.innerText || el.textContent || '');
const optionValue = (el) => {
  const v = el.getAttribute('data-value') ?? el.getAttribute('value');
  return v === null ? null : String(v);
};
const optionDisabled = (el) => {
  if (el.matches(':disabled') || el.closest('[aria-disabled=true],[inert],fieldset[disabled]')) return true;
  for (let node = el; node; node = node.parentElement) {
    if (Array.from(node.classList).some((c) => /disabled/i.test(c))) return true;
  }
  return false;
};
const shownText = (root, shape) => {
  // The text a person sees in ``root``: visible text nodes only, and never
  // the text of an option list that sits inside the control.
  let out = '';
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const parent = node.parentElement;
    if (!parent || !visible(parent)) continue;
    let inOption = false;
    for (let el = parent; el && el !== root.parentElement; el = el.parentElement) {
      if (shape && matchesShape(el, shape)) { inOption = true; break; }
      if (el.matches && el.matches('[role=listbox],[role=tree]') && el !== root) {
        inOption = true; break;
      }
    }
    if (!inOption) out += ' ' + node.nodeValue;
  }
  return norm(out);
};
const showsLabel = (text, label) => {
  if (!text || !label) return false;
  if (text === label) return true;
  // The label as a whole phrase in a short control text ("Cardiology x").
  if (text.length > label.length + 40) return false;
  const at = text.indexOf(label);
  if (at < 0) return false;
  const before = at === 0 ? '' : text[at - 1];
  const after = text[at + label.length] || '';
  const word = /[\p{L}\p{N}]/u;
  return !word.test(before) && !word.test(after);
};
"""

_NATIVE_CENSUS_JS = (
    "(el) => {"
    + _HELPERS
    + r"""
  const sel = el && el.closest ? el.closest('select') : null;
  if (!sel || sel.multiple) return null;
  return {
    disabled: sel.matches(':disabled') || !!sel.closest('[inert],[aria-disabled=true]'),
    value: sel.value,
    options: Array.from(sel.options).map((o) => ({
      value: o.value, label: norm(o.label || o.text),
      disabled: o.disabled || !!o.closest('optgroup[disabled]'),
      placeholder: o.value === '',
    })),
  };
}"""
)

_NATIVE_READ_JS = (
    "(el) => {"
    + _HELPERS
    + r"""
  const sel = el && el.closest ? el.closest('select') : null;
  if (!sel || !sel.isConnected) return null;
  const o = sel.selectedOptions[0];
  return {value: sel.value, label: o ? norm(o.label || o.text) : ''};
}"""
)

_MARK_PRE_JS = (
    "([shape, mark]) => {"
    + _HELPERS
    + r"""
  let n = 0;
  for (const el of document.querySelectorAll(queryShape(shape))) {
    if (matchesShape(el, shape) && visible(el)) { el.setAttribute(mark, '1'); n++; }
  }
  return n;
}"""
)

_UNMARK_JS = r"""(mark) => {
  for (const el of document.querySelectorAll('[' + mark + ']')) el.removeAttribute(mark);
}"""

_CANDIDATES_JS = (
    "([shape, mark]) => {"
    + _HELPERS
    + r"""
  const out = [];
  for (const el of document.querySelectorAll(queryShape(shape))) {
    if (!matchesShape(el, shape) || el.hasAttribute(mark) || !visible(el)) continue;
    out.push({label: optionLabel(el), value: optionValue(el),
              disabled: optionDisabled(el)});
  }
  return out;
}"""
)

_UNIQUE_CANDIDATE_JS = (
    "([shape, mark, want, wanted]) => {"
    + _HELPERS
    + r"""
  // Resolve again by meaning, never by the old position in the census.
  // The list may have changed between settling and this lookup.
  const matches = Array.from(document.querySelectorAll(queryShape(shape))).filter(
    (el) => matchesShape(el, shape) && !el.hasAttribute(mark) && visible(el)
      && (optionLabel(el) === want || optionValue(el) === wanted));
  return matches.length === 1 && !optionDisabled(matches[0]) ? matches[0] : null;
}"""
)

_FOCUSED_TEXT_ENTRY_JS = r"""() => {
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  if (!el) return false;
  const tag = el.tagName.toLowerCase();
  const textInput = tag === 'textarea' || (tag === 'input' && ![
    'button','checkbox','color','file','hidden','image','radio','range','reset','submit',
  ].includes((el.getAttribute('type') || 'text').toLowerCase()));
  if (textInput) { try { el.select(); } catch (e) {} return true; }
  return el.isContentEditable === true;
}"""

_READBACK_JS = (
    "([el, mode, label, shape]) => {"
    + _HELPERS
    + r"""
  if (!el || !el.isConnected) return {ok: false, gone: true};
  if (mode === 'control_value') {
    const input = el.closest('input, textarea') || el;
    return {ok: norm(input.value) === label};
  }
  let node = el;
  for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
    if (node === document.body || node.tagName === 'FORM') break;
    if (showsLabel(shownText(node, shape), label)) return {ok: true};
  }
  return {ok: false};
}"""
)


_READBACK_CALL_JS = "(el, args) => (" + _READBACK_JS + ")([el, ...args])"


_COMMIT_FIELD_JS = r"""(el, field) => {
  const scope = el.closest('form') || document;
  const matches = Array.from(scope.querySelectorAll('input,select,textarea')).filter(
    node => node.name === field || (!node.name && node.id === field));
  return matches.length === 1 ? matches[0] : null;
}"""

_FIELD_STATE_JS = (
    "el => {"
    + _HELPERS
    + r"""
  if (!el || !el.isConnected) return null;
  const selected = el.tagName === 'SELECT' ? el.selectedOptions[0] : null;
  return {value: String(el.value || ''),
    selectedLabel: selected ? norm(selected.label || selected.text) : null,
    multiple: el.multiple === true,
    disabled: el.matches(':disabled'),
    options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => ({
      label: norm(o.label || o.text), value: o.value,
      disabled: o.disabled || !!o.closest('optgroup[disabled]')
    })) : null};
}"""
)

_FIELD_IS_QUERY_JS = r"""el => {
  let active = document.activeElement;
  while (active && active.shadowRoot && active.shadowRoot.activeElement) {
    active = active.shadowRoot.activeElement;
  }
  return active === el;
}"""


@dataclass
class _ChoiceProof:
    scope: Any
    handle: Any
    point: tuple[float, float]
    contract: OptionChoice
    label: str
    value: Optional[str]
    field_handle: Any = None


class BrowserChoice:
    """Run :class:`OptionChoice` contracts on one Playwright page."""

    def __init__(self, page: Any, frame_point: Any) -> None:
        self._page = page
        self._frame_point = frame_point
        self._proof: Optional[_ChoiceProof] = None

    # -- entry ---------------------------------------------------------------

    def choose(
        self, contract: OptionChoice, wanted: str, point: tuple[int, int]
    ) -> dict[str, Any]:
        self._proof = None
        if not choice_text(wanted):
            raise ChoiceRefused("choice_missing", "the wanted value is empty")
        located = self._frame_point(int(point[0]), int(point[1]))
        if located is None:
            raise ChoiceRefused("choice_control_missing", "no control at the point")
        scope = located.scope
        handle = scope.evaluate_handle(
            "([x, y]) => document.elementFromPoint(x, y)", [located.x, located.y]
        ).as_element()
        if handle is None:
            raise ChoiceRefused("choice_control_missing", "no control at the point")
        if contract.kind == "native_select":
            return self._native(scope, handle, contract, wanted, located)
        return self._listed(scope, handle, contract, wanted, point, located)

    def confirm(self) -> bool:
        """Read the control once more on the settled page; consume the proof."""

        proof, self._proof = self._proof, None
        if proof is None:
            return False
        try:
            if proof.contract.kind == "native_select":
                observed = proof.handle.evaluate(_NATIVE_READ_JS)
                return (
                    isinstance(observed, dict)
                    and observed.get("value") == proof.value
                    and observed.get("label") == proof.label
                )
            return bool(self._readback(proof.scope, proof.handle, proof).get("ok"))
        except Exception:
            return False

    # -- native select -------------------------------------------------------

    def _native(
        self,
        scope: Any,
        handle: Any,
        contract: OptionChoice,
        wanted: str,
        located: Any,
    ) -> dict[str, Any]:
        want = choice_text(wanted)
        deadline = time.monotonic() + contract.settle_s
        last: Any = None
        stable_since = time.monotonic()
        census: Optional[dict[str, Any]] = None
        while True:
            census = handle.evaluate(_NATIVE_CENSUS_JS)
            if census is None:
                raise ChoiceRefused(
                    "choice_control_missing", "the control is not a select"
                )
            key = json.dumps(census, sort_keys=True)
            now = time.monotonic()
            if key != last:
                last, stable_since = key, now
            settled = now - stable_since >= _STABLE_S and not census["disabled"]
            matches = [
                o
                for o in census["options"]
                if not o["placeholder"] and (o["label"] == want or o["value"] == wanted)
            ]
            if settled and matches:
                break
            if now >= deadline:
                if now - stable_since < _STABLE_S:
                    raise ChoiceRefused(
                        "choice_unsettled", "the option list did not stop changing"
                    )
                break
            time.sleep(_POLL_S)
        assert census is not None
        if census["disabled"]:
            raise ChoiceRefused("choice_disabled", "the control stayed disabled")
        self._decide(matches)
        chosen = matches[0]
        try:
            delivered = self._commit_native(handle, chosen, want, wanted)
        except ActionDeliveryUncertain:
            raise
        except Exception as exc:
            raise ActionDeliveryUncertain(
                operation="choice_select",
                native=False,
                target_fingerprint=None,
                cause_type=type(exc).__name__,
            ) from exc
        if not delivered:
            raise ChoiceRefused(
                "choice_changed", "the option changed after the list settled"
            )
        observed = handle.evaluate(_NATIVE_READ_JS)
        if (
            not isinstance(observed, dict)
            or observed.get("value") != chosen["value"]
            or observed.get("label") != chosen["label"]
        ):
            raise ChoiceRefused("choice_unverified", "the select shows another option")
        self._proof = _ChoiceProof(
            scope,
            handle,
            (located.x, located.y),
            contract,
            chosen["label"],
            chosen["value"],
        )
        return {"label": chosen["label"], "value": chosen["value"]}

    @staticmethod
    def _commit_native(
        handle: Any, chosen: dict[str, Any], want: str, wanted: str
    ) -> bool:
        # DOM selection does not need focus. Avoid unrelated focus/blur
        # callbacks, and check again after input before emitting change.
        outcome = handle.evaluate(
            "(el, args) => {"
            + _HELPERS
            + r"""
              const [label, value, want, wanted] = args;
              const sel = el.closest('select');
              const matchingOption = () => {
                if (!sel || !sel.isConnected || sel.multiple || sel.matches(':disabled')
                    || sel.closest('[inert],[aria-disabled=true]')) return null;
                const matches = Array.from(sel.options).filter(o => o.value !== ''
                  && (norm(o.label || o.text) === want || o.value === wanted));
                if (matches.length !== 1) return null;
                const option = matches[0];
                return option.disabled || option.closest('optgroup[disabled]')
                  || option.value !== value || norm(option.label || option.text) !== label
                  ? null : option;
              };
              const option = matchingOption();
              if (!option) return false;
              option.selected = true;
              const stillSelected = () => matchingOption() === option
                && sel.selectedOptions.length === 1 && sel.selectedOptions[0] === option
                && sel.value === value;
              if (!stillSelected()) return false;
              sel.dispatchEvent(new Event('input', {bubbles: true}));
              if (!stillSelected()) return 'input_changed';
              sel.dispatchEvent(new Event('change', {bubbles: true}));
              return true;
            }""",
            [chosen["label"], chosen["value"], want, wanted],
        )
        if outcome == "input_changed":
            raise ActionDeliveryUncertain(
                operation="choice_select",
                native=False,
                target_fingerprint=None,
                cause_type="NativeChoiceChangedAfterInput",
            )
        if not isinstance(outcome, bool):
            raise ValueError("invalid native choice commit result")
        return outcome

    # -- listed options ------------------------------------------------------

    def _listed(
        self,
        scope: Any,
        handle: Any,
        contract: OptionChoice,
        wanted: str,
        point: tuple[int, int],
        located: Any,
    ) -> dict[str, Any]:
        assert contract.option is not None
        if not contract.commit_field:
            raise ChoiceRefused(
                "choice_commit_unknown", "no independent form field was recorded"
            )
        field = handle.evaluate_handle(
            _COMMIT_FIELD_JS, contract.commit_field
        ).as_element()
        if field is None:
            raise ChoiceRefused(
                "choice_commit_unknown", "the committed field is missing or ambiguous"
            )
        initial_field = field.evaluate(_FIELD_STATE_JS)
        if not isinstance(initial_field, dict) or initial_field["multiple"]:
            raise ChoiceRefused(
                "choice_commit_unknown", "a single committed field is required"
            )
        if initial_field["disabled"]:
            raise ChoiceRefused("choice_disabled", "the committed field is disabled")
        shape = contract.option.model_dump(mode="json")
        shape["classes"] = list(shape["classes"])
        mark = "data-oaflow-choice-" + uuid.uuid4().hex[:12]
        want = choice_text(wanted)
        try:
            scope.evaluate(_MARK_PRE_JS, [shape, mark])
            try:
                self._page.mouse.click(int(point[0]), int(point[1]))
            except Exception as exc:
                raise ActionDeliveryUncertain(
                    operation="choice_open",
                    native=False,
                    target_fingerprint=None,
                    cause_type=type(exc).__name__,
                ) from exc
            if contract.query != "none":
                self._page.wait_for_timeout(150)
                if not scope.evaluate(_FOCUSED_TEXT_ENTRY_JS):
                    self._close()
                    raise ChoiceRefused(
                        "choice_search_missing", "no search box took focus"
                    )
                if field.evaluate(_FIELD_IS_QUERY_JS):
                    self._close()
                    raise ChoiceRefused(
                        "choice_commit_unknown",
                        "query text is not an independent commit",
                    )
                self._page.keyboard.type(search_text(contract, wanted))
            try:
                matches = self._settled_matches(
                    scope, shape, mark, want, wanted, contract
                )
                self._decide(matches)
            except ChoiceRefused:
                self._close()
                raise
            option = scope.evaluate_handle(
                _UNIQUE_CANDIDATE_JS, [shape, mark, want, wanted]
            ).as_element()
            if option is None:
                self._close()
                raise ChoiceRefused(
                    "choice_changed", "the option changed after the list settled"
                )
            chosen = matches[0]
            before = field.evaluate(_FIELD_STATE_JS)
            if not isinstance(before, dict):
                self._close()
                raise ChoiceRefused(
                    "choice_commit_unknown", "the committed field left the page"
                )
            try:
                expected_value = self._expected_commit_value(
                    contract, wanted, chosen, before
                )
            except ChoiceRefused:
                self._close()
                raise
            try:
                option.click(timeout=5000)
            except Exception as exc:
                raise ActionDeliveryUncertain(
                    operation="choice_click",
                    native=False,
                    target_fingerprint=None,
                    cause_type=type(exc).__name__,
                ) from exc
        finally:
            try:
                scope.evaluate(_UNMARK_JS, mark)
            except Exception:
                pass
        proof = _ChoiceProof(
            scope,
            handle,
            (located.x, located.y),
            contract,
            chosen["label"],
            expected_value,
            field_handle=field,
        )
        deadline = time.monotonic() + _READBACK_S
        while True:
            observed = self._readback(scope, handle, proof)
            if observed.get("ok"):
                break
            if time.monotonic() >= deadline:
                raise ChoiceRefused("choice_unverified", "the control does not show it")
            time.sleep(_POLL_S)
        self._proof = proof
        return {"label": chosen["label"], "value": chosen["value"]}

    @staticmethod
    def _expected_commit_value(
        contract: OptionChoice,
        wanted: str,
        chosen: dict[str, Any],
        before: dict[str, Any],
    ) -> str:
        """Resolve an exact expected value before the option's input edge."""

        if before["multiple"] or before["disabled"]:
            raise ChoiceRefused(
                "choice_commit_unknown", "a single enabled field is required"
            )
        if contract.shows is not None:
            return contract.shows
        if chosen["value"] is not None:
            return str(chosen["value"])
        options = before.get("options")
        if isinstance(options, list):
            matches = [
                option
                for option in options
                if option["label"] == chosen["label"]
                and (chosen["value"] is None or option["value"] == chosen["value"])
            ]
            BrowserChoice._decide(matches)
            return str(matches[0]["value"])
        if (
            choice_text(wanted) == choice_text(contract.label)
            and contract.value is not None
        ):
            return contract.value
        raise ChoiceRefused(
            "choice_commit_unknown", "no exact value mapping is visible for this label"
        )

    def _settled_matches(
        self,
        scope: Any,
        shape: dict[str, Any],
        mark: str,
        want: str,
        wanted: str,
        contract: OptionChoice,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + contract.settle_s
        last: Any = None
        stable_since = time.monotonic()
        while True:
            candidates = scope.evaluate(_CANDIDATES_JS, [shape, mark]) or []
            now = time.monotonic()
            key = tuple((c["label"], c["value"], c["disabled"]) for c in candidates)
            if key != last:
                last, stable_since = key, now
            matches = [
                c
                for c in candidates
                if c["label"] == want
                or (c["value"] is not None and c["value"] == wanted)
            ]
            if now - stable_since >= _STABLE_S and candidates and matches:
                break
            if now >= deadline:
                if now - stable_since < _STABLE_S:
                    raise ChoiceRefused(
                        "choice_unsettled", "the option list did not stop changing"
                    )
                break
            time.sleep(_POLL_S)
        return matches

    @staticmethod
    def _decide(matches: list[dict[str, Any]]) -> None:
        if not matches:
            raise ChoiceRefused("choice_missing", "no option shows the wanted value")
        if len(matches) > 1:
            raise ChoiceRefused("choice_ambiguous", f"{len(matches)} options show it")
        if matches[0]["disabled"]:
            raise ChoiceRefused("choice_disabled", "the option is disabled")

    def _readback(self, scope: Any, handle: Any, proof: _ChoiceProof) -> dict[str, Any]:
        shape = None
        if proof.contract.option is not None:
            shape = proof.contract.option.model_dump(mode="json")
            shape["classes"] = list(shape["classes"])
        expected = (
            choice_text(proof.contract.shows) if proof.contract.shows else proof.label
        )
        observed = handle.evaluate(
            _READBACK_CALL_JS, [proof.contract.readback, expected, shape]
        )
        if not isinstance(observed, dict) or not observed.get("ok"):
            return {"ok": False}
        # Never reacquire a replaced control by its old point: that is a
        # different observation and may belong to another form or record.
        if proof.field_handle is None:
            return {"ok": False}
        committed = proof.field_handle.evaluate(_FIELD_STATE_JS)
        if not isinstance(committed, dict):
            return {"ok": False}
        if committed["multiple"] or committed["value"] != proof.value:
            return {"ok": False}
        return {
            "ok": committed["selectedLabel"] is None
            or committed["selectedLabel"] == proof.label
        }

    def _close(self) -> None:
        try:
            self._page.keyboard.press("Escape")
        except Exception:
            pass
