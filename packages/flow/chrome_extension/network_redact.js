// OpenAdapt optional extension: fail-closed redaction for network saves.
// Part of OpenAdapt#1872. Loaded by background.js (importScripts) and by
// the test harness. The engine re-applies the same rules on ingest
// (openadapt_flow.extension_network); this copy keeps secrets from ever
// reaching the native host.
//
// Fail closed: header values are kept only for a small allowlist; anything
// that looks like a credential is withheld; bodies are kept only when they
// parse as JSON or form data within size limits; any error drops the record.
// Every withheld value is remembered by its complete scalar identity (the
// whole credential, each credential-header part including alphabetic Bearer
// tokens, cookie/parameter values, decoded Basic credentials, and the scalar
// leaves of a withheld subtree). The final check drops the record if any of
// them appears in its JSON text or its percent-decoded canonical form, and
// drops it if that bookkeeping exceeds its limit.
'use strict';

(function (root) {
  const PLACEHOLDER = '<secret>';
  const SAVE_METHODS = ['POST', 'PUT', 'PATCH', 'DELETE'];
  const MAX_BODY_BYTES = 64 * 1024;
  const MAX_DEPTH = 32;
  const MAX_NODES = 5000;
  // Upper bound on remembered withheld identities per record; past it the
  // record is dropped rather than checked partially.
  const MAX_WITHHELD = 4096;
  // Values of unknown sensitivity shorter than this are not remembered (they
  // would match ordinary text). A known secret (PIN / OTP / password field,
  // credential header token, URL password) is remembered at any length.
  const MIN_WITHHELD_LEN = 6;
  const HEADER_PARTS = /[\s;,]+/;
  // Credential-header words that are syntax, not secret.
  const HEADER_SYNTAX = ['bearer', 'basic', 'digest', 'token', 'negotiate', 'httponly', 'secure', 'partitioned'];
  // Attribute pairs that are descriptive and never carry the credential.
  const DESCRIPTIVE_ATTRS = [
    'path',
    'domain',
    'expires',
    'max-age',
    'samesite',
    'priority',
    'realm',
    'charset',
    'scope',
    'algorithm',
    'qop',
    'stale',
    'version',
    'error',
    'error_description',
  ];
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

  // Lenient percent-decoding: each run of %XX bytes is decoded as UTF-8 with
  // replacement characters, like Python's urllib.parse.unquote. Never throws.
  function pctDecode(text) {
    return String(text).replace(/(?:%[0-9A-Fa-f]{2})+/g, (run) => {
      const bytes = new Uint8Array(run.length / 3);
      for (let i = 0; i < bytes.length; i += 1) {
        bytes[i] = parseInt(run.slice(i * 3 + 1, i * 3 + 3), 16);
      }
      return new TextDecoder('utf-8').decode(bytes);
    });
  }

  function makeWithheld() {
    const values = [];
    const short = [];
    const seen = new Set();
    return {
      values,
      short,
      push(candidate, known) {
        if (candidate === PLACEHOLDER || !candidate.trim() || seen.has(candidate)) return;
        let target;
        if (candidate.length >= MIN_WITHHELD_LEN) target = values;
        else if (known) target = short;
        else return;
        if (values.length + short.length >= MAX_WITHHELD) throw new Error('too_many_withheld');
        seen.add(candidate);
        target.push(candidate);
      },
    };
  }

  // Remember a withheld scalar for the final leak check. The placeholder is
  // skipped, and so are short values unless known to be secret, so the check
  // cannot match ordinary output.
  function withhold(list, value, known) {
    if (typeof value === 'number' && Number.isFinite(value)) value = String(value);
    if (typeof value !== 'string') return;
    list.push(value, !!known);
    list.push(value.trim().replace(/^["']+|["']+$/g, ''), !!known);
  }

  function withholdDecoded(list, value, known) {
    withhold(list, value, known);
    withhold(list, pctDecode(value), known);
  }

  // A non-allowlisted header that is not known to be a credential: the whole
  // value and its opaque parts (alphabetic words would match ordinary text).
  function withholdHeader(list, value) {
    withhold(list, value);
    for (const part of String(value).split(HEADER_PARTS)) {
      for (const piece of part.split('=')) {
        if (piece.length >= 8 && !/^[A-Za-z]+$/.test(piece)) withhold(list, piece);
      }
    }
  }

  // A credential header: every scalar it carries. "Bearer <token>" keeps its
  // token even when alphabetic; "sid=<v>; Path=/" keeps <v>; "Basic <b64>"
  // also keeps the decoded user:password and each half.
  function withholdCredentialHeader(list, value) {
    const text = String(value);
    withhold(list, text, true);
    const parts = text.split(HEADER_PARTS).filter((p) => p);
    const scheme = parts.length ? parts[0].toLowerCase() : '';
    parts.forEach((part, index) => {
      const eq = part.indexOf('=');
      const name = eq >= 0 ? part.slice(0, eq) : part;
      if (eq >= 0 && DESCRIPTIVE_ATTRS.indexOf(name.trim().toLowerCase()) >= 0) return;
      if (eq < 0 && HEADER_SYNTAX.indexOf(part.toLowerCase()) >= 0) return;
      // The credential after an auth scheme is known at any length.
      const known = index === 1 && HEADER_SYNTAX.indexOf(scheme) >= 0;
      withholdDecoded(list, part, known);
      withhold(list, part.replace(/=+$/, ''), known);
      if (eq >= 0) {
        withholdDecoded(
          list,
          part.slice(eq + 1).replace(/^["']+|["']+$/g, ''),
          known || isSecretKey(name.trim())
        );
      }
    });
    if (parts.length >= 2 && parts[0].toLowerCase() === 'basic') {
      let decoded = '';
      try {
        const bin = atob(parts[1]);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
        decoded = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
      } catch (e) {
        decoded = '';
      }
      if (decoded) {
        withhold(list, decoded, true);
        const colon = decoded.indexOf(':');
        if (colon >= 0) {
          withhold(list, decoded.slice(0, colon));
          withhold(list, decoded.slice(colon + 1), true); // the password half
        }
      }
    }
  }

  // Every scalar leaf of a removed subtree, within the body limits.
  function withholdLeaves(list, value, budget, depth) {
    budget.nodes += 1;
    if (budget.nodes > MAX_NODES || depth > MAX_DEPTH) throw new Error('body_too_complex');
    if (Array.isArray(value)) {
      for (const v of value) withholdLeaves(list, v, budget, depth + 1);
    } else if (value && typeof value === 'object') {
      for (const k of Object.keys(value)) withholdLeaves(list, value[k], budget, depth + 1);
    } else {
      withhold(list, value, true);
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
      } else if (SECRET_HEADER.test(name) || isSecretValue(value)) {
        withholdCredentialHeader(withheld, value);
        out.push({ name, value: PLACEHOLDER });
      } else {
        withholdHeader(withheld, value);
        out.push({ name, value: PLACEHOLDER });
      }
    }
    return out;
  }

  function redactString(key, value, withheld) {
    if (isSecretKey(key) || isSecretValue(value)) {
      withhold(withheld, value, true);
      return PLACEHOLDER;
    }
    return value;
  }

  function redactTree(value, key, withheld, budget, depth) {
    budget.nodes += 1;
    if (budget.nodes > MAX_NODES || depth > MAX_DEPTH) throw new Error('body_too_complex');
    if (value && typeof value === 'object' && key != null && isSecretKey(key)) {
      // Remember the scalar leaves, not the subtree's JSON encoding: a leaf
      // copied elsewhere must still match.
      withholdLeaves(withheld, value, budget, depth);
      return PLACEHOLDER;
    }
    if (Array.isArray(value)) {
      return value.map((v) => redactTree(v, key, withheld, budget, depth + 1));
    }
    if (value && typeof value === 'object') {
      const out = {};
      for (const k of Object.keys(value)) {
        out[k] = redactTree(value[k], k, withheld, budget, depth + 1);
      }
      return out;
    }
    if (typeof value === 'string') return redactString(key, value, withheld);
    if (key != null && isSecretKey(key) && value != null) {
      withhold(withheld, value, true);
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
    withholdDecoded(withheld, u.username);
    withholdDecoded(withheld, u.password, true);
    const segments = u.pathname.split('/').map((seg) => {
      const decoded = pctDecode(seg);
      if (isSecretValue(decoded)) {
        withhold(withheld, decoded, true);
        return PLACEHOLDER;
      }
      return seg;
    });
    const params = [];
    for (const [k, v] of u.searchParams.entries()) {
      const value = redactString(k, v, withheld);
      params.push(encodeURIComponent(k) + '=' + (value === PLACEHOLDER ? PLACEHOLDER : encodeURIComponent(value)));
    }
    const fragment = u.hash.slice(1);
    withholdDecoded(withheld, fragment);
    for (const [k, v] of new URLSearchParams(fragment).entries()) withhold(withheld, v, isSecretKey(k));
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
        if (e && e.message === 'too_many_withheld') throw e;
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
        if (e && e.message === 'too_many_withheld') throw e;
        return { status: 'omitted:too_complex', body: null };
      }
    }
    return { status: 'omitted:unsupported_type', body: null };
  }

  // True when a withheld identity survives in the record's JSON text or its
  // percent-decoded canonical forms (an encoded copy in a retained URL or
  // field counts too).
  function leaks(record, withheld) {
    const text = JSON.stringify(record);
    const texts = [text, pctDecode(text), pctDecode(text.replace(/\+/g, ' '))];
    for (const value of withheld.values) {
      const forms = [value, JSON.stringify(value).slice(1, -1)];
      for (const form of forms) {
        for (const t of texts) {
          if (t.indexOf(form) >= 0) return true;
        }
      }
    }
    if (withheld.short.length) {
      // Short known secrets are compared against each page-derived scalar
      // (not the JSON syntax or the fixed record vocabulary), so a copy in
      // the URL, a header or the body still counts.
      for (const scalar of userScalars(record)) {
        const t = scalar.split(PLACEHOLDER).join('\u0000');
        const forms = [t, pctDecode(t), pctDecode(t.replace(/\+/g, ' '))];
        for (const value of withheld.short) {
          if (forms.some((f) => f.indexOf(value) >= 0)) return true;
        }
      }
    }
    return false;
  }

  // Every scalar of a record that came from the page, as text.
  function userScalars(record) {
    const out = [];
    for (const key of ['url', 'url_origin']) {
      if (typeof record[key] === 'string') out.push(record[key]);
    }
    for (const key of ['request_headers', 'response_headers']) {
      for (const h of record[key] || []) {
        if (h && typeof h === 'object') out.push(String(h.name || ''), String(h.value || ''));
      }
    }
    const stack = [record.request_body];
    while (stack.length) {
      const node = stack.pop();
      if (Array.isArray(node)) {
        for (const v of node) stack.push(v);
      } else if (node && typeof node === 'object') {
        for (const k of Object.keys(node)) {
          out.push(k);
          stack.push(node[k]);
        }
      } else if (typeof node === 'string') {
        out.push(node);
      } else if (typeof node === 'number' && Number.isFinite(node)) {
        out.push(String(node));
      }
    }
    return out;
  }

  // details: {method, url, type, tabId, frameId, timeStamp, statusCode,
  //           requestHeaders, responseHeaders, requestBody}
  // Returns a redacted network_save evidence line, or null (dropped).
  function redactNetworkSave(details) {
    try {
      if (!details || typeof details !== 'object') return null;
      const method = String(details.method || '').toUpperCase();
      if (SAVE_METHODS.indexOf(method) < 0) return null;
      const withheld = makeWithheld();
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
