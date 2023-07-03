// System Diagram: https://docs.google.com/presentation/d/106AXW3sBe7-7E-zIggnMnaUKUXWAj_aAuSxBspTDcGk/edit#slide=id.p

// Native Messaging port
let port = null;
const hostName = 'openadapt';


/*
 * Function to log messages to service-worker console
*/
function onReceived(response) {
  console.log(response);
}



/*
 * Function to listen for messages from content script and 
 * send them to the native app
*/
function messageListener(message, sender, sendResponse) {
  // console.log({ message, sender, sendResponse });
  port.postMessage(message);
}


port = chrome.runtime.connectNative(hostName);
port.onMessage.addListener(onReceived);
port.onDisconnect.addListener(function() {
  port = null;
});
chrome.runtime.onMessage.addListener(messageListener); // Listen for messages from content scripts
