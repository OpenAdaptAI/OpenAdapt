/**
the background.js file listens for messages from the content script, and 
sends them to PuterBot via native messaging
*/

chrome.runtime.onMessageExternal.addListener(function(message, sender, sendResponse) {
    if (sender.id !== chrome.runtime.id) {
        return;
    }

    // Send the message to PuterBot
    chrome.runtime.sendNativeMessage("puterbot", message, function(response) {
        console.log(response);
    });
});