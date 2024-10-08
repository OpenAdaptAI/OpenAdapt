const DEBUG = true;

if (!DEBUG) {
  console.debug = function() {};
}

let currentMode = "idle"; // Default mode is 'idle'
let recordListenersAttached = false; // Track if record listeners are currently attached
let replayObserversAttached = false; // Track if replay observers are currently attached

// Listen for messages from the background script or Python process
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  console.log("Received message:", message);
  if (message.type === 'SET_MODE') {
    currentMode = message.mode;
    console.log(`Mode set to: ${currentMode}`);

    // Attach or detach listeners based on mode
    if (currentMode === 'record') {
      if (!recordListenersAttached) attachRecordListeners();
      if (replayObserversAttached) disconnectReplayObservers(); // Detach replay observers if needed
    } else if (currentMode === 'replay') {
      debounceSendVisibleHTML('setmode');
      if (!replayObserversAttached) attachReplayObservers();
      if (recordListenersAttached) detachRecordListeners(); // Detach record listeners if needed
    } else if (currentMode === 'idle') {
      if (recordListenersAttached) detachRecordListeners();
      if (replayObserversAttached) disconnectReplayObservers();
    }
  }
});

// Attach event listeners for recording mode
function attachRecordListeners() {
  if (!recordListenersAttached) {
    attachUserEventListeners();
    attachInstrumentationEventListeners();
    recordListenersAttached = true;
  }
}

// Attach user-generated event listeners
function attachUserEventListeners() {
  console.log("attachUserEventListeners()");
  const eventsToCapture = ['click', 'keydown', 'keyup'];

  eventsToCapture.forEach(eventType => {
    document.body.addEventListener(eventType, handleUserEvent, true);
  });
}

// Attach instrumentation event listeners
function attachInstrumentationEventListeners() {
  console.log("attachInstrumentationEventListeners()");
  const eventsToCapture = ['mousedown', 'mouseup', 'mousemove'];

  eventsToCapture.forEach(eventType => {
    document.body.addEventListener(eventType, trackMouseEvent, true);
  });
}

// Detach all event listeners for recording mode
function detachRecordListeners() {
  const eventsToCapture = [
    'click', 'keydown', 'keyup', 'mousedown', 'mouseup', 'mousemove'
  ];

  eventsToCapture.forEach(eventType => {
    document.body.removeEventListener(eventType, handleUserEvent, true);
    document.body.removeEventListener(eventType, trackMouseEvent, true);
  });

  recordListenersAttached = false;
}

// Attach observers for replay mode
function attachReplayObservers() {
  if (!replayObserversAttached) {
    setupIntersectionObserver();
    setupMutationObserver();
    setupScrollAndResizeListeners();
    replayObserversAttached = true;
  }
}

// Disconnect observers for replay mode
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

// Handle scroll events
function handleScrollEvent(event) {
  debounceSendVisibleHTML(event.type);
}

// Handle resize events
function handleResizeEvent(event) {
  debounceSendVisibleHTML(event.type);
}

/*
 * Record
 */

const RETURN_FULL_DOCUMENT = false;
const MAX_COORDS = 3;
const SET_SCREEN_COORDS = false;
const elementIdMap = new WeakMap();
const idToElementMap = new Map(); // Reverse lookup map
let elementIdCounter = 0;
let messageIdCounter = 0;
const pageId = `${Date.now()}-${Math.random()}`;
const coordMappings = {
  x: { client: [], screen: [] },
  y: { client: [], screen: [] }
};

function trackMouseEvent(event) {
  const { clientX, clientY, screenX, screenY } = event;

  const prevCoordMappingsStr = JSON.stringify(coordMappings);

  // Track x-coordinates
  updateCoordinateMappings('x', clientX, screenX);
  // Track y-coordinates
  updateCoordinateMappings('y', clientY, screenY);

  // Ensure only the latest distinct coordinate mappings per dimension are kept
  trimMappings(coordMappings.x);
  trimMappings(coordMappings.y);

  const coordMappingsStr = JSON.stringify(coordMappings);
  if (DEBUG && coordMappingsStr != prevCoordMappingsStr) {
    console.log(JSON.stringify(coordMappings));
  }
}

function updateCoordinateMappings(dim, clientCoord, screenCoord) {
  const coordMap = coordMappings[dim];

  // Check if current event's client coordinate matches any of the existing ones
  if (coordMap.client.includes(clientCoord)) {
    // Update screen coordinate for the matching client coordinate
    coordMap.screen[coordMap.client.indexOf(clientCoord)] = screenCoord;
  } else {
    // Add new coordinate mapping
    coordMap.client.push(clientCoord);
    coordMap.screen.push(screenCoord);
  }
}

function trimMappings(coordMap) {
  // Keep only the latest distinct coordinate mappings
  if (coordMap.client.length > MAX_COORDS) {
    coordMap.client.shift();
    coordMap.screen.shift();
  }
}

function getConversionPoints() {
  const { x, y } = coordMappings;

  // Ensure we have at least two points for each dimension
  if (x.client.length < 2 || y.client.length < 2) {
    return { sxScale: null, syScale: null, sxOffset: null, syOffset: null };
  }

  // Use linear regression or least squares fitting to determine scale factors and offsets
  const { scale: sxScale, offset: sxOffset } = fitLinearTransformation(x.client, x.screen);
  const { scale: syScale, offset: syOffset } = fitLinearTransformation(y.client, y.screen);

  return {
    sxScale, syScale, sxOffset, syOffset
  };
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

  // If conversion points are not sufficient, return null coordinates
  if (conversionPoints.sxScale === null) {
    return { top: null, left: null, bottom: null, right: null };
  }

  const { sxScale, syScale, sxOffset, syOffset } = conversionPoints;

  // Convert element's client bounding box to screen coordinates
  const screenTop = syScale * clientTop + syOffset;
  const screenLeft = sxScale * clientLeft + sxOffset;
  const screenBottom = syScale * clientBottom + syOffset;
  const screenRight = sxScale * clientRight + sxOffset;

  return { top: screenTop, left: screenLeft, bottom: screenBottom, right: screenRight };
}

function sendMessageToBackgroundScript(message) {
  message.id = messageIdCounter++;
  message.pageId = pageId;
  message.url = window.location.href;
  if (DEBUG) {
    const messageType = message.type;
    const messageLength = JSON.stringify(message).length;
    console.log({ messageType, messageLength, message });
  }
  chrome.runtime.sendMessage(message);
}

function generateElementIdAndBbox(element) {
  console.debug(`[generateElementIdAndBbox] Processing element: ${element.tagName}`);

  // ignore invisible elements
  if (!isVisible(element)) {
    console.debug(`[generateElementIdAndBbox] Element is not visible: ${element.tagName}`);
    return;
  }

  // set id
  if (!elementIdMap.has(element)) {
    const newId = `elem-${elementIdCounter++}`;
    console.debug(`[generateElementIdAndBbox] Generated new ID: ${newId} for element: ${element.tagName}`);
    elementIdMap.set(element, newId);
    idToElementMap.set(newId, element); // Reverse mapping
    element.setAttribute('data-id', newId);
  }

  // TODO: store bounding boxes in a map instead of in DOM attributes

  // set client bbox
  let { top, left, bottom, right } = element.getBoundingClientRect();
  let bboxClient = `${top},${left},${bottom},${right}`;
  element.setAttribute('data-tlbr-client', bboxClient);

  // set screen bbox
  if (SET_SCREEN_COORDS) {
    // XXX TODO: support in replay mode, or remove altogether
    ({ top, left, bottom, right } = getScreenCoordinates(element));
    if (top == null) {
      // not enough data points to get screen coordinates
      return
    }
    let bboxScreen = `${top},${left},${bottom},${right}`;
    element.setAttribute('data-tlbr-screen', bboxScreen);
  }

  return elementIdMap.get(element);
}

function instrumentLiveDomWithBbox() {
  document.querySelectorAll('*').forEach(element => generateElementIdAndBbox(element));
}

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

function cleanDomTree(node) {
  const children = Array.from(node.childNodes); // Use childNodes to include all types of child nodes
  for (const child of children) {
    if (child.nodeType === Node.ELEMENT_NODE) {
      // Check for img elements with src="data..."
      if (child.tagName === 'IMG' && child.hasAttribute('src')) {
        const src = child.getAttribute('src');
        if (src.startsWith('data:')) {
          //const [metadata] = src.split(','); // Extract the metadata part (e.g., "data:image/jpeg;base64")
          //child.setAttribute('src', `${metadata}<snip>`); // Replace the data content with "<snip>"
          // The above triggers net::ERR_INVALID_URL, so just remove it for now
          child.setAttribute('src', '');
        }
      }

      const originalId = child.getAttribute('data-id');
      if (originalId) {
        const originalElement = idToElementMap.get(originalId);
        if (!originalElement || !isVisible(originalElement)) {
          node.removeChild(child);
        } else {
          cleanDomTree(child); // Recursive call for child nodes
        }
      }
    } else if (child.nodeType === Node.COMMENT_NODE) {
      node.removeChild(child); // Remove comments
    } else if (child.nodeType === Node.TEXT_NODE) {
      // Strip newlines and whitespace-only text nodes
      const trimmedText = child.textContent.replace(/\s+/g, ' ').trim();
      if (trimmedText.length === 0) {
        node.removeChild(child);
      } else {
        child.textContent = trimmedText; // Replace the text content with stripped version
      }
    }
  }
}

function getVisibleHTMLString() {
  const startTime = performance.now();

  // Step 1: Instrument the live DOM with data-id and data-bbox attributes
  instrumentLiveDomWithBbox();

  if (RETURN_FULL_DOCUMENT) {
    const visibleHTMLDuration = performance.now() - startTime;
    console.log({ visibleHTMLDuration });
    const visibleHTMLString = document.body.outerHTML;
    return { visibleHTMLString, visibleHTMLDuration };
  }

  // Step 2: Clone the body
  const clonedBody = document.body.cloneNode(true);

  // Step 3: Remove invisible elements from the cloned DOM
  cleanDomTree(clonedBody);

  // Step 4: Serialize the modified clone to a string
  const visibleHTMLString = clonedBody.outerHTML;

  const visibleHTMLDuration = performance.now() - startTime;
  console.debug({ visibleHTMLDuration });

  return { visibleHTMLString, visibleHTMLDuration };
}

/**
 * Validates MouseEvent coordinates against bounding boxes for both client and screen.
 * @param {MouseEvent} event - The mouse event containing coordinates.
 * @param {HTMLElement} eventTarget - The target element of the mouse event.
 * @param {string} attrType - The type of attribute to validate ('client' or 'screen').
 * @param {string} coordX - The X coordinate to validate (clientX or screenX).
 * @param {string} coordY - The Y coordinate to validate (clientY or screenY).
 */
function validateCoordinates(event, eventTarget, attrType, coordX, coordY) {
  const attr = `data-tlbr-${attrType}`
  const bboxAttr = eventTarget.getAttribute(attr);
  if (!bboxAttr) {
    console.warn(`${attr} is empty`);
    return;
  }
  const [top, left, bottom, right] = bboxAttr.split(',').map(parseFloat);
  const x = event[coordX];
  const y = event[coordY];

  if (x < left || x > right || y < top || y > bottom) {
    console.warn(`${attrType} coordinates outside:`, JSON.stringify({
      [coordX]: x,
      [coordY]: y,
      bbox: { top, left, bottom, right },
    }));
    console.log(JSON.stringify({ devicePixelRatio, innerHeight, innerWidth, outerHeight, outerWidth, scrollY, scrollX, pageXOffset, pageYOffset, screenTop, screenLeft }));
  } else {
    console.log(`${attrType} coordinates inside:`, JSON.stringify({
      [coordX]: x,
      [coordY]: y,
      bbox: { top, left, bottom, right },
    }));
  }
}

function handleUserEvent(event) {
  const eventTarget = event.target;
  const eventTargetId = generateElementIdAndBbox(eventTarget);
  const timestamp = Date.now() / 1000;  // Convert to Python-compatible seconds

  const { visibleHTMLString, visibleHTMLDuration } = getVisibleHTMLString();

  const eventData = {
    type: 'USER_EVENT',
    eventType: event.type,
    targetId: eventTargetId,
    timestamp: timestamp,
    visibleHTMLString,
    visibleHTMLDuration,
    devicePixelRatio,
  };

  if (event instanceof KeyboardEvent) {
    eventData.key = event.key;
    eventData.code = event.code;
  } else if (event instanceof MouseEvent) {
    eventData.clientX = event.clientX;
    eventData.clientY = event.clientY;
    eventData.screenX = event.screenX;
    eventData.screenY = event.screenY;
    eventData.button = event.button;
    eventData.coordMappings = coordMappings;
    validateCoordinates(event, eventTarget, 'client', 'clientX', 'clientY');
    if (SET_SCREEN_COORDS) {
      validateCoordinates(event, eventTarget, 'screen', 'screenX', 'screenY');
    }
  }
  sendMessageToBackgroundScript(eventData);
}

// Attach event listeners for user-generated events
function attachUserEventListeners() {
  const eventsToCapture = [
    'click',
    // input events are triggered after the DOM change is written, so we can't use them
    // (since the resulting HTML would not look as the DOM was at the time the
    // user took the action, i.e. immediately before)
    //'input',
    'keydown',
    'keyup',
  ];

  eventsToCapture.forEach(eventType => {
    document.body.addEventListener(eventType, handleUserEvent, true);
  });
}

function attachInstrumentationEventListeners() {
  const eventsToCapture = [
    'mousedown',
    'mouseup',
    'mousemove',
  ];
  eventsToCapture.forEach(eventType => {
    document.body.addEventListener(eventType, trackMouseEvent, true);
  });
}

/*
 * Replay
 */

let debounceTimeoutId = null; // Timeout ID for debouncing
const DEBOUNCE_DELAY = 10;

function setupIntersectionObserver() {
  const observer = new IntersectionObserver(handleIntersection, {
    root: null, // Use the viewport as the root
    threshold: 0 // Consider an element visible if any part of it is in view
  });

  document.querySelectorAll('*').forEach(element => observer.observe(element));
}

function handleIntersection(entries) {
  let shouldSendUpdate = false;
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      shouldSendUpdate = true;
    }
  });
  if (shouldSendUpdate) {
    debounceSendVisibleHTML('intersection');
  }
}

function setupMutationObserver() {
  const observer = new MutationObserver(handleMutations);
  observer.observe(document.body, {
    childList: true,
    // XXX this results in continuous DOM_EVENT messages on some websites (e.g. ChatGPT)
    subtree: true,
    attributes: true
  });
}

function handleMutations(mutationsList) {
  const startTime = performance.now(); // Capture start time for the instrumentation
  console.debug(`[handleMutations] Start handling ${mutationsList.length} mutations at ${startTime}`);

  let shouldSendUpdate = false;

  for (const mutation of mutationsList) {
    console.debug(`[handleMutations] Mutation type: ${mutation.type}, target: ${mutation.target.tagName}`);
    for (const node of mutation.addedNodes) {
      if (node.nodeType === Node.ELEMENT_NODE) {
        console.debug(`[handleMutations] Added node: ${node.tagName}`);

        // Uncommenting this freezes some websites (e.g. ChatGPT).
        // It should not be necessary to call this here since it is also called in
        // getVisibleHTMLString.
        //generateElementIdAndBbox(node); // Generate a new ID and bbox for the added node

        if (isVisible(node)) {
          shouldSendUpdate = true;
          break; // Exit the loop early
        }
      }
    }
    if (shouldSendUpdate) break; // Exit outer loop if update is needed

    for (const node of mutation.removedNodes) {
      console.log(`[handleMutations] Removed node: ${node.tagName}`);
      if (node.nodeType === Node.ELEMENT_NODE && idToElementMap.has(node.getAttribute('data-id'))) {
        shouldSendUpdate = true;
        break; // Exit the loop early
      }
    }
    if (shouldSendUpdate) break; // Exit outer loop if update is needed
  }

  const endTime = performance.now();
  console.debug(`[handleMutations] Finished handling mutations. Duration: ${endTime - startTime}ms`);

  if (shouldSendUpdate) {
    debounceSendVisibleHTML('mutation');
  }
}

function debounceSendVisibleHTML(eventType) {
  // Clear the previous timeout, if any
  if (debounceTimeoutId) {
    clearTimeout(debounceTimeoutId);
  }

  console.debug(`[debounceSendVisibleHTML] Debouncing visible HTML send for event: ${eventType}`);
  // Set a new timeout
  debounceTimeoutId = setTimeout(() => {
    sendVisibleHTML(eventType);
  }, DEBOUNCE_DELAY);
}

function sendVisibleHTML(eventType) {
  console.debug(`Handling DOM event: ${eventType}`);
  const timestamp = Date.now() / 1000;  // Convert to Python-compatible seconds

  const { visibleHTMLString, visibleHTMLDuration } = getVisibleHTMLString();

  const eventData = {
    type: 'DOM_EVENT',
    eventType: eventType,
    timestamp: timestamp,
    visibleHTMLString,
    visibleHTMLDuration,
  };

  sendMessageToBackgroundScript(eventData);
}

function setupScrollAndResizeListeners() {
  window.addEventListener('scroll', handleScrollEvent, { passive: true });
  window.addEventListener('resize', handleResizeEvent, { passive: true });
}

/* Debugging */

const DEBUG_DRAW = false;  // Flag for drawing bounding boxes

// Start continuous drawing if DEBUG_DRAW is enabled
if (DEBUG_DRAW) {
  startDrawingBoundingBoxes();
}

/**
 * Start continuously drawing bounding boxes for visible elements.
 */
function startDrawingBoundingBoxes() {
  function drawBoundingBoxesLoop() {
    // Clean up existing bounding boxes before drawing new ones
    cleanUpBoundingBoxes();

    // Query all visible elements and draw their bounding boxes
    document.querySelectorAll('*').forEach(element => {
      if (isVisible(element)) {
        drawBoundingBoxForElement(element);
      }
    });

    // Use requestAnimationFrame for continuous updates without performance impact
    requestAnimationFrame(drawBoundingBoxesLoop);
  }

  // Kick off the loop
  drawBoundingBoxesLoop();
}

/**
 * Draw a bounding box for the given element.
 * Uses client coordinates.
 * @param {HTMLElement} element - The DOM element to draw the bounding box for.
 */
function drawBoundingBoxForElement(element) {
  const { top, left, bottom, right } = element.getBoundingClientRect();

  // Create and style the overlay to represent the bounding box
  let bboxOverlay = document.createElement('div');
  bboxOverlay.style.position = 'absolute';
  bboxOverlay.style.border = '2px solid red';
  bboxOverlay.style.top = `${top + window.scrollY}px`;  // Adjust for scrolling
  bboxOverlay.style.left = `${left + window.scrollX}px`;  // Adjust for scrolling
  bboxOverlay.style.width = `${right - left}px`;
  bboxOverlay.style.height = `${bottom - top}px`;
  bboxOverlay.style.pointerEvents = 'none';  // Prevent interference with normal element interactions
  bboxOverlay.style.zIndex = '9999';  // Ensure it's drawn on top
  bboxOverlay.setAttribute('data-debug-bbox', element.getAttribute('data-id') || '');

  // Append the overlay to the body
  document.body.appendChild(bboxOverlay);
}

/**
 * Clean up all existing bounding boxes to prevent overlapping or lingering overlays.
 */
function cleanUpBoundingBoxes() {
  document.querySelectorAll('[data-debug-bbox]').forEach(overlay => overlay.remove());
}
