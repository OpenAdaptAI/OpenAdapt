let port = null;  // Native Messaging port
const hostName = 'openadapt';


/*
 * Handle received messages
*/
function onReceived(response) {
  console.log(response);
}

/*
 * Handle disconnection from native app
*/
function onDisconnectedFromNativeHost() {
  console.log('Disconnected from native host');
  port = null;
}


port = chrome.runtime.connectNative(hostName);
port.onMessage.addListener(onReceived);
port.onDisconnect.addListener(onDisconnectedFromNativeHost);
console.log('Connected to native messaging host: ' + hostName);
port.postMessage({ text: "Hello, my_application" });

/*
* Forward messages from content scripts to the native app
*/
chrome.runtime.onMessage.addListener(function (message, sender, sendResponse) {
  if (message.action === 'logDOMChange') {
    port.postMessage(message.documentInfo);
    console.log('Message forwarded to native app');
  }
});