// Run the optional Flow Chrome extension scripts in Node against a small
// fake DOM that supports open shadow roots. Reports messages each step.
//
// Usage:
//   node chrome_extension_harness.js content SCRIPT_PATH STEPS_JSON
//   node chrome_extension_harness.js background SCRIPT_PATH STEPS_JSON
//   node chrome_extension_harness.js eval SCRIPT_PATH STEPS_JSON
'use strict';

const fs = require('fs');
const vm = require('vm');

const [kind, scriptPath, stepsJson] = process.argv.slice(2);
const steps = JSON.parse(stepsJson);

class FakeEventTarget {
  constructor() {
    this._listeners = [];
  }
  addEventListener(type, callback, options) {
    if (!callback) return;
    const capture = typeof options === 'boolean' ? options : Boolean(options && options.capture);
    if (!this._listeners.some((l) => l.type === type && l.callback === callback && l.capture === capture)) {
      this._listeners.push({ type, callback, capture });
    }
  }
  removeEventListener(type, callback, options) {
    const capture = typeof options === 'boolean' ? options : Boolean(options && options.capture);
    this._listeners = this._listeners.filter(
      (l) => !(l.type === type && l.callback === callback && l.capture === capture)
    );
  }
  listenerCount(type) {
    return this._listeners.filter((l) => !type || l.type === type).length;
  }
}

class Node extends FakeEventTarget {
  constructor() {
    super();
    this.parentNode = null;
    this.parentElement = null;
    this.childNodes = [];
    this.nodeType = 1;
  }
}
Node.ELEMENT_NODE = 1;

class Element extends Node {
  constructor(tagName, attributes = {}) {
    super();
    this.tagName = String(tagName).toUpperCase();
    this.attributes = new Map(Object.entries(attributes));
    this.children = [];
    this.value = '';
    this._shadow = null;
  }
  get id() {
    return this.attributes.get('id') || '';
  }
  get type() {
    return this.attributes.get('type') || '';
  }
  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }
  appendChild(child) {
    child.parentNode = this;
    child.parentElement = this;
    this.childNodes.push(child);
    this.children.push(child);
    return child;
  }
  getRootNode() {
    let n = this;
    while (n.parentNode) n = n.parentNode;
    return n._isShadowRoot ? n : document;
  }
  attachShadow(init) {
    const root = new ShadowRoot(this, (init && init.mode) || 'open');
    this._shadow = root;
    return root;
  }
  get shadowRoot() {
    if (!this._shadow) return null;
    return this._shadow.mode === 'open' ? this._shadow : null;
  }
}

class ShadowRoot extends FakeEventTarget {
  constructor(host, mode) {
    super();
    this.host = host;
    this.mode = mode;
    this.childNodes = [];
    this.children = [];
    this.parentNode = null;
    this.parentElement = null;
    this._isShadowRoot = true;
    this.nodeType = 11;
  }
  appendChild(child) {
    child.parentNode = this;
    child.parentElement = null;
    this.childNodes.push(child);
    this.children.push(child);
    return child;
  }
}

class Document extends FakeEventTarget {
  constructor() {
    super();
    this.nodeType = 9;
    this.documentElement = new Element('html');
    this.body = new Element('body');
    this.documentElement.appendChild(this.body);
  }
  addEventListener(type, callback, options) {
    super.addEventListener(type, callback, options);
  }
  removeEventListener(type, callback, options) {
    super.removeEventListener(type, callback, options);
  }
}

const document = new Document();
const location = { href: 'https://app.example/form' };
const window = { top: null, document, location };
window.top = window;

const sent = [];
const chrome = {
  runtime: {
    sendMessage(msg, cb) {
      sent.push({ channel: 'runtime.sendMessage', msg });
      if (typeof cb === 'function') cb({ active: false });
    },
    onMessage: {
      _listeners: [],
      addListener(fn) {
        this._listeners.push(fn);
      },
      _deliver(msg) {
        const replies = [];
        for (const fn of this._listeners) {
          fn(msg, {}, (r) => replies.push(r));
        }
        return replies;
      },
    },
    connectNative() {
      throw new Error('native host not available in harness');
    },
  },
  action: {
    setBadgeText() {},
    setBadgeBackgroundColor() {},
    setTitle() {},
  },
  tabs: {
    query(_q, cb) {
      cb([]);
    },
    sendMessage() {},
  },
};

const context = {
  console,
  chrome,
  document,
  window,
  location,
  Element,
  Node,
  Date,
  Array,
  String,
  Boolean,
  Map,
  globalThis: {},
};

context.globalThis = context;
const script = fs.readFileSync(scriptPath, 'utf8');
vm.runInNewContext(script, context, { filename: scriptPath });

function buildDom(spec) {
  // spec: { host: {tag, attrs}, shadow: {mode, children: [...]}, light?: [...] }
  const host = new Element(spec.host.tag || 'div', spec.host.attrs || {});
  document.body.appendChild(host);
  let leaf = host;
  if (spec.shadow) {
    const root = host.attachShadow({ mode: spec.shadow.mode || 'open' });
    for (const child of spec.shadow.children || []) {
      const el = new Element(child.tag, child.attrs || {});
      if (child.value != null) el.value = child.value;
      root.appendChild(el);
      leaf = el;
    }
  }
  return { host, leaf };
}

// Dispatch a real-shaped event to the document's capture listeners, as a
// browser would: listeners outside a shadow root see ev.target retargeted to
// the outermost host, and composedPath() hides nodes inside closed roots.
function dispatchToDocument(leaf, type) {
  const full = [];
  let n = leaf;
  while (n) {
    full.push(n);
    if (n._isShadowRoot) n = n.host;
    else n = n.parentNode;
  }
  full.push(document);
  // Drop everything inside a closed shadow root (and the root itself).
  let visible = full;
  for (let i = full.length - 1; i >= 0; i--) {
    if (full[i]._isShadowRoot && full[i].mode === 'closed') {
      visible = full.slice(i + 1);
      break;
    }
  }
  // Retarget: the outermost element not inside any shadow root.
  let target = leaf;
  for (const node of full) {
    if (node.nodeType === 1 && node.getRootNode() === document) {
      target = node;
      break;
    }
  }
  const ev = {
    type,
    target,
    composed: true,
    composedPath() {
      return visible.slice();
    },
  };
  for (const l of document._listeners.slice()) {
    if (l.type === type && l.capture) l.callback(ev);
  }
  return { retargeted_tag: target.tagName.toLowerCase(), retargeted_id: target.id || null };
}

const results = [];

for (const step of steps) {
  sent.length = 0;
  const beforeListeners = {
    document: document.listenerCount(),
  };
  if (kind === 'content' || kind === 'eval') {
    const api = context.__openadaptExtensionContent;
    if (step.mode === 'record') api.setRecording(true);
    else if (step.mode === 'idle') api.setRecording(false);
    else if (step.build) {
      const { leaf } = buildDom(step.build);
      if (step.action === 'path') {
        results.push({ path: api.shadowPiercingPath(leaf), sent: sent.slice() });
        continue;
      }
      if (step.action === 'evidence') {
        results.push({
          evidence: api.targetEvidence(leaf, step.event || 'click'),
          sent: sent.slice(),
          recording: api.recording,
        });
        continue;
      }
      if (step.action === 'dispatch') {
        const dispatched = dispatchToDocument(leaf, step.event || 'click');
        results.push({
          dispatched,
          sent: sent.slice(),
          recording: api.recording,
        });
        continue;
      }
      if (step.action === 'click' || step.action === 'input' || step.action === 'focus') {
        // Simulate by calling emit path through targetEvidence + manual send when recording.
        const evidence = api.targetEvidence(leaf, step.action === 'focus' ? 'focus' : step.action);
        if (api.recording && evidence) {
          chrome.runtime.sendMessage({ type: 'EXTENSION_EVIDENCE', evidence });
        }
        results.push({
          evidence,
          sent: sent.slice(),
          recording: api.recording,
          listeners: { document: document.listenerCount() },
        });
        continue;
      }
    } else if (step.isSecret) {
      const el = new Element(step.isSecret.tag || 'input', step.isSecret.attrs || {});
      results.push({ secret: api.isSecretField(el), sent: sent.slice() });
      continue;
    }
  }
  if (kind === 'background') {
    const api = context.__openadaptExtensionBackground;
    if (step.mode === 'record') api.setRecording(true);
    else if (step.mode === 'idle') api.setRecording(false);
    else if (step.evidence) {
      chrome.runtime.onMessage._deliver({
        type: 'EXTENSION_EVIDENCE',
        evidence: step.evidence,
      });
    } else if (step.state) {
      results.push({
        recording: api.recording,
        queued: api.queued,
        sent: sent.slice(),
      });
      continue;
    }
  }
  results.push({
    sent: sent.slice(),
    listeners: { document: document.listenerCount() },
    recording:
      (context.__openadaptExtensionContent && context.__openadaptExtensionContent.recording) ||
      (context.__openadaptExtensionBackground && context.__openadaptExtensionBackground.recording) ||
      false,
  });
}

process.stdout.write(JSON.stringify(results));
