// System Diagram: https://docs.google.com/presentation/d/106AXW3sBe7-7E-zIggnMnaUKUXWAj_aAuSxBspTDcGk/edit#slide=id.p


/*
 * Function to send a message to the background script
*/
function sendMessageToBackgroundScript(message) {
  chrome.runtime.sendMessage(message);
}


/*
 * Function to detect DOM changes
*/
function detectDOMChanges() {
  // Create a mutation observer
  const observer = new MutationObserver(function(mutationsList) {
    // Send a message to the background script when a DOM change is detected
    console.log({ mutationsList })
    sendMessageToBackgroundScript({
      action: 'logDOMChange',
      documentObject: document.body.innerHTML,
    });
  });

  // Start observing DOM changes
  observer.observe(document, {
    subtree: true,
    childList: true,
    attributes: true,
    characterData: true,
  });
}

// Call the function to start detecting DOM changes
detectDOMChanges();
