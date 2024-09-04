// Global variables to store the most recent visible HTML string and its timestamp
let latestVisibleHtmlString = '';
let latestVisibleHtmlTimestamp = 0;

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
    eventData.button = event.button;
  } else if (event instanceof InputEvent) {
    eventData.inputType = event.inputType;
    eventData.data = event.data;
  } else if (event instanceof FocusEvent) {
    eventData.relatedTarget = event.relatedTarget ? generateElementIdAndBbox(event.relatedTarget) : null;
  }

  sendMessageToBackgroundScript(eventData);
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

// Initial setup
setupMutationObserver();
attachUserEventListeners();
attachWindowEventListeners();
sendVisibleHtmlString(); // Initialize the visible HTML
