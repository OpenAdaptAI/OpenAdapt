// OpenAdapt optional extension evidence (content script).
// Captures open-shadow-DOM target paths and frame context (including
// cross-origin iframes, which run their own copy) while a recording is active.
// Never sends password / declared-secret values. No remote endpoints.
'use strict';

(function () {
  const SCHEMA = 'openadapt.extension-evidence/1';
  const REDACTED_VALUE = '<secret>';
  const MFA_ATTR_RE =
    /(one[-_ ]?time[-_ ]?code|\botp\b|otp|mfa|2fa|totp|authenticator|passkey|webauthn|duo|verification[-_ ]?code|sms[-_ ]?code)/i;
  const SECRET_NAME =
    /pass(word|wd|phrase)?\b|passwd|pwd|secret|private[_ -]?key|api[_ -]?key|apikey|token|card[_ -]?(number|num|no)|cc[-_ ]?num(ber)?|cc[-_ ]?csc|cc[-_ ]?exp|(^|[_ -])ssn($|[_ -])|user[_ -]?ssn|social[-_ ]?security|(^|[_ -])cvv($|[_ -])|(^|[_ -])cvc($|[_ -])|security[-_ ]?code|^pin$|(^|[_ -])pin($|[_ -])|^otp$|(^|[_ -])otp($|[_ -])|^code$|sms[-_ ]?code|one[-_ ]?time[-_ ]?code|mfa|2fa|totp|authenticator|passkey|webauthn|duo|verification[-_ ]?code/i;
  const SECRET_AUTOCOMPLETE = /(^|\s)(current-password|new-password|one-time-code|cc-number|cc-csc|cc-exp|cc-exp-month|cc-exp-year)(\s|$)/;
  // Token-based secret field names, shared with the engine. Identifiers are
  // split at camelCase / PascalCase boundaries and non-alphanumerics, then
  // lowercased; a phrase must appear as whole consecutive tokens, so
  // patientSsn and "Card CVV" match but pinnedItems does not.
  // secret-field-vocabulary:begin (copy of openadapt_flow/secret_field_vocabulary.json)
  const SECRET_FIELD_PHRASES = [
    "password",
    "passwd",
    "pwd",
    "passphrase",
    "passcode",
    "pin",
    "ssn",
    "social security",
    "sin",
    "tax id",
    "cvv",
    "cvv2",
    "cvc",
    "cvc2",
    "csc",
    "card number",
    "cc number",
    "iban",
    "account number",
    "routing",
    "otp",
    "one time code",
    "verification code",
    "mfa",
    "totp",
    "2fa",
    "sms code",
    "api key",
    "access token",
    "refresh token",
    "secret",
    "auth",
    "token",
    "jwt",
    "bearer",
    "credential",
    "credentials",
    "cred",
    "csrf",
    "xsrf",
    "cookie",
    "signature",
    "session id",
    "sessionid",
    "private key",
    "access key",
    "apikey",
    "salt",
    "hash",
    "tfa"
  ];
  // secret-field-vocabulary:end
  const SECRET_PHRASE_TOKENS = SECRET_FIELD_PHRASES.map((p) => p.split(' '));

  function fieldTokens(value) {
    return String(value == null ? '' : value)
      .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
      .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
      .toLowerCase()
      .split(/[^a-z0-9]+/)
      .filter(Boolean);
  }

  function namesSecretField(value) {
    const tokens = fieldTokens(value);
    return SECRET_PHRASE_TOKENS.some((phrase) => {
      for (let start = 0; start + phrase.length <= tokens.length; start++) {
        if (phrase.every((word, i) => tokens[start + i] === word)) return true;
      }
      return false;
    });
  }

  let recording = false;
  let authPaused = false;
  let seq = 0;
  const attached = { document: 0, window: 0 };

  function isSecretField(el) {
    if (!el || el.nodeType !== 1) return false;
    const type = (el.getAttribute && el.getAttribute('type')) || el.type || '';
    if (String(type).toLowerCase() === 'password') return true;
    if (el.getAttribute && el.getAttribute('data-openadapt-secret') != null) return true;
    if (!el.getAttribute) return false;
    // Check name, id, and aria-label independently: a benign name must not hide
    // a secret or MFA attribute, and every field MFA_ATTR_RE flags is secret.
    for (const attr of ['name', 'id', 'aria-label']) {
      const candidate = el.getAttribute(attr);
      if (
        candidate &&
        (SECRET_NAME.test(String(candidate)) ||
          MFA_ATTR_RE.test(String(candidate)) ||
          namesSecretField(candidate))
      ) {
        return true;
      }
    }
    const autocomplete = String(el.getAttribute('autocomplete') || '').toLowerCase();
    if (SECRET_AUTOCOMPLETE.test(autocomplete) || namesSecretField(autocomplete)) return true;
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
    if (!recording || authPaused || !evidence) return;
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

  function syncCaptureListeners() {
    if (recording && !authPaused) attach();
    else detach();
  }

  function setRecording(active) {
    recording = Boolean(active);
    syncCaptureListeners();
    if (recording && !isIframe()) {
      reportAuthInterstitialIfPresent();
      startAuthObserver();
    } else if (!recording && !runActive) {
      stopAuthObserver();
    }
  }

  function setAuthPaused(paused) {
    authPaused = Boolean(paused);
    syncCaptureListeners();
  }

  // --- Side-panel Co-Pilot field highlights (cosmetic data-openadapt-* only) ---
  const VALID_COPILOT_STATES = new Set(['filled', 'conflict', 'halt']);
  const COPILOT_STYLE_ATTR = 'data-openadapt-copilot-style';
  const COPILOT_STATE_ATTR = 'data-openadapt-copilot-state';
  const COPILOT_CAPTION_ATTR = 'data-openadapt-copilot-caption';
  const COPILOT_CSS = [
    `[${COPILOT_STATE_ATTR}="filled"] {`,
    '  outline: 2px solid #059669 !important;',
    '  outline-offset: 2px !important;',
    '  box-shadow: 0 0 0 4px rgba(5, 150, 105, 0.16) !important;',
    '}',
    `[${COPILOT_STATE_ATTR}="conflict"] {`,
    '  outline: 2.5px solid #d97706 !important;',
    '  outline-offset: 2px !important;',
    '  box-shadow: 0 0 0 4px rgba(217, 119, 6, 0.22) !important;',
    '}',
    `[${COPILOT_STATE_ATTR}="halt"] {`,
    '  outline: 2.5px solid #dc2626 !important;',
    '  outline-offset: 2px !important;',
    '  box-shadow: 0 0 0 4px rgba(220, 38, 38, 0.24) !important;',
    '}',
  ].join('\n');

  const trackedHighlightElements = new Set();
  let lastAttentionScrollKey = '';
  let pendingDomReadyHighlights = null;

  function ensureCopilotStyle() {
    if (typeof document === 'undefined' || !document.createElement) return;
    if (
      typeof document.querySelector === 'function' &&
      document.querySelector(`style[${COPILOT_STYLE_ATTR}="true"]`)
    ) {
      return;
    }
    const parent = document.head || document.documentElement;
    if (!parent || typeof parent.appendChild !== 'function') return;
    const styleEl = document.createElement('style');
    styleEl.setAttribute(COPILOT_STYLE_ATTR, 'true');
    styleEl.textContent = COPILOT_CSS;
    parent.appendChild(styleEl);
  }

  function querySelectorPiercing(selector) {
    if (typeof document === 'undefined' || typeof selector !== 'string') return null;
    const trimmed = selector.trim();
    if (!trimmed) return null;
    const segments = trimmed.split(/\s*>>>\s*/);
    let root = document;
    let current = null;
    for (let i = 0; i < segments.length; i += 1) {
      const part = segments[i];
      if (!part || !root || typeof root.querySelector !== 'function') return null;
      try {
        current = root.querySelector(part);
      } catch (e) {
        return null;
      }
      if (!current) return null;
      if (i < segments.length - 1) {
        root = current.shadowRoot;
      }
    }
    return current;
  }

  function clearCopilotHighlights() {
    for (const el of trackedHighlightElements) {
      if (el && typeof el.removeAttribute === 'function') {
        el.removeAttribute(COPILOT_STATE_ATTR);
        el.removeAttribute(COPILOT_CAPTION_ATTR);
      }
    }
    trackedHighlightElements.clear();
    if (typeof document !== 'undefined' && typeof document.querySelectorAll === 'function') {
      let existing = [];
      try {
        existing = document.querySelectorAll(`[${COPILOT_STATE_ATTR}]`);
      } catch (e) {
        existing = [];
      }
      for (const el of existing) {
        if (el && typeof el.removeAttribute === 'function') {
          el.removeAttribute(COPILOT_STATE_ATTR);
          el.removeAttribute(COPILOT_CAPTION_ATTR);
        }
      }
    }
  }

  function applyCopilotHighlights(highlights, options) {
    const list = Array.isArray(highlights) ? highlights : [];
    const forceScroll = Boolean(options && options.forceScroll);

    if (typeof document !== 'undefined' && document.readyState === 'loading') {
      pendingDomReadyHighlights = { list, forceScroll };
      if (typeof document.addEventListener === 'function') {
        document.addEventListener(
          'DOMContentLoaded',
          () => {
            if (pendingDomReadyHighlights) {
              const next = pendingDomReadyHighlights;
              pendingDomReadyHighlights = null;
              applyCopilotHighlights(next.list, { forceScroll: next.forceScroll });
            }
          },
          { once: true }
        );
      }
    }

    clearCopilotHighlights();
    if (!list.length) {
      lastAttentionScrollKey = '';
      return 0;
    }

    ensureCopilotStyle();
    let applied = 0;
    let firstAttentionEl = null;
    let firstAttentionKey = '';

    for (const item of list) {
      if (!item || typeof item !== 'object') continue;
      const state = String(item.badge_state || '');
      const selector = typeof item.dom_selector === 'string' ? item.dom_selector.trim() : '';
      if (!selector || !VALID_COPILOT_STATES.has(state)) continue;
      const el = querySelectorPiercing(selector);
      if (!el || typeof el.setAttribute !== 'function') continue;

      // Never read or transmit field values; only stamp cosmetic attributes.
      el.setAttribute(COPILOT_STATE_ATTR, state);
      el.setAttribute(COPILOT_CAPTION_ATTR, String(item.caption || ''));
      trackedHighlightElements.add(el);
      applied += 1;

      if (!firstAttentionEl && (state === 'halt' || state === 'conflict')) {
        firstAttentionEl = el;
        firstAttentionKey = `${selector}:${state}`;
      }
    }

    if (
      firstAttentionEl &&
      typeof firstAttentionEl.scrollIntoView === 'function' &&
      (forceScroll || firstAttentionKey !== lastAttentionScrollKey)
    ) {
      lastAttentionScrollKey = firstAttentionKey;
      try {
        firstAttentionEl.scrollIntoView({ block: 'center', inline: 'nearest' });
      } catch (e) {}
    } else if (!firstAttentionEl) {
      lastAttentionScrollKey = '';
    }

    return applied;
  }

  // --- Operator-owned sign-in / 1Password / MFA interstitial detection ------
  // Strictly structural: NEVER reads `.value` or `.defaultValue` of any input.
  // MFA_ATTR_RE is shared with isSecretField above so every detected MFA field is redacted.
  const AUTH_ACTION_RE = /(\/login|\/signin|\/sign-in|\/sso|\/oauth|\/mfa|\/2fa|\/verify)/i;

  function detectAuthInterstitial(doc) {
    const targetDoc = doc || (typeof document !== 'undefined' ? document : null);
    if (!targetDoc || typeof targetDoc.querySelectorAll !== 'function') {
      return { detected: false, kind: null, signals: [] };
    }
    const signals = [];
    let hasMfa = false;
    let hasPassword = false;
    let hasSsoForm = false;

    let inputs = [];
    try {
      inputs = targetDoc.querySelectorAll('input');
    } catch (e) {
      inputs = [];
    }
    for (const input of inputs) {
      if (!input || typeof input.getAttribute !== 'function') continue;
      const type = String(input.getAttribute('type') || '').toLowerCase();
      if (type === 'hidden') continue;
      const ac = String(input.getAttribute('autocomplete') || '').toLowerCase();
      const name = String(input.getAttribute('name') || '');
      const id = String(input.getAttribute('id') || '');
      const aria = String(input.getAttribute('aria-label') || '');
      const meta = `${ac} ${name} ${id} ${aria}`;

      if (ac === 'one-time-code' || MFA_ATTR_RE.test(meta)) {
        hasMfa = true;
        if (signals.indexOf('mfa_input') === -1) signals.push('mfa_input');
      } else if (type === 'password') {
        hasPassword = true;
        if (signals.indexOf('password_input') === -1) signals.push('password_input');
      }
    }

    let forms = [];
    try {
      forms = targetDoc.querySelectorAll('form');
    } catch (e) {
      forms = [];
    }
    for (const form of forms) {
      if (!form || typeof form.getAttribute !== 'function') continue;
      const action = String(form.getAttribute('action') || '');
      if (AUTH_ACTION_RE.test(action)) {
        hasSsoForm = true;
        if (signals.indexOf('auth_form_action') === -1) signals.push('auth_form_action');
      }
    }

    if (hasMfa) {
      return { detected: true, kind: 'mfa', signals };
    }
    if (hasPassword) {
      return { detected: true, kind: hasSsoForm ? 'sso' : 'signin', signals };
    }
    return { detected: false, kind: null, signals };
  }

  function reportAuthInterstitialIfPresent(doc) {
    const check = detectAuthInterstitial(doc);
    if (!check.detected) return check;
    try {
      chrome.runtime.sendMessage({
        type: 'AUTH_INTERSTITIAL_DETECTED',
        kind: check.kind,
        signals: check.signals,
      });
    } catch (e) {}
    return check;
  }

  let runActive = false;
  let authObserver = null;
  let authObserverTimer = null;

  function stopAuthObserver() {
    if (authObserverTimer) {
      clearTimeout(authObserverTimer);
      authObserverTimer = null;
    }
    if (authObserver) {
      try {
        authObserver.disconnect();
      } catch (e) {}
      authObserver = null;
    }
  }

  function startAuthObserver() {
    if (isIframe() || authObserver) return;
    if (typeof MutationObserver !== 'function' || typeof document === 'undefined') return;
    const root = document.documentElement || document.body;
    if (!root) return;
    try {
      authObserver = new MutationObserver(() => {
        if (!runActive && !recording) return;
        if (authObserverTimer) return;
        authObserverTimer = setTimeout(() => {
          authObserverTimer = null;
          reportAuthInterstitialIfPresent();
        }, 250);
      });
      authObserver.observe(root, { childList: true, subtree: true });
    } catch (e) {
      authObserver = null;
    }
  }

  function setRunActive(active) {
    const next = Boolean(active);
    if (next === runActive) return;
    runActive = next;
    if (runActive && !isIframe()) {
      reportAuthInterstitialIfPresent();
      startAuthObserver();
    } else if (!runActive && !recording) {
      stopAuthObserver();
    }
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || typeof msg !== 'object') return;
    if (msg.type === 'SET_RECORDING') {
      setRecording(Boolean(msg.active));
      return;
    }
    if (msg.type === 'SET_AUTH_PAUSED') {
      setAuthPaused(Boolean(msg.paused));
      return;
    }
    if (msg.type === 'SET_RUN_ACTIVE') {
      setRunActive(Boolean(msg.active));
      return;
    }
    if (msg.type === 'COPILOT_APPLY_HIGHLIGHTS') {
      const applied = applyCopilotHighlights(msg.highlights, {
        forceScroll: Boolean(msg.forceScroll),
      });
      if (typeof sendResponse === 'function') {
        sendResponse({ ok: true, applied });
      }
      return;
    }
    if (msg.type === 'COPILOT_CHECK_AUTH') {
      const check = detectAuthInterstitial();
      if (typeof sendResponse === 'function') {
        sendResponse({ ok: true, auth: check });
      }
    }
  });

  // Ask background for current state on load (idempotent).
  // Outside an active run, do zero background DOM scanning or mutation observing.
  try {
    chrome.runtime.sendMessage({ type: 'CONTENT_READY' }, (resp) => {
      if (resp && typeof resp.authPaused === 'boolean') setAuthPaused(resp.authPaused);
      if (resp && typeof resp.active === 'boolean') setRecording(resp.active);
      if (resp && resp.runActive) setRunActive(true);
    });
  } catch (e) {}

  // Expose for the Node harness (no Chrome).
  if (typeof globalThis !== 'undefined') {
    globalThis.__openadaptExtensionContent = {
      setRecording,
      setAuthPaused,
      setRunActive,
      applyCopilotHighlights,
      detectAuthInterstitial,
      reportAuthInterstitialIfPresent,
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
      get authPaused() {
        return authPaused;
      },
    };
  }
})();
