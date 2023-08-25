/**
 * @file background.js
 * @description This file is responsible for communicating with the native
 * messaging host and the content script.
 * @see https://docs.google.com/presentation/d/106AXW3sBe7-7E-zIggnMnaUKUXWAj_aAuSxBspTDcGk/edit#slide=id.p
 */

const hostName = "openadapt";
var port = null; // Native Messaging port
var lastMsg = null;

/*
 * Handle received messages from browser.js
 */
function onReceived(response) {
  console.log(response);
}

function onDisconnected() {
  msg = "Failed to connect: " + chrome.runtime.lastError.message; // silence error
  port = null;
}

function connect() {
  port = chrome.runtime.connectNative(hostName);
  port.onMessage.addListener(onReceived);
  port.onDisconnect.addListener(onDisconnected);
}

/*
 * Message listener for content script
 */
function messageListener(message, sender, sendResponse) {
  const timestampThreshold = 30; // arbitrary threshold in milliseconds

  try {
    if (lastMsg !== null) {
      if (
        Math.abs(message.timestamp - lastMsg.timestamp) < timestampThreshold &&
        message.tagName === lastMsg.tagName &&
        message.action === lastMsg.action
      ) {
        return;
      }
    }
    console.log({ message, sender, sendResponse });
    port.postMessage(message); // send to browser.py (native messaging host)
    lastMsg = message;
  } catch (e) {
    connect();
  }
}
connect();
chrome.runtime.onMessage.addListener(messageListener);
