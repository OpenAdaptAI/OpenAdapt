// Run a Chrome extension prototype script in Node against a small fake
// browser and report every message the script sends.
//
// Usage:
//   node chrome_extension_harness.js content SCRIPT_PATH STEPS_JSON
//   node chrome_extension_harness.js background SCRIPT_PATH STEPS_JSON
//
// The fake follows the DOM rules the scripts depend on: a listener is keyed
// by (type, callback, capture), a boolean third argument is the capture flag,
// and an event reaches capture listeners on its ancestors before its target.
// Each step's output lists the messages sent during that step.
'use strict';

const fs = require('fs');
const vm = require('vm');

const [kind, scriptPath, stepsJson] = process.argv.slice(2);
const steps = JSON.parse(stepsJson);

function captureFlag(options) {
  if (typeof options === 'boolean') return options;
  return Boolean(options && options.capture);
}

class FakeEventTarget {
  constructor() {
    this._listeners = [];
  }

  addEventListener(type, callback, options) {
    if (!callback) return;
    const capture = captureFlag(options);
    const exists = this._listeners.some(
      (l) => l.type === type && l.callback === callback && l.capture === capture
    );
    if (!exists) this._listeners.push({ type, callback, capture });
  }

  removeEventListener(type, callback, options) {
    const capture = captureFlag(options);
    this._listeners = this._listeners.filter(
      (l) => !(l.type === type && l.callback === callback && l.capture === capture)
    );
  }

  _invoke(event, phase) {
    for (const l of this._listeners.slice()) {
      if (l.type !== event.type) continue;
      if (phase === 'capture' && !l.capture) continue;
      if (phase === 'bubble' && l.capture) continue;
      l.callback.call(this, event);
    }
  }
}

class Event {
  constructor(type, init = {}) {
    this.type = type;
    this.bubbles = Boolean(init.bubbles);
    this.target = null;
  }
}
class UIEvent extends Event {}
class MouseEvent extends UIEvent {
  constructor(type, init = {}) {
    super(type, init);
    this.clientX = init.clientX || 0;
    this.clientY = init.clientY || 0;
    this.screenX = init.screenX || 0;
    this.screenY = init.screenY || 0;
    this.button = init.button || 0;
  }
}
class KeyboardEvent extends UIEvent {
  constructor(type, init = {}) {
    super(type, init);
    this.key = init.key || '';
    this.code = init.code || '';
    this.shiftKey = false;
    this.ctrlKey = false;
    this.altKey = false;
    this.metaKey = false;
  }
}

class Node extends FakeEventTarget {}
Node.ELEMENT_NODE = 1;
Node.TEXT_NODE = 3;
Node.COMMENT_NODE = 8;

class Element extends Node {}

class HTMLElement extends Element {
  constructor(tagName, attributes = {}, parent = null) {
    super();
    this.tagName = tagName.toUpperCase();
    this.nodeType = Node.ELEMENT_NODE;
    this.parentNode = parent;
    this.childNodes = [];
    this.attributes = new Map(Object.entries(attributes));
    this.classList = [];
    this.onclick = null;
    this.innerText = '';
    this.textContent = '';
    this.value = '';
    if (parent) parent.childNodes.push(this);
  }

  get id() {
    return this.attributes.get('id') || '';
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  getBoundingClientRect() {
    return { top: 10, left: 10, bottom: 30, right: 110, width: 100, height: 20 };
  }

  closest() {
    return null;
  }

  cloneNode(deep) {
    const copy = new HTMLElement(this.tagName, Object.fromEntries(this.attributes));
    if (deep) {
      for (const child of this.childNodes) {
        const childCopy = child.cloneNode(true);
        childCopy.parentNode = copy;
        copy.childNodes.push(childCopy);
      }
    }
    return copy;
  }

  removeChild(child) {
    this.childNodes = this.childNodes.filter((c) => c !== child);
    return child;
  }

  get outerHTML() {
    const tag = this.tagName.toLowerCase();
    const attrs = [...this.attributes].map(([k, v]) => ` ${k}="${v}"`).join('');
    const inner = this.childNodes.map((c) => c.outerHTML).join('');
    return `<${tag}${attrs}>${inner}</${tag}>`;
  }

  focus() {}
  click() {}
}

class FakeObserver {
  observe() {}
  disconnect() {}
}

function descendants(element) {
  const out = [];
  for (const child of element.childNodes) {
    out.push(child, ...descendants(child));
  }
  return out;
}

function makeBrowser() {
  const window = new FakeEventTarget();
  const document = new FakeEventTarget();
  const html = new HTMLElement('html');
  const body = new HTMLElement('body', {}, html);
  const form = new HTMLElement('form', {}, body);
  const password = new HTMLElement(
    'input', { id: 'password', type: 'password', name: 'password' }, form
  );
  const button = new HTMLElement('button', { type: 'submit' }, form);
  html.parentNode = document;

  Object.assign(document, {
    nodeType: 9,
    body,
    documentElement: { clientWidth: 1280, clientHeight: 800 },
    activeElement: password,
    querySelectorAll: (selector) => (selector === '*' ? [html, ...descendants(html)] : []),
    querySelector: () => null,
    getElementById: () => null,
  });
  Object.assign(window, {
    scrollX: 0,
    scrollY: 0,
    innerWidth: 1280,
    innerHeight: 800,
    devicePixelRatio: 1,
    location: { href: 'https://example.test/sign-in' },
    getComputedStyle: () => ({ visibility: 'visible', display: 'block', opacity: '1' }),
    scrollBy: () => {},
  });
  return { window, document, body, password, button };
}

// Deliver an event the way a browser does: capture listeners from the window
// down to the target's parent, all listeners on the target, then bubble
// listeners back up when the event bubbles.
function dispatch(browser, target, event) {
  const ancestors = [];
  for (let node = target.parentNode; node; node = node.parentNode) ancestors.unshift(node);
  if (target !== browser.window) ancestors.unshift(browser.window);
  event.target = target;
  for (const node of ancestors) node._invoke(event, 'capture');
  target._invoke(event, 'capture');
  target._invoke(event, 'bubble');
  if (event.bubbles) {
    for (const node of ancestors.slice().reverse()) node._invoke(event, 'bubble');
  }
}

function summarize(message) {
  return {
    type: message.type,
    eventType: message.eventType || null,
    key: message.key === undefined ? null : message.key,
    carriesHTML: typeof message.visibleHTMLString === 'string',
  };
}

function listenerCount(target) {
  return target._listeners.length;
}

async function runContent(source) {
  const browser = makeBrowser();
  const sent = [];
  const messageListeners = [];
  let clock = 1_000_000;
  class FakeDate extends Date {
    static now() {
      return clock;
    }
  }
  const sandbox = {
    window: browser.window,
    document: browser.document,
    Node,
    Element,
    HTMLElement,
    Event,
    UIEvent,
    MouseEvent,
    KeyboardEvent,
    IntersectionObserver: FakeObserver,
    MutationObserver: FakeObserver,
    performance: { now: () => 0 },
    console: { log() {}, debug() {}, warn() {}, error() {} },
    setTimeout,
    clearTimeout,
    Date: FakeDate,
    chrome: {
      runtime: {
        sendMessage: (message) => sent.push(JSON.parse(JSON.stringify(message))),
        onMessage: { addListener: (fn) => messageListeners.push(fn) },
      },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: scriptPath });

  const results = [];
  for (const step of steps) {
    clock += 1000; // Past every debounce window.
    const before = sent.length;
    if (step.mode) {
      for (const fn of messageListeners) {
        fn({ type: 'SET_MODE', payload: { mode: step.mode } }, {}, () => {});
      }
    } else if (step.event === 'keydown' || step.event === 'keyup') {
      dispatch(browser, browser.password, new KeyboardEvent(step.event, {
        bubbles: true, key: step.key || 's', code: 'KeyS',
      }));
    } else if (step.event === 'click') {
      dispatch(browser, browser.button, new MouseEvent('click', {
        bubbles: true, clientX: 20, clientY: 20, screenX: 120, screenY: 220,
      }));
    } else if (step.event === 'mousemove' || step.event === 'mousedown'
      || step.event === 'mouseup') {
      dispatch(browser, browser.body, new MouseEvent(step.event, {
        bubbles: true, clientX: 40 + results.length, clientY: 50,
        screenX: 140 + results.length, screenY: 250,
      }));
    } else if (step.event === 'scroll') {
      browser.window.scrollY += 100;
      dispatch(browser, browser.document, new Event('scroll', { bubbles: true }));
    } else {
      throw new Error(`unknown step ${JSON.stringify(step)}`);
    }
    // Let debounced timers inside the script fire within this step.
    await new Promise((resolve) => setTimeout(resolve, 30));
    results.push({
      step,
      sent: sent.slice(before).map(summarize),
      listeners: {
        document: listenerCount(browser.document),
        body: listenerCount(browser.body),
      },
    });
  }
  return results;
}

async function runBackground(source) {
  const sockets = [];
  class FakeWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = FakeWebSocket.OPEN;
      this.sent = [];
      sockets.push(this);
    }
    send(data) {
      this.sent.push(JSON.parse(data));
    }
    close() {}
  }
  FakeWebSocket.CONNECTING = 0;
  FakeWebSocket.OPEN = 1;
  const event = () => {
    const listeners = [];
    return { addListener: (fn) => listeners.push(fn), listeners };
  };
  const onMessage = event();
  const chrome = {
    runtime: { onMessage, onInstalled: event(), onStartup: event() },
    tabs: {
      query: (_query, callback) => callback([]),
      sendMessage: () => Promise.resolve(),
      onUpdated: event(),
    },
    action: { setIcon() {}, setBadgeText() {}, setBadgeBackgroundColor() {} },
    alarms: { create() {}, onAlarm: event() },
  };
  const sandbox = {
    chrome,
    WebSocket: FakeWebSocket,
    console: { log() {}, debug() {}, warn() {}, error() {} },
    setInterval: () => 0,
    clearInterval: () => {},
    Date,
    JSON,
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: scriptPath });
  const socket = sockets[sockets.length - 1];
  socket.onopen();

  const results = [];
  for (const step of steps) {
    const before = socket.sent.length;
    if (step.mode) {
      socket.onmessage({ data: JSON.stringify({ type: 'SET_MODE', payload: { mode: step.mode } }) });
    } else if (step.message) {
      for (const fn of onMessage.listeners) {
        fn(JSON.parse(JSON.stringify(step.message)), { tab: { id: 7 } }, () => {});
      }
    } else {
      throw new Error(`unknown step ${JSON.stringify(step)}`);
    }
    results.push({ step, sent: socket.sent.slice(before).map(summarize) });
  }
  return results;
}

async function main() {
  const source = fs.readFileSync(scriptPath, 'utf8');
  const run = { content: runContent, background: runBackground }[kind];
  if (!run) throw new Error(`unknown script kind ${kind}`);
  process.stdout.write(JSON.stringify(await run(source)));
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exit(1);
});
