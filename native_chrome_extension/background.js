let port = null;  // Native Messaging port
const hostName = 'openadapt';


/*
 * Handle received messages
*/
function onReceived(response) {
  console.log(response);
}


/*
 * Forward messages from content scripts to the native app
*/
function messageListener(message, sender, sendResponse) {
  port.postMessage(message);
}

port = chrome.runtime.connectNative(hostName);
port.onMessage.addListener(onReceived);
chrome.runtime.onMessage.addListener(messageListener);
