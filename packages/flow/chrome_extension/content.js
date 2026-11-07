// OpenAdapt optional extension evidence (content script).
// Captures open-shadow-DOM target paths and frame context (including
// cross-origin iframes, which run their own copy) while a recording is active.
// Never sends password / declared-secret values. No remote endpoints.
'use strict';

(function () {
  const SCHEMA = 'openadapt.extension-evidence/1';
  const REDACTED_VALUE = '<secret>';
  const SECRET_NAME = /pass(word|wd|phrase)?\b|passwd|pwd|secret|private[_-]?key|^pin$|^otp$/i;
  const SECRET_AUTOCOMPLETE = /(^|\s)(current-password|new-password|one-time-code)(\s|$)/;

  let recording = false;
  let seq = 0;
  const attached = { document: 0, window: 0 };

  function isSecretField(el) {
    if (!el || el.nodeType !== 1) return false;
    const type = (el.getAttribute && el.getAttribute('type')) || el.type || '';
    if (String(type).toLowerCase() === 'password') return true;
    if (el.getAttribute && el.getAttribute('data-openadapt-secret') != null) return true;
    if (!el.getAttribute) return false;
    // Check name and id independently: a benign name must not hide a secret id.
    for (const attr of ['name', 'id']) {
      const candidate = el.getAttribute(attr);
      if (candidate && SECRET_NAME.test(String(candidate))) return true;
    }
    const autocomplete = String(el.getAttribute('autocomplete') || '').toLowerCase();
    if (SECRET_AUTOCOMPLETE.test(autocomplete)) return true;
    return false;
  }

  // The element the user actually acted on. A document-level listener sees
  // ev.target retargeted to the outermost shadow host; composedPath() still
  // exposes nodes inside open shadow roots (closed roots stay opaque).
  function eventTarget(ev) {
    if (!ev) return null;
    if (typeof ev.composedPath === 'function') {
      const path = ev.composedPath();
      for (const node of path || []) {
        if (node && node.nodeType === 1) return node;
      }
    }
    return ev.target || null;
  }

  // A URL without query, fragment or credentials: those often carry tokens.
  // Non-web schemes (data:, blob:, javascript:) keep only the scheme.
  function safeUrl(href) {
    if (!href) return null;
    let u;
    try {
      u = new URL(String(href));
    } catch (e) {
      return null;
    }
    if (u.protocol === 'http:' || u.protocol === 'https:') {
      return `${u.protocol}//${u.host}${u.pathname}`;
    }
    if (u.protocol === 'about:') return `about:${u.pathname}`;
    return u.protocol;
  }

  function frameOrigin() {
    try {
      return (typeof location !== 'undefined' && location.origin) || null;
    } catch (e) {
      return null;
    }
  }

  function isIframe() {
    try {
      return typeof window !== 'undefined' && window.top !== window;
    } catch (e) {
      return true;
    }
  }

  // True when the parent frame is a different origin (its location is
  // unreadable). The parent links its <iframe> element via frame_focus.
  function crossOriginParent() {
    if (!isIframe()) return false;
    try {
      void window.parent.location.href;
      return false;
    } catch (e) {
      return true;
    }
  }

  function frameContext() {
    return {
      frame_url: safeUrl(typeof location !== 'undefined' && location.href),
      frame_origin: frameOrigin(),
      is_iframe: isIframe(),
      cross_origin_parent: crossOriginParent(),
    };
  }

  function cssEscapeIdent(value) {
    // Minimal CSS.escape for idents used in selectors.
    return String(value).replace(/([ !"#$%&'()*+,./:;<=>?@[\\\]^`{|}~])/g, '\\$1');
  }

  function stepFor(el) {
    if (!el || el.nodeType !== 1) return null;
    const tag = (el.tagName || '').toLowerCase();
    if (!tag) return null;
    if (el.id) return `${tag}#${cssEscapeIdent(el.id)}`;
    const name = el.getAttribute && el.getAttribute('name');
    if (name) return `${tag}[name="${cssEscapeIdent(name)}"]`;
    const parent = el.parentNode;
    if (!parent || !parent.children) return tag;
    const siblings = Array.from(parent.children).filter(
      (c) => (c.tagName || '').toLowerCase() === tag
    );
    if (siblings.length <= 1) return tag;
    const index = siblings.indexOf(el) + 1;
    return `${tag}:nth-of-type(${index})`;
  }

  // Build a path that pierces open shadow roots: "host >>> inner >>> leaf".
  // Closed shadow roots are marked and stop the walk (page JS cannot read them).
  function shadowPiercingPath(el) {
    const parts = [];
    let node = el;
    let crossedShadow = false;
    let closedShadow = false;

    while (node && node.nodeType === 1) {
      const step = stepFor(node);
      if (step) parts.push(step);
      const root = node.getRootNode && node.getRootNode();
      if (root && root !== document && root.host) {
        crossedShadow = true;
        // Open shadow: continue from the host. Closed: stop.
        if (root.mode === 'closed') {
          closedShadow = true;
          parts.push('>>closed');
          break;
        }
        parts.push('>>>');
        node = root.host;
        continue;
      }
      node = node.parentElement;
    }
    parts.reverse();
    // Collapse ">>> " separators already pushed as tokens.
    const path = [];
    for (const p of parts) {
      if (p === '>>>' || p === '>>closed') {
        path.push(p);
      } else {
        path.push(p);
      }
    }
    return {
      path: path.join(' '),
      in_shadow: crossedShadow,
      closed_shadow: closedShadow,
    };
  }

  function targetEvidence(el, eventType) {
    if (!el || el.nodeType !== 1) return null;
    const walk = shadowPiercingPath(el);
    const secret = isSecretField(el);
    const evidence = {
      schema: SCHEMA,
      kind: 'target',
      event: eventType,
      tag: (el.tagName || '').toLowerCase(),
      role: (el.getAttribute && el.getAttribute('role')) || null,
      name: (el.getAttribute && (el.getAttribute('name') || el.getAttribute('aria-label'))) || null,
      id: el.id || null,
      path: walk.path,
      in_shadow: walk.in_shadow,
      closed_shadow: walk.closed_shadow,
      ...frameContext(),
      secret_field: secret,
      value: secret ? REDACTED_VALUE : null,
      seq: seq++,
      t_ms: Date.now(),
    };
    // Never put a live secret value on the wire.
    if (secret) evidence.value = REDACTED_VALUE;
    else if (typeof el.value === 'string' && el.value && eventType === 'input') {
      evidence.value = el.value;
    }
    return evidence;
  }

  // Deepest focused element, through open shadow roots.
  function deepActiveElement() {
    let el = document.activeElement;
    while (el && el.shadowRoot && el.shadowRoot.activeElement) {
      el = el.shadowRoot.activeElement;
    }
    return el;
  }

  // The parent never sees events inside a (cross-origin) child frame. When
  // focus moves into one, describe the <iframe> element from this side so the
  // engine can link the child frame's own evidence to a parent path.
  function frameFocusEvidence() {
    const el = deepActiveElement();
    if (!el || el.nodeType !== 1) return null;
    const tag = (el.tagName || '').toLowerCase();
    if (tag !== 'iframe' && tag !== 'frame') return null;
    const walk = shadowPiercingPath(el);
    const src = (el.getAttribute && el.getAttribute('src')) || el.src || '';
    let resolved = null;
    try {
      resolved = src ? new URL(src, location.href).href : null;
    } catch (e) {
      resolved = null;
    }
    const srcUrl = safeUrl(resolved);
    let childOrigin = null;
    try {
      childOrigin = resolved ? new URL(resolved).origin : null;
    } catch (e) {
      childOrigin = null;
    }
    return {
      schema: SCHEMA,
      kind: 'frame_focus',
      event: 'frame_focus',
      tag,
      id: el.id || null,
      name: (el.getAttribute && el.getAttribute('name')) || null,
      title: (el.getAttribute && el.getAttribute('title')) || null,
      path: walk.path,
      in_shadow: walk.in_shadow,
      closed_shadow: walk.closed_shadow,
      frame_src: srcUrl,
      child_origin: childOrigin && childOrigin !== 'null' ? childOrigin : null,
      cross_origin_child: Boolean(childOrigin && childOrigin !== frameOrigin()),
      ...frameContext(),
      secret_field: false,
      value: null,
      seq: seq++,
      t_ms: Date.now(),
    };
  }

  function onWindowBlur() {
    // activeElement updates synchronously when focus enters a child frame.
    emit(frameFocusEvidence());
  }

  function emit(evidence) {
    if (!recording || !evidence) return;
    try {
      chrome.runtime.sendMessage({ type: 'EXTENSION_EVIDENCE', evidence });
    } catch (e) {
      // Extension context gone: fail closed (drop).
    }
  }

  function onClick(ev) {
    emit(targetEvidence(eventTarget(ev), 'click'));
  }

  function onFocusIn(ev) {
    emit(targetEvidence(eventTarget(ev), 'focus'));
  }

  function onInput(ev) {
    emit(targetEvidence(eventTarget(ev), 'input'));
  }

  function attach() {
    if (attached.document) return;
    document.addEventListener('click', onClick, true);
    document.addEventListener('focusin', onFocusIn, true);
    document.addEventListener('input', onInput, true);
    attached.document = 3;
    if (typeof window !== 'undefined' && window.addEventListener) {
      window.addEventListener('blur', onWindowBlur, true);
      attached.window = 1;
    }
  }

  function detach() {
    if (!attached.document) return;
    document.removeEventListener('click', onClick, true);
    document.removeEventListener('focusin', onFocusIn, true);
    document.removeEventListener('input', onInput, true);
    attached.document = 0;
    if (attached.window && typeof window !== 'undefined' && window.removeEventListener) {
      window.removeEventListener('blur', onWindowBlur, true);
    }
    attached.window = 0;
  }

  function setRecording(active) {
    recording = Boolean(active);
    if (recording) attach();
    else detach();
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || typeof msg !== 'object') return;
    if (msg.type === 'SET_RECORDING') {
      setRecording(Boolean(msg.active));
    }
  });

  // Ask background for current state on load (idempotent).
  try {
    chrome.runtime.sendMessage({ type: 'CONTENT_READY' }, (resp) => {
      if (resp && typeof resp.active === 'boolean') setRecording(resp.active);
    });
  } catch (e) {}

  // Expose for the Node harness (no Chrome).
  if (typeof globalThis !== 'undefined') {
    globalThis.__openadaptExtensionContent = {
      setRecording,
      shadowPiercingPath,
      targetEvidence,
      isSecretField,
      eventTarget,
      safeUrl,
      frameContext,
      frameFocusEvidence,
      get attached() {
        return { ...attached };
      },
      get recording() {
        return recording;
      },
    };
  }
})();
