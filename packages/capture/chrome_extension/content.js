/**
 * OpenAdapt Capture - Content Script
 *
 * Injected into web pages to capture DOM events with:
 * - Coordinate tracking (client → screen via linear regression)
 * - DOM instrumentation (data-id, data-tlbr-client/screen)
 * - Full visible HTML capture
 * - Semantic element references (role, name, bbox, tagName, classList)
 *
 * Based on legacy OpenAdapt chrome extension with enhancements.
 *
 * TESTING STATUS:
 * - Coordinate tracking, DOM instrumentation, visible HTML: CONFIRMED WORKING (legacy repo)
 * - Semantic element refs (extractSemanticElementRef): NEW, UNTESTED
 */

const DEBUG = false;

if (!DEBUG) {
  console.debug = function() {};
}

let currentMode = 'idle';
let recordListenersAttached = false;
let replayObserversAttached = false;

// ============ Configuration ============

const RETURN_FULL_DOCUMENT = false;
const MAX_COORDS = 3;
const SET_SCREEN_COORDS = true;

// ============ Element ID Tracking ============

const elementIdMap = new WeakMap();
const idToElementMap = new Map();
let elementIdCounter = 0;
let messageIdCounter = 0;
const pageId = `${Date.now()}-${Math.random()}`;

// ============ Coordinate Mappings ============

const coordMappings = {
  x: { client: [], screen: [] },
  y: { client: [], screen: [] }
};

let lastScrollPosition = { x: window.scrollX, y: window.scrollY };

// ============ Role Mapping (for semantic refs) ============

const IMPLICIT_ROLES = {
  'a': (el) => el.hasAttribute('href') ? 'link' : null,
  'button': () => 'button',
  'input': (el) => {
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    const typeMap = {
      'text': 'textbox', 'email': 'textbox', 'url': 'textbox', 'tel': 'textbox',
      'search': 'searchbox', 'password': 'textbox', 'number': 'spinbutton',
      'range': 'slider', 'checkbox': 'checkbox', 'radio': 'radio',
      'submit': 'button', 'button': 'button', 'reset': 'button', 'image': 'button'
    };
    return typeMap[type] || 'textbox';
  },
  'select': () => 'combobox',
  'textarea': () => 'textbox',
  'img': () => 'image',
  'h1': () => 'heading', 'h2': () => 'heading', 'h3': () => 'heading',
  'h4': () => 'heading', 'h5': () => 'heading', 'h6': () => 'heading',
  'nav': () => 'navigation', 'main': () => 'main', 'aside': () => 'complementary',
  'footer': () => 'contentinfo', 'header': () => 'banner', 'article': () => 'article',
  'section': () => 'region', 'form': () => 'form', 'table': () => 'table',
  'ul': () => 'list', 'ol': () => 'list', 'li': () => 'listitem',
  'dialog': () => 'dialog', 'menu': () => 'menu', 'menuitem': () => 'menuitem',
  'progress': () => 'progressbar', 'meter': () => 'meter'
};

// ============ Mode Management ============

function setMode(mode) {
  currentMode = mode;
  console.log(`[OpenAdapt] Mode set to: ${currentMode}`);

  if (currentMode === 'record') {
    if (!recordListenersAttached) attachRecordListeners();
    if (replayObserversAttached) disconnectReplayObservers();
  } else if (currentMode === 'replay') {
    debounceSendVisibleHTML('setmode');
    if (!replayObserversAttached) attachReplayObservers();
    if (recordListenersAttached) detachRecordListeners();
  } else if (currentMode === 'idle') {
    if (recordListenersAttached) detachRecordListeners();
    if (replayObserversAttached) disconnectReplayObservers();
  }
}

// ============ Coordinate Transformation ============

function trackMouseEvent(event) {
  const { clientX, clientY, screenX, screenY } = event;
  const prevCoordMappingsStr = JSON.stringify(coordMappings);

  updateCoordinateMappings('x', clientX, screenX);
  updateCoordinateMappings('y', clientY, screenY);

  trimMappings(coordMappings.x);
  trimMappings(coordMappings.y);

  const coordMappingsStr = JSON.stringify(coordMappings);
  if (DEBUG && coordMappingsStr !== prevCoordMappingsStr) {
    console.log(JSON.stringify(coordMappings));
  }
}

function updateCoordinateMappings(dim, clientCoord, screenCoord) {
  const coordMap = coordMappings[dim];
  if (coordMap.client.includes(clientCoord)) {
    coordMap.screen[coordMap.client.indexOf(clientCoord)] = screenCoord;
  } else {
    coordMap.client.push(clientCoord);
    coordMap.screen.push(screenCoord);
  }
}

function trimMappings(coordMap) {
  if (coordMap.client.length > MAX_COORDS) {
    coordMap.client.shift();
    coordMap.screen.shift();
  }
}

function getConversionPoints() {
  const { x, y } = coordMappings;
  if (x.client.length < 2 || y.client.length < 2) {
    return { sxScale: null, syScale: null, sxOffset: null, syOffset: null };
  }
  const { scale: sxScale, offset: sxOffset } = fitLinearTransformation(x.client, x.screen);
  const { scale: syScale, offset: syOffset } = fitLinearTransformation(y.client, y.screen);
  return { sxScale, syScale, sxOffset, syOffset };
}

function fitLinearTransformation(clientCoords, screenCoords) {
  const n = clientCoords.length;
  let sumClient = 0, sumScreen = 0, sumClientSquared = 0, sumClientScreen = 0;

  for (let i = 0; i < n; i++) {
    sumClient += clientCoords[i];
    sumScreen += screenCoords[i];
    sumClientSquared += clientCoords[i] * clientCoords[i];
    sumClientScreen += clientCoords[i] * screenCoords[i];
  }

  const scale = (n * sumClientScreen - sumClient * sumScreen) / (n * sumClientSquared - sumClient * sumClient);
  const offset = (sumScreen - scale * sumClient) / n;
  return { scale, offset };
}

function getScreenCoordinates(element) {
  const rect = element.getBoundingClientRect();
  const { top: clientTop, left: clientLeft, bottom: clientBottom, right: clientRight } = rect;
  const conversionPoints = getConversionPoints();

  if (conversionPoints.sxScale === null) {
    return { top: null, left: null, bottom: null, right: null };
  }

  const { sxScale, syScale, sxOffset, syOffset } = conversionPoints;
  return {
    top: syScale * clientTop + syOffset,
    left: sxScale * clientLeft + sxOffset,
    bottom: syScale * clientBottom + syOffset,
    right: sxScale * clientRight + sxOffset
  };
}

// ============ Element Visibility ============

function isVisible(element) {
  const rect = element.getBoundingClientRect();
  const style = window.getComputedStyle(element);

  return (
    rect.width > 0 &&
    rect.height > 0 &&
    rect.bottom >= 0 &&
    rect.right >= 0 &&
    rect.top <= (window.innerHeight || document.documentElement.clientHeight) &&
    rect.left <= (window.innerWidth || document.documentElement.clientWidth) &&
    style.visibility !== 'hidden' &&
    style.display !== 'none' &&
    style.opacity !== '0'
  );
}

// ============ DOM Instrumentation ============

function generateElementIdAndBbox(element) {
  if (!isVisible(element)) {
    return null;
  }

  // Set data-id
  if (!elementIdMap.has(element)) {
    const newId = `elem-${elementIdCounter++}`;
    elementIdMap.set(element, newId);
    idToElementMap.set(newId, element);
    element.setAttribute('data-id', newId);
  }

  // Set client bbox
  let { top, left, bottom, right } = element.getBoundingClientRect();
  element.setAttribute('data-tlbr-client', `${top},${left},${bottom},${right}`);

  // Set screen bbox
  if (SET_SCREEN_COORDS) {
    const screenCoords = getScreenCoordinates(element);
    if (screenCoords.top !== null) {
      element.setAttribute('data-tlbr-screen',
        `${screenCoords.top},${screenCoords.left},${screenCoords.bottom},${screenCoords.right}`);
    }
  }

  return elementIdMap.get(element);
}

function instrumentLiveDomWithBbox() {
  document.querySelectorAll('*').forEach(element => generateElementIdAndBbox(element));
}

// ============ Visible HTML Generation ============

function cleanDomTree(node) {
  const children = Array.from(node.childNodes);
  for (const child of children) {
    if (child.nodeType === Node.ELEMENT_NODE) {
      // Handle data URLs in images
      if (child.tagName === 'IMG' && child.hasAttribute('src')) {
        const src = child.getAttribute('src');
        if (src.startsWith('data:')) {
          child.setAttribute('src', '');
        }
      }

      const originalId = child.getAttribute('data-id');
      if (originalId) {
        const originalElement = idToElementMap.get(originalId);
        if (!originalElement || !isVisible(originalElement)) {
          node.removeChild(child);
        } else {
          cleanDomTree(child);
        }
      }
    } else if (child.nodeType === Node.COMMENT_NODE) {
      node.removeChild(child);
    } else if (child.nodeType === Node.TEXT_NODE) {
      const trimmedText = child.textContent.replace(/\s+/g, ' ').trim();
      if (trimmedText.length === 0) {
        node.removeChild(child);
      } else {
        child.textContent = trimmedText;
      }
    }
  }
}

function getVisibleHTMLString() {
  const startTime = performance.now();
  instrumentLiveDomWithBbox();

  if (RETURN_FULL_DOCUMENT) {
    const visibleHTMLDuration = performance.now() - startTime;
    return { visibleHTMLString: document.body.outerHTML, visibleHTMLDuration };
  }

  const clonedBody = document.body.cloneNode(true);
  cleanDomTree(clonedBody);
  const visibleHTMLString = clonedBody.outerHTML;
  const visibleHTMLDuration = performance.now() - startTime;

  return { visibleHTMLString, visibleHTMLDuration };
}

// ============ Semantic Element Reference ============

function getRole(element) {
  const explicitRole = element.getAttribute('role');
  if (explicitRole) return explicitRole;

  const tagName = element.tagName.toLowerCase();
  const implicitRoleFn = IMPLICIT_ROLES[tagName];
  if (implicitRoleFn) {
    const role = implicitRoleFn(element);
    if (role) return role;
  }

  if (element.onclick || element.getAttribute('tabindex') !== null) {
    return 'generic';
  }
  return null;
}

function getName(element) {
  const ariaLabel = element.getAttribute('aria-label');
  if (ariaLabel) return ariaLabel;

  const labelledBy = element.getAttribute('aria-labelledby');
  if (labelledBy) {
    const labelElement = document.getElementById(labelledBy);
    if (labelElement) return labelElement.textContent?.trim() || '';
  }

  if (element.id) {
    const label = document.querySelector(`label[for="${element.id}"]`);
    if (label) return label.textContent?.trim() || '';
  }

  const parentLabel = element.closest('label');
  if (parentLabel && parentLabel !== element) {
    return parentLabel.textContent?.trim() || '';
  }

  const alt = element.getAttribute('alt');
  if (alt) return alt;

  const title = element.getAttribute('title');
  if (title) return title;

  const placeholder = element.getAttribute('placeholder');
  if (placeholder) return placeholder;

  const innerText = element.innerText?.trim();
  if (innerText) {
    return innerText.length > 100 ? innerText.substring(0, 100) + '...' : innerText;
  }

  return '';
}

function extractSemanticElementRef(element) {
  if (!element || element.nodeType !== Node.ELEMENT_NODE) return null;

  const rect = element.getBoundingClientRect();
  return {
    role: getRole(element),
    name: getName(element),
    dataId: element.getAttribute('data-id'),
    bbox: {
      x: rect.left + window.scrollX,
      y: rect.top + window.scrollY,
      width: rect.width,
      height: rect.height
    },
    clientBbox: element.getAttribute('data-tlbr-client'),
    screenBbox: element.getAttribute('data-tlbr-screen'),
    tagName: element.tagName.toLowerCase(),
    id: element.id || null,
    classList: element.classList.length > 0 ? Array.from(element.classList) : null
  };
}

// ============ Message Sending ============

function sendMessageToBackgroundScript(message) {
  message.id = messageIdCounter++;
  message.pageId = pageId;
  message.url = window.location.href;

  if (DEBUG) {
    console.log({ messageType: message.type, messageLength: JSON.stringify(message).length, message });
  }

  try {
    chrome.runtime.sendMessage(message);
  } catch (e) {
    // Extension context may be invalidated
  }
}

// ============ Record Mode Event Handlers ============

function handleUserEvent(event) {
  let eventTarget = event.target;

  if (!(eventTarget instanceof HTMLElement)) {
    eventTarget = eventTarget.activeElement || document.body;
  }

  const eventTargetId = generateElementIdAndBbox(eventTarget);
  const timestamp = Date.now() / 1000;
  const { visibleHTMLString, visibleHTMLDuration } = getVisibleHTMLString();

  // Calculate scroll displacement
  const currentScrollX = window.scrollX;
  const currentScrollY = window.scrollY;
  const scrollDeltaX = currentScrollX - lastScrollPosition.x;
  const scrollDeltaY = currentScrollY - lastScrollPosition.y;
  lastScrollPosition = { x: currentScrollX, y: currentScrollY };

  // Last mouse coordinates for scroll events
  const lastMouseClientX = coordMappings.x.client[coordMappings.x.client.length - 1] || -1;
  const lastMouseClientY = coordMappings.y.client[coordMappings.y.client.length - 1] || -1;

  const eventData = {
    type: 'USER_EVENT',
    eventType: event.type,
    targetId: eventTargetId,
    timestamp: timestamp,
    visibleHTMLString,
    visibleHTMLDuration,
    devicePixelRatio: window.devicePixelRatio,
    element: extractSemanticElementRef(eventTarget)
  };

  if (event instanceof KeyboardEvent) {
    eventData.key = event.key;
    eventData.code = event.code;
    eventData.shiftKey = event.shiftKey;
    eventData.ctrlKey = event.ctrlKey;
    eventData.altKey = event.altKey;
    eventData.metaKey = event.metaKey;
  } else if (event instanceof MouseEvent) {
    eventData.clientX = event.clientX;
    eventData.clientY = event.clientY;
    eventData.screenX = event.screenX;
    eventData.screenY = event.screenY;
    eventData.button = event.button;
    eventData.coordMappings = coordMappings;
  } else if (event.type === 'scroll') {
    eventData.scrollDeltaX = scrollDeltaX;
    eventData.scrollDeltaY = -scrollDeltaY; // Negative to match pynput
    eventData.clientX = lastMouseClientX;
    eventData.clientY = lastMouseClientY;
  }

  sendMessageToBackgroundScript(eventData);
}

// ============ Record Listeners ============

function attachRecordListeners() {
  if (recordListenersAttached) return;

  const eventTargetMap = {
    'click': document.body,
    'keydown': document.body,
    'keyup': document.body,
    'mousemove': document.body,
    'scroll': document
  };

  const eventDebounceDelayMap = {
    'click': 0,
    'keydown': 0,
    'keyup': 0,
    'mousemove': 100,
    'scroll': 100
  };

  const lastEventTimeMap = new Map();

  Object.entries(eventTargetMap).forEach(([eventType, target]) => {
    target.addEventListener(eventType, (event) => {
      const debounceDelay = eventDebounceDelayMap[eventType];
      const lastEventTime = lastEventTimeMap.get(eventType) || 0;
      const now = Date.now();

      if (now - lastEventTime >= debounceDelay) {
        handleUserEvent(event);
        lastEventTimeMap.set(eventType, now);
      }
    }, true);
  });

  // Track mouse for coordinate mappings
  ['mousedown', 'mouseup', 'mousemove'].forEach(eventType => {
    document.body.addEventListener(eventType, trackMouseEvent, true);
  });

  recordListenersAttached = true;
}

function detachRecordListeners() {
  // Note: In practice we'd need to store handler references to properly remove them
  // For now, just mark as detached
  recordListenersAttached = false;
}

// ============ Replay Mode ============

let debounceTimeoutId = null;
const DEBOUNCE_DELAY = 10;

function attachReplayObservers() {
  if (replayObserversAttached) return;

  setupIntersectionObserver();
  setupMutationObserver();
  setupScrollAndResizeListeners();
  replayObserversAttached = true;
}

function disconnectReplayObservers() {
  if (window.intersectionObserverInstance) {
    window.intersectionObserverInstance.disconnect();
  }
  if (window.mutationObserverInstance) {
    window.mutationObserverInstance.disconnect();
  }
  window.removeEventListener('scroll', handleScrollEvent, { passive: true });
  window.removeEventListener('resize', handleResizeEvent, { passive: true });
  replayObserversAttached = false;
}

function setupIntersectionObserver() {
  const observer = new IntersectionObserver(handleIntersection, {
    root: null,
    threshold: 0
  });
  document.querySelectorAll('*').forEach(element => observer.observe(element));
  window.intersectionObserverInstance = observer;
}

function handleIntersection(entries) {
  let shouldSendUpdate = false;
  entries.forEach(entry => {
    if (entry.isIntersecting) shouldSendUpdate = true;
  });
  if (shouldSendUpdate) debounceSendVisibleHTML('intersection');
}

function setupMutationObserver() {
  const observer = new MutationObserver(handleMutations);
  observer.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true
  });
  window.mutationObserverInstance = observer;
}

function handleMutations(mutationsList) {
  let shouldSendUpdate = false;

  for (const mutation of mutationsList) {
    for (const node of mutation.addedNodes) {
      if (node.nodeType === Node.ELEMENT_NODE && isVisible(node)) {
        shouldSendUpdate = true;
        break;
      }
    }
    if (shouldSendUpdate) break;

    for (const node of mutation.removedNodes) {
      if (node.nodeType === Node.ELEMENT_NODE && idToElementMap.has(node.getAttribute?.('data-id'))) {
        shouldSendUpdate = true;
        break;
      }
    }
    if (shouldSendUpdate) break;
  }

  if (shouldSendUpdate) debounceSendVisibleHTML('mutation');
}

function setupScrollAndResizeListeners() {
  window.addEventListener('scroll', handleScrollEvent, { passive: true });
  window.addEventListener('resize', handleResizeEvent, { passive: true });
}

function handleScrollEvent() {
  debounceSendVisibleHTML('scroll');
}

function handleResizeEvent() {
  debounceSendVisibleHTML('resize');
}

function debounceSendVisibleHTML(eventType) {
  if (debounceTimeoutId) clearTimeout(debounceTimeoutId);
  debounceTimeoutId = setTimeout(() => sendVisibleHTML(eventType), DEBOUNCE_DELAY);
}

function sendVisibleHTML(eventType) {
  const timestamp = Date.now() / 1000;
  const { visibleHTMLString, visibleHTMLDuration } = getVisibleHTMLString();

  sendMessageToBackgroundScript({
    type: 'DOM_EVENT',
    eventType: eventType,
    timestamp: timestamp,
    visibleHTMLString,
    visibleHTMLDuration
  });
}

// ============ Action Execution (Replay) ============

function handleExecuteAction(action) {
  if (!action) return;

  let element = null;

  // Find by data-id first (most reliable)
  if (action.targetId) {
    element = idToElementMap.get(action.targetId) || document.querySelector(`[data-id="${action.targetId}"]`);
  }

  // Fall back to CSS selector
  if (!element && action.cssSelector) {
    try {
      element = document.querySelector(action.cssSelector);
    } catch (e) {}
  }

  if (!element) {
    sendMessageToBackgroundScript({
      type: 'ERROR',
      code: 'ELEMENT_NOT_FOUND',
      message: `Could not locate element: ${action.targetId || action.cssSelector}`
    });
    return;
  }

  switch (action.type) {
    case 'click':
      element.click();
      break;
    case 'type':
      if (action.text) {
        element.focus();
        element.value = action.text;
        element.dispatchEvent(new Event('input', { bubbles: true }));
        element.dispatchEvent(new Event('change', { bubbles: true }));
      }
      break;
    case 'scroll':
      window.scrollBy(action.deltaX || 0, action.deltaY || 0);
      break;
    case 'focus':
      element.focus();
      break;
  }
}

// ============ Message Handling ============

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'SET_MODE') {
    setMode(message.mode || message.payload?.mode || 'idle');
    sendResponse({ success: true });
  } else if (message.type === 'EXECUTE_ACTION') {
    handleExecuteAction(message.action || message.payload?.action);
    sendResponse({ success: true });
  } else if (message.type === 'GET_VISIBLE_HTML') {
    const { visibleHTMLString, visibleHTMLDuration } = getVisibleHTMLString();
    sendResponse({ visibleHTMLString, visibleHTMLDuration });
  }
  return true;
});

// ============ Initialization ============

console.log('[OpenAdapt] Content script loaded');
