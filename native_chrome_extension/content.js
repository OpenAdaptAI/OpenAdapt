// Function to extract relevant information from the document
function extractDocumentInfo() {
  const documentInfo = {
    title: document.title,
    url: window.location.href,
    html: document.documentElement.outerHTML
  };
  return documentInfo;
}

// Send the document info to the background script
function sendDocumentInfo() {
    const documentInfo = extractDocumentInfo();
    chrome.runtime.sendMessage({ type: 'document', documentInfo }, function(response) {
      console.log('Document info sent to background script');
    });
}


sendDocumentInfo();  // Send the initial document info

document.addEventListener('DOMSubtreeModified', sendDocumentInfo);  // Listen for DOM changes and send the updated document info
