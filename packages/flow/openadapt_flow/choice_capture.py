"""Recording dropdown choices and browser dialogs (docs/CHOICES.md).

The page-side halves are spliced into the interactive recorder's closure (they
use its ``emit``, ``frameSpace`` and secret helpers); the Python halves run on
the recorder's event pump.
"""

from __future__ import annotations

from typing import Any

CHOICE_CAPTURE_JS = r"""
  // ---- dropdown choices (docs/CHOICES.md) -------------------------------
  // A choice is a click on an option of a list the previous action opened.
  // Two behaviours define it, never a widget library: the option has an
  // option role, or it is one of several same-shaped siblings (same tag,
  // shared classes); and it, or a container around it, appeared or was shown
  // after the previous pointer action. The control the previous click hit
  // must then show the option's label (checked from Python once the screen
  // settles, through choiceCommit).
  let actionSeq = 0;
  let choiceGesture = null;
  let choiceGestureSerial = 0;
  let pointerChoiceLink = {};
  const shownAt = new WeakMap();
  const builtClasses = new WeakMap();
  const choiceObserver = new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === 'childList') {
        for (const node of record.addedNodes) {
          if (node.nodeType !== 1) continue;
          shownAt.set(node, {seq: actionSeq, added: true});
          // The classes an inserted element was built with, before hover or
          // keyboard highlighting adds state classes to one of them.
          try {
            builtClasses.set(node, node.className);
            const inner = node.querySelectorAll ? node.querySelectorAll('[class]') : [];
            for (let k = 0; k < inner.length && k < 500; k++) {
              builtClasses.set(inner[k], inner[k].className);
            }
          } catch (e) {}
        }
      } else if (record.target && record.target.nodeType === 1) {
        const prior = shownAt.get(record.target);
        if (!(prior && prior.seq === actionSeq && prior.added)) {
          shownAt.set(record.target, {seq: actionSeq, added: false});
        }
      }
    }
  });
  try {
    choiceObserver.observe(document, {
      childList: true, subtree: true, attributes: true,
      attributeFilter: ['hidden', 'style', 'class', 'aria-hidden', 'aria-expanded'],
    });
  } catch (e) {}
  const OPTION_ROLES = ['option', 'treeitem'];
  const choiceNorm = (t) => String(t || '').replace(/\s+/g, ' ').trim();
  function choiceTextIsSafe(payload) {
    // Choice metadata and dialog text obey the same capture-time rule as
    // identity text: retain it exactly, or refuse before it reaches Python.
    const strings = (value) => typeof value === 'string' ? [value]
      : (value && typeof value === 'object' ? Object.values(value).flatMap(strings) : []);
    if (opaqueSecretActive || strings(payload).some(
        value => secretValueIn(value, identityMatchValues()) !== null)) {
      emit({kind: 'privacy_refusal', reason: 'captured-text'});
      return false;
    }
    return true;
  }
  function popupLike(node) {
    try {
      const role = node.getAttribute('role');
      if (['listbox', 'tree', 'grid', 'dialog', 'menu'].indexOf(role) >= 0) return true;
      const position = getComputedStyle(node).position;
      return position === 'absolute' || position === 'fixed';
    } catch (e) { return false; }
  }
  function choiceVisible(el) {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el), r = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && Number(style.opacity) !== 0 && r.width > 0 && r.height > 0
      && r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight;
  }
  function popupSnapshot() {
    const nodes = document.querySelectorAll('*'), state = new WeakMap();
    // An incomplete census proves no newly shown unlabelled popup.
    if (nodes.length > 10000) return {state: state, complete: false};
    for (const node of nodes) if (popupLike(node)) state.set(node, choiceVisible(node));
    return {state: state, complete: true};
  }
  function gestureMutation(node, gesture, requireAdded) {
    for (let cur = node; cur && cur !== document.documentElement; cur = cur.parentElement) {
      const mark = shownAt.get(cur);
      if (mark && mark.seq >= gesture.seq && (!requireAdded || mark.added)) return true;
    }
    return false;
  }
  function choiceGestureOwns(node, gesture) {
    if (!node || !gesture || !gesture.control.isConnected) return false;
    // Explicit accessibility ownership is stronger than temporal proximity.
    for (let control = gesture.control, depth = 0; control && depth < 4;
         control = control.parentElement, depth++) {
      if (control.matches('[role=combobox],[role=listbox]') && control.contains(node)) return true;
      for (const attr of ['aria-controls', 'aria-owns']) {
        const ids = String(control.getAttribute(attr) || '').split(/\s+/).filter(Boolean);
        if (ids.some(id => { const owned = document.getElementById(id); return owned && owned.contains(node); })) return true;
      }
    }
    if (!gesture.snapshot.complete) return false;
    for (let popup = node; popup && popup !== document.body; popup = popup.parentElement) {
      if (!popupLike(popup) || !choiceVisible(popup)) continue;
      const before = gesture.snapshot.state.get(popup);
      if (before === undefined && gestureMutation(popup, gesture, true)) return true;
      if (before === false && gestureMutation(popup, gesture, false)) return true;
    }
    return false;
  }
  function choiceSearchFor(el, gesture) {
    return !!el && isTextEntry(el) && !secretStates.has(el)
      && !!gesture && (el === gesture.control || choiceGestureOwns(el, gesture));
  }
  function linkChoiceSearch(el, trusted) {
    const gesture = choiceGesture;
    if (!trusted || !choiceSearchFor(el, gesture)) return null;
    const session = inputSessionFor(el);
    if (gesture.searchSession && gesture.searchSession !== session) gesture.ambiguous = true;
    gesture.searchSession = session;
    return gesture.ambiguous ? null : {gesture: gesture.token, session: session};
  }
  function choicePointerLink(target, trusted) {
    if (!trusted) { choiceGesture = null; return {}; }
    const input = target && target.closest ? target.closest('input,textarea,[contenteditable]') : null;
    const search = linkChoiceSearch(input, trusted);
    if (search) return {choice_search_gesture: search.gesture, input_session: search.session};
    choiceGesture = {
      token: DOC_ID + ':choice-gesture:' + String(++choiceGestureSerial),
      control: target, seq: actionSeq + 1, snapshot: popupSnapshot(),
      searchSession: null, ambiguous: false,
    };
    return {choice_gesture: choiceGesture.token};
  }
  function appearedThisAction(el) {
    // Inserted since the previous press, or a floating container (a popup)
    // around it changed its visibility attributes since then. Attribute
    // changes on the option itself (hover highlighting) do not count.
    for (let node = el; node && node !== document.documentElement;
         node = node.parentElement) {
      const mark = shownAt.get(node);
      if (!mark || mark.seq !== actionSeq) continue;
      if (mark.added || (node !== el && popupLike(node))) return true;
    }
    return false;
  }
  // Classes that mark a state (the highlighted or hovered option), not what
  // an option is. Leaving them in would find only the one highlighted option.
  const STATE_CLASS = /(^|[-_])(highlight|highlighted|hover|hovered|focus|focused|active|selected|current|checked|open|pressed|keyboard)$|^(is|has)-/i;
  function sharedClasses(el) {
    const parent = el.parentElement;
    if (!parent || el === document.body) return null;
    const own = Array.from(el.classList).filter(
      (c) => /^[A-Za-z_-][A-Za-z0-9_-]{0,127}$/.test(c) && !STATE_CLASS.test(c));
    const siblings = Array.from(parent.children).filter(
      (sib) => sib !== el && sib.tagName === el.tagName);
    if (!own.length) {
      // A plain list (items with no classes at all): the tag alone, and
      // replay still only looks at items that appeared after the opening.
      return siblings.length && !el.className
        && siblings.every((sib) => !sib.className) ? [] : null;
    }
    const shared = own.filter((c) => siblings.some((sib) => sib.classList.contains(c)));
    const peers = shared.length
      ? siblings.filter((sib) => shared.every((c) => sib.classList.contains(c))) : [];
    if (peers.length) return shared.slice(0, 8);
    if (siblings.length) return null;
    // The only option left (a search filtered the list): its shape is the
    // classes it was built with, which leaves out highlighting.
    const built = builtClasses.get(el);
    if (typeof built !== 'string') return null;
    const tokens = built.split(/\s+/).filter(
      (c) => c && own.indexOf(c) >= 0 && /^[A-Za-z_-][A-Za-z0-9_-]{0,127}$/.test(c));
    return tokens.length ? tokens.slice(0, 8) : null;
  }
  function optionEvidence(target) {
    try {
      if (!target || !target.closest) return null;
      let el = target.closest(OPTION_ROLES.map((r) => '[role="' + r + '"]').join(','));
      let shape = null;
      if (el) {
        shape = {role: el.getAttribute('role'), tag: el.tagName.toLowerCase(), classes: []};
      } else {
        for (let node = target, depth = 0; node && depth < 4;
             node = node.parentElement, depth++) {
          if (node === document.body || node.matches('form, table, tbody, thead')) break;
          if (node.matches('a[href], button, input, select, textarea, label')) break;
          const classes = sharedClasses(node);
          if (classes) {
            el = node;
            shape = {role: null, tag: node.tagName.toLowerCase(), classes: classes};
            break;
          }
        }
      }
      if (!el || !shape || !(appearedThisAction(el) || choiceGestureOwns(el, choiceGesture))) return null;
      const label = choiceNorm(el.innerText || el.textContent);
      if (!label || label.length > 512) return null;
      const rawValue = el.getAttribute('data-value') ?? el.getAttribute('value');
      if (!choiceTextIsSafe({label: label, value: rawValue, shape: shape})) return null;
      return {
        el: el,
        shape: shape,
        label: label,
        value: rawValue === null ? null : String(rawValue).slice(0, 512),
        disabled: el.getAttribute('aria-disabled') === 'true'
          || Array.from(el.classList).some((c) => /disabled/i.test(c)),
      };
    } catch (e) { return null; }
  }
  function namedFieldValues() {
    const out = {};
    try {
      // Keyed by name, or by id for an unnamed control (the same name/id
      // that --param takes).
      for (const el of document.querySelectorAll(
          'input[name], select[name], textarea[name], input[id], select[id], textarea[id]')) {
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (type === 'password' || secretStates.has(el)) continue;
        const key = el.name ? 'name:' + el.name : (el.id ? 'id:' + el.id : '');
        if (key && !(key in out)) out[key] = String(el.value);
      }
    } catch (e) {}
    return out;
  }
  const choiceTokens = new Map();
  let lastControl = null;
  let choiceDown = null;
  function choiceCommit(token) {
    // Called once the screen settled after the option click.
    const pending = choiceTokens.get(token);
    choiceTokens.delete(token);
    if (!pending) return null;
    const label = pending.label.toLowerCase();
    const shows = (text) => {
      text = choiceNorm(text).toLowerCase();
      if (!text) return false;
      if (text === label) return true;
      if (text.length > label.length + 40) return false;
      const at = text.indexOf(label);
      if (at < 0) return false;
      const word = /[\p{L}\p{N}]/u;
      return !word.test(at ? text[at - 1] : '')
        && !word.test(text[at + label.length] || '');
    };
    let readback = null;
    const control = pending.control;
    if (control && control.isConnected) {
      const input = control.closest && control.closest('input, textarea');
      if (input && isTextEntry(input)
          && choiceNorm(input.value).toLowerCase() === label) {
        readback = 'control_value';
      } else {
        // The label as text in the control or a box around it (a search
        // field that clears itself and shows the choice beside it).
        for (let node = control, depth = 0; node && depth < 4;
             node = node.parentElement, depth++) {
          if (node === document.body || node.tagName === 'FORM') break;
          if (shows(node.innerText)) { readback = 'control_text'; break; }
        }
      }
    }
    let shown = null;
    if (!readback && control && control.isConnected) {
      // A list whose choice writes something other than its label into a
      // text control (a calendar day writes a date): keep that exact text.
      const input = control.closest && control.closest('input, textarea');
      if (input && isTextEntry(input) && choiceNorm(input.value)
          && choiceNorm(input.value) !== choiceNorm(pending.before)
          && secretValueIn(input.value, identityMatchValues()) === null) {
        readback = 'control_value';
        shown = choiceNorm(input.value).slice(0, 512);
      }
    }
    const after = namedFieldValues();
    const changedAll = Object.keys(after).filter(
      (key) => (key in pending.fields) && pending.fields[key] !== after[key]);
    // A form field by its name wins over an unnamed control keyed by id
    // (a search box beside the field that also changed).
    const named = changedAll.filter((key) => key.startsWith('name:'));
    const changed = named.length ? named : changedAll;
    const fieldKey = changed.length === 1 ? changed[0] : null;
    const committed = {readback: readback, shows: shown,
                       field: fieldKey ? fieldKey.slice(fieldKey.indexOf(':') + 1) : null,
                       field_value: fieldKey ? after[fieldKey] : null};
    return choiceTextIsSafe(committed) ? committed : null;
  }
  function choicePointerEvent(down) {
    const token = SESSION_ID + ':choice:' + String(++choiceSerial);
    let before = '';
    try {
      const input = down.control && down.control.closest
        && down.control.closest('input, textarea');
      if (input) before = String(input.value);
    } catch (e) {}
    choiceTokens.set(token, {
      label: down.choice.label, control: down.control, fields: down.fields,
      before: before,
    });
    lastChoiceAt = performance.now();
    if (choiceTokens.size > 16) choiceTokens.delete(choiceTokens.keys().next().value);
    const out = {
      kind: 'click', x: down.x, y: down.y, sid: down.sid,
      structural: down.structural, idr: down.idr,
      choice: {
        token: token, shape: down.choice.shape, label: down.choice.label,
        value: down.choice.value, disabled: down.choice.disabled,
        opening_gesture: down.openingGesture || null,
        search_input_session: down.searchInputSession || null,
      },
    };
    if (down.sid_withheld) out.sid_withheld = down.sid_withheld;
    choiceGesture = null;
    lastControl = null;
    return out;
  }
  let choiceSerial = 0;
  let lastChoiceAt = -1e9;

  // ---- browser dialogs ----------------------------------------------------
  // alert/confirm/prompt block the page and their answer never reaches a DOM
  // event, so the answer is read where the page reads it: the call's return
  // value. A person answers the dialog in the browser; this only reports it.
  for (const kind of ['alert', 'confirm', 'prompt']) {
    try {
      const original = window[kind];
      if (typeof original !== 'function') continue;
      const wrapped = function (message) {
        const answer = original.apply(window, arguments);
        try {
          // A dialog from an iframe carries no coordinates, but its frame
          // must still be one the recording can place.
          if (frameSpace() === null) { refuseFrame(); return answer; }
          const text = String(message === undefined ? '' : message);
          if (!choiceTextIsSafe(text)) return answer;
          emitInFrameSpace({
            kind: 'dialog', type: kind,
            message: text.slice(0, 2000),
            response: kind === 'alert' ? 'accept'
              : (kind === 'confirm' ? (answer ? 'accept' : 'dismiss')
                 : (answer === null ? 'dismiss' : 'prompt')),
          });
        } catch (e) {}
        return answer;
      };
      Object.defineProperty(window, kind, {value: wrapped, configurable: true, writable: true});
    } catch (e) {}
  }

"""

NATIVE_CHOICE_JS = r"""
  function nativeChoice(el) {
    // A visible <select> the person changed is a choice of its own. A
    // hidden one is the form value behind a custom list; that list's option
    // click is the choice, and this change is only its commit.
    try {
      const fs = frameSpace();
      if (fs === null) { refuseFrame(); return; }
      if (el.multiple) { emit({kind: 'control_refusal', control: 'select'}); return; }
      const r = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      const shown = r.width >= 2 && r.height >= 2 && style.visibility !== 'hidden'
        && style.display !== 'none' && Number(style.opacity) !== 0;
      if (!shown) return;
      const option = el.selectedOptions[0];
      const label = option ? choiceNorm(option.label || option.text) : '';
      if (!label) {
        emit({kind: 'control_refusal', control: 'select'});
        return;
      }
      const value = String(el.value);
      const field = el.name || el.id || null;
      if (!choiceTextIsSafe({label: label, value: value, field: field})) return;
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const rowIdentity = structuredIdentityEvidence(cx, cy, el);
      const out = {
        kind: 'select_choice',
        x: Math.round(cx + fs.dx), y: Math.round(cy + fs.dy),
        sid: rowIdentity.sid,
        structural: frameStructural(fs, structuralTarget(cx, cy, el)),
        idr: composeRect(fs, identifierRect()),
        choice: {
          label: label, value: value.slice(0, 512),
          field: field,
          opening_gesture: choiceGesture && (choiceGesture.control === el
            || (choiceGesture.control.closest && choiceGesture.control.closest('select') === el))
            ? choiceGesture.token : null,
        },
      };
      choiceGesture = null;
      lastControl = null;
      if (rowIdentity.withheld) out.sid_withheld = rowIdentity.withheld;
      emitInFrameSpace(out);
    } catch (e) {
      emit({kind: 'control_refusal', control: 'select'});
    }
  }
"""


def attach_dialog(recorder: Any, ev: dict[str, Any]) -> None:
    """Give the browser dialog a click raised to that click's event."""

    from openadapt_flow.interactive_recorder import BrowserAttachError

    response = ev.get("response")
    if response == "prompt":
        recorder._listener_error = BrowserAttachError(
            "the page asked for typed text in a browser prompt() dialog; "
            "a typed dialog answer is not a recordable action, so "
            "recording stopped"
        )
        recorder.done = True
        return
    pending = recorder._pending_click
    if pending is not None and not recorder._same_deferred_document(pending, ev):
        recorder._listener_error = BrowserAttachError(
            "a browser dialog came from another document than its pending click; "
            "recording stopped because ownership cannot be proven"
        )
        recorder.done = True
        return
    if pending is None or response not in ("accept", "dismiss") or "dialog" in pending:
        recorder._listener_error = BrowserAttachError(
            "a browser dialog opened without exactly one click that raised "
            "it; recording stopped so its answer cannot be lost"
        )
        recorder.done = True
        return
    pending["dialog"] = {
        "response": response,
        "message": str(ev.get("message") or "")[:2000],
        "type": ev.get("type"),
    }


def commit_choice(recorder: Any, ev: dict[str, Any]) -> None:
    """Settle what an option click chose (docs/CHOICES.md).

    The page kept the control the previous press hit. Now that the screen
    settled, it reports whether that control shows the option, and which
    one named form field changed. Without that read-back the click stays
    an ordinary click.
    """

    raw = ev.get("choice")
    if not isinstance(raw, dict):
        return
    if raw.get("kind") == "native_select":
        if not raw.get("label"):
            ev.pop("choice", None)
        return
    token = raw.get("token")
    commit: Any = None
    if isinstance(token, str) and recorder.page is not None:
        for frame in list(recorder.page.frames):
            try:
                commit = frame.evaluate(
                    "(t) => { const r = window.__oaflowRecorder;"
                    " return r && r.choiceCommit ? r.choiceCommit(t) : null; }",
                    token,
                )
            except Exception:
                commit = None
            if isinstance(commit, dict):
                break
    if not isinstance(commit, dict) or commit.get("readback") not in (
        "control_text",
        "control_value",
    ):
        ev.pop("choice", None)
        return
    shape_data = raw.get("shape")
    shape: dict[str, Any] = shape_data if isinstance(shape_data, dict) else {}
    ev["choice"] = {
        "kind": "listed_option",
        "label": str(raw.get("label") or ""),
        "value": raw.get("value") if isinstance(raw.get("value"), str) else None,
        "shape": {
            "role": shape.get("role"),
            "tag": str(shape.get("tag") or ""),
            "classes": [str(c) for c in (shape.get("classes") or [])][:8],
        },
        "disabled": bool(raw.get("disabled")),
        "readback": commit["readback"],
        "shows": commit.get("shows") if isinstance(commit.get("shows"), str) else None,
        "field": commit.get("field") if isinstance(commit.get("field"), str) else None,
        "field_value": commit.get("field_value")
        if isinstance(commit.get("field_value"), str)
        else None,
        "opening_gesture": raw.get("opening_gesture")
        if isinstance(raw.get("opening_gesture"), str)
        else None,
        "search_input_session": raw.get("search_input_session")
        if isinstance(raw.get("search_input_session"), str)
        else None,
    }
