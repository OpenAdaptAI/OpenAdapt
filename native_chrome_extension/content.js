/**
The content.js file listens for changes to the DOM, and 
sends a message to the background script when a change is detected.
*/

// Connect to the background script
const port = chrome.runtime.connect();

// Listen for changes to the DOM
// Ref.: https://stackoverflow.com/questions/8882502/how-to-track-dom-change-in-chrome-extension solution

const observer = new MutationObserver(mutations => {
  mutations.forEach(mutation => { // change occurs in DOM
    try { 
      port.postMessage(mutation); // then, Send a message to the background script: background.js
    } catch (e) {
      console.error(e);
      port.disconnect();
      const newPort = chrome.runtime.connect();
      newPort.onMessage.addListener(msg => console.log(msg));
    }
  });
});

observer.observe(document, { childList: true, subtree: true });
