// // Handle received messages
// function onReceived(response) {
//     console.log(response);
// }

// let port = chrome.runtime.connectNative("openadapt");
// port.onMessage.addListener(onReceived);  // Receive messages from the native messaging host
// port.postMessage("hello");

// /*
// On a click on the browser action, send the app a message.
// */
// chrome.action.onClicked.addListener((tab) => {
//   port.postMessage("Thanks! For using OpenAdapt DOM Listener.");
// });


// let observer = new MutationObserver(mutations => {
//   for(let mutation of mutations) {
//       chrome.runtime.sendNativeMessage("openadapt", mutation).then(onReceived);
//   }
// });
// observer.observe(document, { childList: true, subtree: true });


// Native Messaging port
let port = null;

// Handle received messages
function onReceived(response) {
  console.log(response);
}


const hostName = 'openadapt'; // Replace with your Native Messaging host name
port = chrome.runtime.connectNative(hostName);
port.onMessage.addListener(onReceived); // Receive messages from the Native Messaging host
port.postMessage('hello');


// Send document to the Native Messaging host
function sendDocument(documentHTML) {
  if (port) {
    const message = {
      type: 'document',
      html: documentHTML,
    };

    port.postMessage(message);
    console.log('Sent document to Native Messaging host:', message);
  }
}

// Message listener for content script
function messageListener(message, sender, sendResponse) {
  if (message.type === 'document') {
    sendDocument(message.html);
  }
}


// Listen for messages from content scripts
chrome.runtime.onMessage.addListener(messageListener);


/*
On a click on the browser action, send a message to the active tab's content script.
*/
chrome.action.onClicked.addListener((tab) => {
  chrome.tabs.sendMessage(tab.id, { type: 'getDocument' });
});
