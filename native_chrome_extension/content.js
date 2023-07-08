/**
 * @file content.js
 * @description This file is injected into the web page and is responsible for
 * capturing DOM changes and sending them to the background script.
*/

let logged = false;
let ignoreAttributes = new Set();
let observer = null;


/*
 * Function to send a message to the background script
*/
function sendMessageToBackgroundScript(message) {
  chrome.runtime.sendMessage(message);
}


/*
 * Function to capture initial document state and 
 * send it to the background script
*/
function captureDocumentState() {
  const documentBody = document.body.outerHTML;
  const documentHead = document.head.outerHTML;
  const page_url = window.location.href;

  sendMessageToBackgroundScript({
    action: 'captureDocumentState',
    documentBody: documentBody,
    documentHead: documentHead,
    url: page_url
  });
}


/*
 * Function to detect DOM changes
*/
function detectDOMChanges(mutationsList) {
  // Send a message to the background script when a DOM change is detected
  if (mutationsList.length === 0) {
    return;
  }
  if (!logged) {
    console.log({ mutationsList });
    // logged = true;
    // debugger;
  }
  captureDocumentState();
}


/*
 * Mutation observer callback function
*/
function handleMutation(mutationsList) {
  const filteredMutations = [];

  // Filter out mutations related to ignored attributes
  mutationsList.forEach((mutation) => {
    if (ignoreAttributes.has(mutation.attributeName)) {
      return;
    }
    filteredMutations.push(mutation);
  });

  detectDOMChanges(filteredMutations);
}


/*
 * Function to start observing DOM changes
*/
function startObservingDOMChanges() {
  // Create a new mutation observer if it doesn't exist
  if (!observer) {
    observer = new MutationObserver(handleMutation);
  }

  // Start observing DOM changes
  observer.observe(document, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: Array.from(ignoreAttributes),
    characterData: true
  });
}


/*
 * Function to stop observing DOM changes
*/
function stopObservingDOMChanges() {
  if (observer) {
    observer.disconnect();
    observer = null;
  }
}


/*
 * Function to get element positions
*/
function getElementPositions() {
  const elements = document.getElementsByTagName("*");

  for (const element of elements) {
    const rect = element.getBoundingClientRect();
    const attrs = ['top', 'right', 'bottom', 'left', 'width', 'height'];
    for (const attr of attrs) {
      element.setAttribute(`data-${attr}`, rect[attr]);
      ignoreAttributes.add(`data-${attr}`);
    }
  }
}


getElementPositions();

captureDocumentState();

startObservingDOMChanges();
