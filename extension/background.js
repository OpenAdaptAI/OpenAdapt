// Native Messaging port
let port = null;
const hostName = 'mldsai';


// Handle received messages
function onReceived(response) {
  console.log(response);
}

function onDisconnect() {
  console.log("Failed to connect: " + chrome.runtime.lastError.message);
}


// Message listener for content script
function messageListener(message, sender, sendResponse) {
  port.postMessage(message);
}

port = chrome.runtime.connectNative(hostName);
port.onMessage.addListener(onReceived);
port.onDisconnect.addListener(onDisconnect);
chrome.runtime.onMessage.addListener(messageListener); // Listen for messages from content scripts
