/**
the background.js file listens for messages from the content script, and 
sends them to OpenAdapt via native messaging
*/

// Connect to the native messaging host
let port = chrome.runtime.connectNative("com.openadapt.domlistener");

// Receive messages from the native messaging host
port.onMessage.addListener(onReceived);

// Send initial message to the native messaging host
port.postMessage("hello");

// Handle received messages
function onReceived(response) {
    console.log(response);
}

// Listen for messages from the content script
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    // Forward the message to the native messaging host
    port.postMessage(message);
});

// Listen for DOM changes in the active tab
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (changeInfo.status === "complete") {
        // Inject content script to the updated tab
        chrome.tabs.executeScript(tabId, { file: "content.js" });
    }
});