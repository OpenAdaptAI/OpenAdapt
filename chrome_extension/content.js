const DEBUG = true;
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

  // Ensure only the latest 2 distinct coordinate mappings per dimension are kept
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
  // ignore invisible elements
  if (!isVisible(element)) {
    return;
  }

  // set id
  if (!elementIdMap.has(element)) {
    const newId = `elem-${elementIdCounter++}`;
    elementIdMap.set(element, newId);
    idToElementMap.set(newId, element); // Reverse mapping
    element.setAttribute('data-id', newId);
  }

  // set client bbox
  let { top, left, bottom, right } = element.getBoundingClientRect();
  let bboxClient = `${top},${left},${bottom},${right}`;
  element.setAttribute('data-tlbr-client', bboxClient);

  // set screen bbox
  if (SET_SCREEN_COORDS) {
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

function getVisibleHtmlString() {
  const startTime = performance.now();

  // Step 1: Instrument the live DOM with data-id and data-bbox attributes
  instrumentLiveDomWithBbox();

  if (RETURN_FULL_DOCUMENT) {
    const visibleHtmlDuration = performance.now() - startTime;
    console.log({ visibleHtmlDuration });
    const visibleHtmlString = document.body.outerHTML;
    return { visibleHtmlString, visibleHtmlDuration };
  }

  // Step 2: Clone the body
  const clonedBody = document.body.cloneNode(true);

  // Step 3: Remove invisible elements from the cloned DOM
  cleanDomTree(clonedBody);

  // Step 4: Serialize the modified clone to a string
  const visibleHtmlString = clonedBody.outerHTML;

  const visibleHtmlDuration = performance.now() - startTime;
  console.log({ visibleHtmlDuration });

  return { visibleHtmlString, visibleHtmlDuration };
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

function handleUserGeneratedEvent(event) {
  const eventTarget = event.target;
  const eventTargetId = generateElementIdAndBbox(eventTarget);
  const timestamp = Date.now() / 1000;  // Convert to Python-compatible seconds

  const { visibleHtmlString, visibleHtmlDuration } = getVisibleHtmlString();

  const eventData = {
    type: 'USER_EVENT',
    eventType: event.type,
    targetId: eventTargetId,
    timestamp: timestamp,
    visibleHtmlString,
    visibleHtmlDuration,
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
    document.body.addEventListener(eventType, handleUserGeneratedEvent, true);
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

// Initial setup
attachUserEventListeners();
attachInstrumentationEventListeners();
