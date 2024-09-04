const elementIdMap = new WeakMap();
const idToElementMap = new Map(); // Reverse lookup map
let idCounter = 0;
const debug = true; // Global debug flag
let latestVisibleHtmlString = '';
let latestVisibleHtmlTimestamp = 0;

function sendMessageToBackgroundScript(message) {
  if (debug) {
    const messageType = message.type;
    const messageLength = JSON.stringify(message).length;
    console.log({ messageType, messageLength, message });
  }
  chrome.runtime.sendMessage(message);
}

function generateElementIdAndBbox(element) {
  if (!elementIdMap.has(element)) {
    const newId = `elem-${idCounter++}`;
    elementIdMap.set(element, newId);
    idToElementMap.set(newId, element); // Reverse mapping
    element.setAttribute('data-id', newId);

    if (isVisible(element)) {
      const { top, left, bottom, right } = getScreenCoordinates(element);
      const bbox = `${top},${left},${bottom},${right}`;
      element.setAttribute('data-tlbr', bbox);
    }
  }
  return elementIdMap.get(element);
}

function getScreenCoordinates(element) {
  const rect = element.getBoundingClientRect();
  const screenX = window.screenX + window.outerWidth - window.innerWidth;
  const screenY = window.screenY + window.outerHeight - window.innerHeight;
  const top = rect.top + screenY;
  const left = rect.left + screenX;
  const bottom = rect.bottom + screenY;
  const right = rect.right + screenX;

  return { top, left, bottom, right };
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
          const [metadata] = src.split(','); // Extract the metadata part (e.g., "data:image/jpeg;base64")
          child.setAttribute('src', `${metadata}<snip>`); // Replace the data content with "<snip>"
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
  console.time('getVisibleHtmlString duration');

  // Step 1: Instrument the live DOM with data-id and data-bbox attributes
  instrumentLiveDomWithBbox();

  // Step 2: Clone the body
  const clonedBody = document.body.cloneNode(true);

  // Step 3: Remove invisible elements from the cloned DOM
  cleanDomTree(clonedBody);

  // Step 4: Serialize the modified clone to a string
  const visibleHtmlString = clonedBody.outerHTML;
  console.timeEnd('getVisibleHtmlString duration');

  return visibleHtmlString;
}

function handleIntersection(entries) {
  let shouldSendUpdate = false;
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      shouldSendUpdate = true;
    }
  });
  if (shouldSendUpdate) {
    sendVisibleHtmlString(null);
  }
}

function sendVisibleHtmlString(startTime = null) {
  if (startTime === null) {
    startTime = performance.now();
  }

  const timestamp = Date.now() / 1000;  // Convert to Python-compatible seconds
  const visibleHtmlString = getVisibleHtmlString();
  
  // Update the global state with the most recent visible HTML and its timestamp
  latestVisibleHtmlString = visibleHtmlString;
  latestVisibleHtmlTimestamp = timestamp;
}

function handleUserGeneratedEvent(event) {
  const eventTarget = event.target;
  const eventTargetId = generateElementIdAndBbox(eventTarget);
  const timestamp = Date.now() / 1000;  // Convert to Python-compatible seconds

  const eventData = {
    type: 'USER_EVENT',
    eventType: event.type,
    targetId: eventTargetId,
    url: window.location.href,
    timestamp: timestamp,
    // Attach the most recent visible HTML data
    visibleHtmlData: latestVisibleHtmlString,
    visibleHtmlTimestamp: latestVisibleHtmlTimestamp,
  };

  // Add event-specific data
  if (event instanceof KeyboardEvent) {
    eventData.key = event.key;
    eventData.code = event.code;
  } else if (event instanceof MouseEvent) {
    eventData.clientX = event.clientX;
    eventData.clientY = event.clientY;
    eventData.devicePixelRatio = window.devicePixelRatio;
    eventData.button = event.button;
  } else if (event instanceof InputEvent) {
    eventData.inputType = event.inputType;
    eventData.data = event.data;
  } else if (event instanceof FocusEvent) {
    eventData.relatedTarget = event.relatedTarget ? generateElementIdAndBbox(event.relatedTarget) : null;
  }

  sendMessageToBackgroundScript(eventData);
}

function setupIntersectionObserver() {
  const observer = new IntersectionObserver(handleIntersection, {
    root: null, // Use the viewport as the root
    threshold: 0 // Consider an element visible if any part of it is in view
  });

  document.querySelectorAll('*').forEach(element => observer.observe(element));
}


function setupMutationObserver() {
  const observer = new MutationObserver(handleMutations);
  observer.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true
  });
}


// Ensure VISIBLE_HTML data is updated on mutations
function handleMutations(mutationsList) {
  const startTime = performance.now(); // Capture start time for instrumentation

  let shouldUpdateVisibleHtml = false;
  mutationsList.forEach(mutation => {
    mutation.addedNodes.forEach(node => {
      if (node.nodeType === Node.ELEMENT_NODE) {
        generateElementIdAndBbox(node); // Generate a new ID and bbox for the added node
        if (isVisible(node)) {
          shouldUpdateVisibleHtml = true;
        }
      }
    });
    mutation.removedNodes.forEach(node => {
      if (node.nodeType === Node.ELEMENT_NODE && idToElementMap.has(node.getAttribute('data-id'))) {
        shouldUpdateVisibleHtml = true;
      }
    });
  });

  if (shouldUpdateVisibleHtml) {
    sendVisibleHtmlString(startTime); // Update the most recent visible HTML
  }
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
    'input',
    'keydown',
    'keyup',
  ];

  eventsToCapture.forEach(eventType => {
    document.body.addEventListener(eventType, handleUserGeneratedEvent, true);
  });
}

function attachWindowEventListeners() {
  window.addEventListener('focus', sendVisibleHtmlString);
}

// Initial setup
setupIntersectionObserver();
setupMutationObserver();
attachUserEventListeners();
attachWindowEventListeners();
sendVisibleHtmlString();
