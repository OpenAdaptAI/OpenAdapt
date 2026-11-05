// OpenAdapt optional extension evidence (content script).
// Captures open-shadow-DOM target paths while a recording is active.
// Never sends password / declared-secret values. No remote endpoints.
'use strict';

(function () {
  const SCHEMA = 'openadapt.extension-evidence/1';
  const SECRET = '<secret>';
  const SECRET_NAME = /pass(word|wd|phrase)?\b|passwd|pwd|secret|private[_-]?key|^pin$|^otp$/i;

  let recording = false;
  let seq = 0;
  const attached = { document: 0, window: 0 };

  function isSecretField(el) {
    if (!el || el.nodeType !== 1) return false;
    const type = (el.getAttribute && el.getAttribute('type')) || el.type || '';
    if (String(type).toLowerCase() === 'password') return true;
    if (el.getAttribute && el.getAttribute('data-openadapt-secret') != null) return true;
    const name = (el.getAttribute && (el.getAttribute('name') || el.getAttribute('id'))) || '';
    return Boolean(name && SECRET_NAME.test(String(name)));
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
      frame_url: (typeof location !== 'undefined' && location.href) || null,
      is_iframe: (typeof window !== 'undefined' && window.top !== window) || false,
      secret_field: secret,
      value: secret ? SECRET : null,
      seq: seq++,
      t_ms: Date.now(),
    };
    // Never put a live secret value on the wire.
    if (secret) evidence.value = SECRET;
    else if (typeof el.value === 'string' && el.value && eventType === 'input') {
      evidence.value = el.value;
    }
    return evidence;
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
    emit(targetEvidence(ev.target, 'click'));
  }

  function onFocusIn(ev) {
    emit(targetEvidence(ev.target, 'focus'));
  }

  function onInput(ev) {
    emit(targetEvidence(ev.target, 'input'));
  }

  function attach() {
    if (attached.document) return;
    document.addEventListener('click', onClick, true);
    document.addEventListener('focusin', onFocusIn, true);
    document.addEventListener('input', onInput, true);
    attached.document = 3;
  }

  function detach() {
    if (!attached.document) return;
    document.removeEventListener('click', onClick, true);
    document.removeEventListener('focusin', onFocusIn, true);
    document.removeEventListener('input', onInput, true);
    attached.document = 0;
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
      get attached() {
        return { ...attached };
      },
      get recording() {
        return recording;
      },
    };
  }
})();
