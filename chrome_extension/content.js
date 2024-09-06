const DEBUG = true;
const RETURN_FULL_DOCUMENT = false;
const elementIdMap = new WeakMap();
const idToElementMap = new Map(); // Reverse lookup map
let elementIdCounter = 0;
let messageIdCounter = 0;
// TODO: only track as many as necessary
const eventBuffer = [];
const maxBufferSize = 100;

// Function to track mouse events and maintain buffer size
function trackMouseEvent(event) {
  const { clientX, clientY, screenX, screenY } = event;
  eventBuffer.push({ clientX, clientY, screenX, screenY });

  // Maintain buffer size
  if (eventBuffer.length > maxBufferSize) {
    eventBuffer.shift();
  }
}

function sendMessageToBackgroundScript(message) {
  message.id = messageIdCounter++;
  if (DEBUG) {
    const messageType = message.type;
    const messageLength = JSON.stringify(message).length;
    console.log({ messageType, messageLength, message });
  }
  chrome.runtime.sendMessage(message);
}

function generateElementIdAndBbox(element) {
  let { top, left, bottom, right } = getScreenCoordinates(element);
  if (top == 0 && left == 0 && bottom == 0 && right == 0) {
    // not enough data points to get screen coordinates
    return
  }
  //console.log({ top, left, bottom, right });
  if (!elementIdMap.has(element)) {
    const newId = `elem-${elementIdCounter++}`;
    elementIdMap.set(element, newId);
    idToElementMap.set(newId, element); // Reverse mapping
    element.setAttribute('data-id', newId);
  }

  if (isVisible(element)) {
    let bboxScreen = `${top},${left},${bottom},${right}`;
    element.setAttribute('data-tlbr-screen', bboxScreen);

    ({ top, left, bottom, right } = element.getBoundingClientRect());
    
    let bboxClient = `${top},${left},${bottom},${right}`;
    element.setAttribute('data-tlbr-client', bboxClient);

  }
  return elementIdMap.get(element);
}

function getScreenCoordinates(element) {
  // Get the bounding client rectangle for the element
  const rect = element.getBoundingClientRect();
  const { top: clientTop, left: clientLeft, bottom: clientBottom, right: clientRight } = rect;

  // Assume recent mouse events are stored in eventBuffer
  if (eventBuffer.length < 2) {
    console.warn("Not enough data to compute screen coordinates.");
    return { top: 0, left: 0, bottom: 0, right: 0 };
  }

  // Use the last mouse event from the buffer
  const { clientX: cx1, clientY: cy1, screenX: sx1, screenY: sy1 } = eventBuffer[eventBuffer.length - 1];

  // Initialize variables for the second event
  let cx2, cy2, sx2, sy2;
  let found = false;

  // Iterate from the second last element until one is found that is not aligned
  for (let i = eventBuffer.length - 2; i >= 0; i--) {
    ({ clientX: cx2, clientY: cy2, screenX: sx2, screenY: sy2 } = eventBuffer[i]);

    // Check for vertical and horizontal alignment
    const isVerticalAligned = (cx1 === cx2);
    const isHorizontalAligned = (cy1 === cy2);

    if (!isVerticalAligned && !isHorizontalAligned) {
      found = true;
      break;
    }
  }

  // If no suitable event pair is found, log a warning and return default values
  if (!found) {
    console.warn(`No suitable event pair found; cannot compute conversion. ${eventBuffer.length}`);
    return { top: 0, left: 0, bottom: 0, right: 0 };
  }

  // Calculate scale factors and translation offsets for both axes
  const sxScale = (sx2 - sx1) / (cx2 - cx1);
  const syScale = (sy2 - sy1) / (cy2 - cy1);
  const sxOffset = sx1 - sxScale * cx1;
  const syOffset = sy1 - syScale * cy1;

  //console.log({ sxScale, syScale, sxOffset, syOffset });

  // Convert element's client bounding box to screen coordinates
  const screenTop = syScale * clientTop + syOffset;
  const screenLeft = sxScale * clientLeft + sxOffset;
  const screenBottom = syScale * clientBottom + syOffset;
  const screenRight = sxScale * clientRight + sxOffset;

  return { top: screenTop, left: screenLeft, bottom: screenBottom, right: screenRight };
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
          child.setAttribute('src', ''); // Replace the data content with "<snip>"
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
  /*
  if (eventBuffer.length < 2) {
    return;
  }
  */
  console.time('getVisibleHtmlString duration');

  // Step 1: Instrument the live DOM with data-id and data-bbox attributes
  instrumentLiveDomWithBbox();

  if (RETURN_FULL_DOCUMENT) {
    console.timeEnd('getVisibleHtmlString duration');
    return document.body.outerHTML;
  }

  // Step 2: Clone the body
  const clonedBody = document.body.cloneNode(true);

  // Step 3: Remove invisible elements from the cloned DOM
  cleanDomTree(clonedBody);

  // Step 4: Serialize the modified clone to a string
  const visibleHtmlString = clonedBody.outerHTML;
  console.timeEnd('getVisibleHtmlString duration');

  return visibleHtmlString;
}

function setVisibleHtmlString(startTime = null) {
  if (startTime === null) {
    startTime = performance.now();
  }

  const visibleHtmlString = getVisibleHtmlString();
  const visibleHtmlDuration = performance.now() - startTime;

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

  const { visibleHtmlString, visibleHtmlDuration } = setVisibleHtmlString();

  const eventData = {
    type: 'USER_EVENT',
    eventType: event.type,
    targetId: eventTargetId,
    url: window.location.href,
    timestamp: timestamp,
    visibleHtmlString,
    visibleHtmlDuration,
  };

  if (event instanceof KeyboardEvent) {
    eventData.key = event.key;
    eventData.code = event.code;
  } else if (event instanceof MouseEvent) {
    console.log({ event });
    eventData.screenX = event.screenX;
    eventData.screenY = event.screenY;
    eventData.clientX = event.clientX;
    eventData.clientY = event.clientY;
    eventData.button = event.button;
    validateCoordinates(event, eventTarget, 'client', 'clientX', 'clientY');
    validateCoordinates(event, eventTarget, 'screen', 'screenX', 'screenY');
  } else if (event instanceof InputEvent) {
    eventData.inputType = event.inputType;
    eventData.data = event.data;
  } else if (event instanceof FocusEvent) {
    eventData.relatedTarget = event.relatedTarget ? generateElementIdAndBbox(event.relatedTarget) : null;
  }

  sendMessageToBackgroundScript(eventData);
}

function setupMutationObserver() {
  const observer = new MutationObserver(handleMutations);
  observer.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true
  });
}

// Attach event listeners for user-generated events
function attachUserEventListeners() {
  const eventsToCapture = [
    'click',
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
    'click',
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
