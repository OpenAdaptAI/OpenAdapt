
// Native Messaging port
let port = null;

// Handle received messages
function onReceived(response) {
  console.log(response);
}


const hostName = 'openadapt';
port = chrome.runtime.connectNative(hostName);
port.onMessage.addListener(onReceived);
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
