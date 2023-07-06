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
    if (mutationsList.length === 0) {
      return;
    }
    if (!logged) {
      console.log({ mutationsList })
      // logged = true;
      // debugger;
    }
    getElementPositions();
    sendMessageToBackgroundScript({
      action: 'logDOMChange',
      documentBody: document.body.outerHTML,
      documentHead: document.head.outerHTML
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
let logged = true;
let ignoreAttributes = new Set()


function getElementPositions() {
  // iterate over all elements in the page
  const elements = document.getElementsByTagName("*");

  for (const element of elements) {
    const rect = element.getBoundingClientRect();
    // console.log({ rect });
    const attrs = ['top', 'right', 'bottom', 'left', 'width', 'height'];
    for (const attr of attrs) {
      element.setAttribute(`data-${attr}`, rect[attr]);
      ignoreAttributes.add(`data-${attr}`)
    }
  }

  // later, improve performance:
  // - ignore elements that are not in the viewport
  // - on update, use mutations to only update elements that have changed
}

