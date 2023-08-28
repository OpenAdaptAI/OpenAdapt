/**
 * @file content.js
 * @description This file is injected into the web page and is responsible for
 * capturing DOM changes and sending them to the background script.
 */

let logged = false;
let ignoreAttributes = new Set();

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
    action: "captureDocumentState",
    documentBody: documentBody,
    documentHead: documentHead,
    url: page_url,
    timestamp: Date.now(),
  });
}

const observer = new MutationObserver((mutations) => {
  mutations.forEach((mutation) => {
    const { type, target } = mutation;
    const tagName = target.tagName.toLowerCase();
    const attributes = {};

    for (const attr of target.attributes) {
      attributes[attr.name] = attr.value;
    }

    const rect = target.getBoundingClientRect();
    const x = rect.left + window.scrollX;
    const y = rect.top + window.scrollY;
    const value = target.value;

    sendMessageToBackgroundScript({
      action: "mutation",
      type: type,
      tagName: tagName,
      attributes: attributes,
      x: x,
      y: y,
      value: value,
      url: window.location.href,
      timestamp: Date.now(),
    });
  });
});

observer.observe(document.body, {
  childList: true,
  subtree: true,
  attributes: true,
  attributeOldValue: true,
  characterData: true,
  characterDataOldValue: true,
});
captureDocumentState();
