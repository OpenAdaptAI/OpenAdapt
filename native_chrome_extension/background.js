// Native Messaging port
const hostName = 'openadapt';
var port = chrome.runtime.connectNative('openadapt');


// Handle received messages
function onReceived(response) {
  console.log(response);
}


// Message listener for content script
function messageListener(message, sender, sendResponse) {
  port.postMessage(message);
}

console.log(port.name);
port.onMessage.addListener(onReceived);
chrome.runtime.onMessage.addListener(messageListener); // Listen for messages from content scripts
