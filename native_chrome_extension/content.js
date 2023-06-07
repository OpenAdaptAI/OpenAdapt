chrome.runtime.sendMessage("Hello from content.js!");


// Function to send the document to the background script
function sendDocument(doc) {
  chrome.runtime.sendMessage({ type: 'document', document: doc });
}


// Listen for DOM changes and send the document to the background script
document.addEventListener('DOMContentLoaded', () => {

    // Send the initial document
    sendDocument(document);
  
    // Listen for DOM changes
    document.addEventListener('DOMSubtreeModified', () => sendDocument(document));
});
