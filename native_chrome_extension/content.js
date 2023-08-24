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
    action: "captureDocumentState",
    documentBody: documentBody,
    documentHead: documentHead,
    url: page_url,
    timestamp: Date.now(),
  });
}

function handleElementClick(event) {
  const element = event.target;
  const tagName = element.tagName;
  const attributes = {};

  for (const attr of element.attributes) {
    attributes[attr.name] = attr.value;
  }

  const rect = element.getBoundingClientRect();
  const x = rect.left + window.scrollX;
  const y = rect.top + window.scrollY;

  sendMessageToBackgroundScript({
    action: "elementClicked",
    tagName: tagName,
    attributes: attributes,
    x: x,
    y: y,
    url: window.location.href,
    timestamp: Date.now(),
  });
}

function debounce(func, delay) {
  let timerId;
  return function (...args) {
    if (timerId) {
      clearTimeout(timerId);
    }
    timerId = setTimeout(() => {
      func.apply(this, args);
      timerId = null;
    }, delay);
  };
}

function handleDebouncedInput(event) {
  const element = event.target;
  const tagName = element.tagName;
  const attributes = {};

  for (const attr of element.attributes) {
    attributes[attr.name] = attr.value;
  }

  const rect = element.getBoundingClientRect();
  const x = rect.left + window.scrollX;
  const y = rect.top + window.scrollY;
  const value = element.value;

  sendMessageToBackgroundScript({
    action: "elementInput",
    tagName: tagName,
    attributes: attributes,
    x: x,
    y: y,
    value: value,
    url: window.location.href,
    timestamp: Date.now(),
  });
}

const debouncedInputHandler = debounce(handleDebouncedInput, 500);

function handleElementInput(event) {
  debouncedInputHandler(event);
}

function addEventListeners() {
  const elements = document.getElementsByTagName("*");

  for (const element of elements) {
    element.addEventListener("click", handleElementClick);
    element.addEventListener("input", handleElementInput);
  }
}

addEventListeners();
captureDocumentState();
