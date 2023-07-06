// System Diagram: https://docs.google.com/presentation/d/106AXW3sBe7-7E-zIggnMnaUKUXWAj_aAuSxBspTDcGk/edit#slide=id.p

// Native Messaging port


let port = null;
const hostName = 'openadapt';


// Handle received messages from browser.js
function onReceived(response) {
  console.log(response);
}


// Message listener for content script
function messageListener(message, sender, sendResponse) {
  // console.log({ message, sender, sendResponse });
  port.postMessage(message);
}


// Event listener for tab switch or new tab open
function handleTabEvent() {
  chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
    const activeTab = tabs[0];
    const message = {
      action: 'afterTabEvent',
      tabId: activeTab.id
    };

    // Check if the tab is ready to receive messages or content script
    if (activeTab.status === 'complete') {
      chrome.tabs.sendMessage(activeTab.id, message);
    }
  });
}


port = chrome.runtime.connectNative(hostName);
port.onMessage.addListener(onReceived);
chrome.runtime.onMessage.addListener(messageListener);

// Event listeners for tab switch or new tab open
chrome.tabs.onActivated.addListener(handleTabEvent);
chrome.tabs.onCreated.addListener(handleTabEvent);
// chrome.tabs.onUpdated.addListener(handleTabEvent);
// chrome.tabs.onReplaced.addListener(handleTabEvent);
// chrome.tabs.onDetached.addListener(handleTabEvent);
