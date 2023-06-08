let port = null;  // Native Messaging port
const hostName = 'openadapt';


/*
 * Handle received messages
*/
function onReceived(response) {
  console.log(response);
}


port = chrome.runtime.connectNative(hostName);
port.onMessage.addListener(onReceived);
console.log('Connected to native messaging host: ' + hostName);


/*
* Forward messages from content scripts to the native app
*/
function messageListener(message, sender, sendResponse) {
  port.postMessage(message);
}


chrome.runtime.onMessage.addListener(messageListener);