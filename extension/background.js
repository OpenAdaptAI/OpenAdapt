let port = null;  // Native Messaging port
const hostName = 'mldsai';


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
