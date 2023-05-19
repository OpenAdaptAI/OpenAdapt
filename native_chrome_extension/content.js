/**
The content.js file listens for changes to the DOM, and 
sends a message to the background script when a change is detected.
*/

// DOM mutation observer
let observer = new MutationObserver(mutations => {
    for (let mutation of mutations) {
        // Send the mutation to the background script
        chrome.runtime.sendMessage(mutation);
    }
});

// Start observing DOM mutations
observer.observe(document, { childList: true, subtree: true });