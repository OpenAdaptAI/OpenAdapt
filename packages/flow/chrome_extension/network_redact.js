// OpenAdapt optional extension: fail-closed redaction for network saves.
// Part of OpenAdapt#1872. Loaded by background.js (importScripts) and by
// the test harness. The engine re-applies the same rules on ingest
// (openadapt_flow.extension_network); this copy keeps secrets from ever
// reaching the native host.
//
// Fail closed: header values are kept only for a small allowlist; anything
// that looks like a credential is withheld; bodies are kept only when they
// parse as JSON or form data within size limits; any error drops the record.
'use strict';

(function (root) {
  const PLACEHOLDER = '<secret>';
  const SAVE_METHODS = ['POST', 'PUT', 'PATCH', 'DELETE'];
  const MAX_BODY_BYTES = 64 * 1024;
  const MAX_DEPTH = 32;
  const MAX_NODES = 5000;
  // Header values kept as-is. Everything else keeps its name only.
  const ALLOWED_HEADERS = [
    'accept',
    'content-encoding',
    'content-length',
    'content-type',
    'x-requested-with',
  ];
  // Never kept, even if a caller widens the allowlist.
  const SECRET_HEADER = /auth|cookie|token|secret|session|key|csrf|xsrf|signature|password|credential|bearer/i;
  const SECRET_KEY = new RegExp(
    [
      'pass(word|wd|phrase)?',
      'pwd',
      'secret',
      'token',
      'auth',
      'api[_-]?key',
      'apikey',
      'access[_-]?key',
      'private[_-]?key',
      'session',
      '^sid$',
      'cookie',
      'csrf',
      'xsrf',
      'signature',
      '^sig$',
      '^otp$',
      '^pin$',
      '^ssn$',
      'card[_-]?(number|num|no)',
      '^cvv$',
      '^cvc$',
      '^code$',
      'credential',
    ].join('|'),
    'i'
  );
  const SECRET_VALUE = [
    /^\s*(bearer|basic|digest|token)\s+\S+/i,
    /eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*/, // JWT
    /\b(sk|pk|rk)[-_](live|test)?[-_]?[A-Za-z0-9]{16,}/,
    /\bgh[pousr]_[A-Za-z0-9]{20,}/,
    /\bxox[abprs]-[A-Za-z0-9-]{10,}/,
    /\bAKIA[0-9A-Z]{16}\b/,
    /-----BEGIN [A-Z ]*PRIVATE KEY-----/,
  ];

  // Remember a withheld value for the final leak check. Short values and the
  // placeholder are skipped so the check cannot match ordinary output.
  function withhold(list, value) {
    if (typeof value !== 'string') return;
    if (value === PLACEHOLDER || value.length < 6) return;
    list.push(value);
  }

  // A header value and its credential-looking parts ("Bearer <token>",
  // "sid=<token>; Path=/") must not reappear elsewhere in the record.
  function withholdHeader(list, value) {
    withhold(list, value);
    for (const part of String(value).split(/[\s;,=]+/)) {
      if (part.length >= 8 && !/^[A-Za-z]+$/.test(part)) withhold(list, part);
    }
  }

  function looksLikeToken(value) {
    // Long opaque strings (hex / base64 / base64url) with letters and digits.
    const s = String(value);
    if (s.length < 32) return false;
    if (!/^[A-Za-z0-9+/=_.-]+$/.test(s)) return false;
    return /[0-9]/.test(s) && /[A-Za-z]/.test(s);
  }

  function isSecretValue(value) {
    if (typeof value !== 'string') return false;
    if (value === PLACEHOLDER) return false;
    if (looksLikeToken(value)) return true;
    return SECRET_VALUE.some((re) => re.test(value));
  }

  function isSecretKey(key) {
    return SECRET_KEY.test(String(key || ''));
  }

  function headerName(name) {
    return String(name || '').trim().toLowerCase();
  }

  function redactHeaders(headers, withheld) {
    const out = [];
    for (const h of headers || []) {
      if (!h || typeof h !== 'object') continue;
      const name = headerName(h.name);
      if (!name) continue;
      let value = h.value;
      if (value == null && h.binaryValue) value = '';
      value = value == null ? '' : String(value);
      const keep =
        ALLOWED_HEADERS.indexOf(name) >= 0 &&
        !SECRET_HEADER.test(name) &&
        !isSecretValue(value);
      if (keep) {
        out.push({ name, value });
      } else {
        withholdHeader(withheld, value);
        out.push({ name, value: PLACEHOLDER });
      }
    }
    return out;
  }

  function redactString(key, value, withheld) {
    if (isSecretKey(key) || isSecretValue(value)) {
      withhold(withheld, value);
      return PLACEHOLDER;
    }
    return value;
  }

  function redactTree(value, key, withheld, budget, depth) {
    budget.nodes += 1;
    if (budget.nodes > MAX_NODES || depth > MAX_DEPTH) throw new Error('body_too_complex');
    if (Array.isArray(value)) {
      if (key != null && isSecretKey(key)) {
        withhold(withheld, JSON.stringify(value));
        return PLACEHOLDER;
      }
      return value.map((v) => redactTree(v, key, withheld, budget, depth + 1));
    }
    if (value && typeof value === 'object') {
      if (key != null && isSecretKey(key)) {
        withhold(withheld, JSON.stringify(value));
        return PLACEHOLDER;
      }
      const out = {};
      for (const k of Object.keys(value)) {
        out[k] = redactTree(value[k], k, withheld, budget, depth + 1);
      }
      return out;
    }
    if (typeof value === 'string') return redactString(key, value, withheld);
    if (key != null && isSecretKey(key) && value != null) {
      return PLACEHOLDER;
    }
    return value;
  }

  function redactUrl(raw, withheld) {
    let u;
    try {
      u = new URL(String(raw));
    } catch (e) {
      return null;
    }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    withhold(withheld, decodeURIComponent(u.username));
    withhold(withheld, decodeURIComponent(u.password));
    const segments = u.pathname.split('/').map((seg) => {
      let decoded = seg;
      try {
        decoded = decodeURIComponent(seg);
      } catch (e) {}
      if (isSecretValue(decoded)) {
        withhold(withheld, decoded);
        return PLACEHOLDER;
      }
      return seg;
    });
    const params = [];
    for (const [k, v] of u.searchParams.entries()) {
      const value = redactString(k, v, withheld);
      params.push(encodeURIComponent(k) + '=' + (value === PLACEHOLDER ? PLACEHOLDER : encodeURIComponent(value)));
    }
    withhold(withheld, u.hash.slice(1));
    const query = params.length ? '?' + params.join('&') : '';
    return { url: u.protocol + '//' + u.host + segments.join('/') + query, origin: u.origin };
  }

  function headerValue(headers, wanted) {
    for (const h of headers || []) {
      if (h && headerName(h.name) === wanted) return String(h.value || '');
    }
    return '';
  }

  function decodeRaw(raw) {
    let total = 0;
    const parts = [];
    for (const piece of raw || []) {
      if (!piece || piece.file) return { error: 'file_upload' };
      const bytes = piece.bytes;
      if (!bytes) return { error: 'unreadable' };
      const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
      total += view.byteLength;
      if (total > MAX_BODY_BYTES) return { error: 'too_large' };
      parts.push(view);
    }
    const merged = new Uint8Array(total);
    let offset = 0;
    for (const p of parts) {
      merged.set(p, offset);
      offset += p.byteLength;
    }
    try {
      return { text: new TextDecoder('utf-8', { fatal: true }).decode(merged) };
    } catch (e) {
      return { error: 'not_utf8' };
    }
  }

  // Returns {status, body}. status is json | form | none | omitted:<reason>.
  function redactBody(requestBody, contentType, withheld) {
    if (!requestBody) return { status: 'none', body: null };
    if (requestBody.error) return { status: 'omitted:unreadable', body: null };
    const budget = { nodes: 0 };
    if (requestBody.formData && typeof requestBody.formData === 'object') {
      const form = {};
      let size = 0;
      for (const k of Object.keys(requestBody.formData)) {
        const values = (requestBody.formData[k] || []).map((v) => String(v));
        size += k.length + values.join('').length;
        form[k] = values;
      }
      if (size > MAX_BODY_BYTES) return { status: 'omitted:too_large', body: null };
      try {
        return { status: 'form', body: redactTree(form, null, withheld, budget, 0) };
      } catch (e) {
        return { status: 'omitted:too_complex', body: null };
      }
    }
    if (Array.isArray(requestBody.raw)) {
      const ct = String(contentType || '').toLowerCase();
      if (ct && ct.indexOf('json') < 0) return { status: 'omitted:unsupported_type', body: null };
      const decoded = decodeRaw(requestBody.raw);
      if (decoded.error) return { status: 'omitted:' + decoded.error, body: null };
      let parsed;
      try {
        parsed = JSON.parse(decoded.text);
      } catch (e) {
        return { status: 'omitted:unsupported_type', body: null };
      }
      try {
        return { status: 'json', body: redactTree(parsed, null, withheld, budget, 0) };
      } catch (e) {
        return { status: 'omitted:too_complex', body: null };
      }
    }
    return { status: 'omitted:unsupported_type', body: null };
  }

  function leaks(record, withheld) {
    const text = JSON.stringify(record);
    for (const value of withheld) {
      if (text.indexOf(value) >= 0 || text.indexOf(JSON.stringify(value).slice(1, -1)) >= 0) {
        return true;
      }
    }
    return false;
  }

  // details: {method, url, type, tabId, frameId, timeStamp, statusCode,
  //           requestHeaders, responseHeaders, requestBody}
  // Returns a redacted network_save evidence line, or null (dropped).
  function redactNetworkSave(details) {
    try {
      if (!details || typeof details !== 'object') return null;
      const method = String(details.method || '').toUpperCase();
      if (SAVE_METHODS.indexOf(method) < 0) return null;
      const withheld = [];
      const url = redactUrl(details.url, withheld);
      if (!url) return null;
      const requestHeaders = redactHeaders(details.requestHeaders, withheld);
      const responseHeaders = redactHeaders(details.responseHeaders, withheld);
      const body = redactBody(
        details.requestBody,
        headerValue(details.requestHeaders, 'content-type'),
        withheld
      );
      const record = {
        kind: 'network_save',
        method,
        url: url.url,
        url_origin: url.origin,
        resource_type: details.type ? String(details.type) : null,
        tab_id: Number.isInteger(details.tabId) ? details.tabId : null,
        frame_id: Number.isInteger(details.frameId) ? details.frameId : null,
        status: Number.isInteger(details.statusCode) ? details.statusCode : null,
        request_headers: requestHeaders,
        response_headers: responseHeaders,
        request_body_status: body.status,
        request_body: body.body,
        redacted: true,
        t: typeof details.timeStamp === 'number' ? details.timeStamp : Date.now(),
      };
      if (leaks(record, withheld)) return null;
      return record;
    } catch (e) {
      return null;
    }
  }

  root.OpenAdaptNetworkRedact = {
    PLACEHOLDER,
    SAVE_METHODS: SAVE_METHODS.slice(),
    ALLOWED_HEADERS: ALLOWED_HEADERS.slice(),
    isSecretKey,
    isSecretValue,
    redactNetworkSave,
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);
