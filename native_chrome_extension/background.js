/*
On startup, connect to the "ping_pong" app.
*/
let port = chrome.runtime.connectNative("openadapt");

// Receive messages from the native messaging host
port.onMessage.addListener(onReceived);

// Handle received messages
function onReceived(response) {
    console.log(response);
}

// /*
// Listen for messages from the app.
// */
// port.onMessage.addListener((response) => {
//   console.log("Received: " + response);
//   // port.postMessage(message);
// });

// Listen for messages from the content script
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // Forward the message to the native messaging host
  port.postMessage(message);
});


// Send initial message to the native messaging host
port.postMessage("hello");


/*
On a click on the browser action, send the app a message.
*/
chrome.action.onClicked.addListener((tab) => {
  port.postMessage("Thanks! For using OpenAdapt DOM Listener.");
});


// DOM mutation observer
let observer = new MutationObserver(mutations => {
  for (let mutation of mutations) {
      // Send the mutation to the background script
      port.postMessage(mutation);
  }
});

// Start observing DOM mutations
observer.observe(document, { childList: true, subtree: true });
