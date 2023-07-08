/**
 * @file background.js
 * @description This file is responsible for communicating with the native
 * messaging host and the content script.
 * @see https://docs.google.com/presentation/d/106AXW3sBe7-7E-zIggnMnaUKUXWAj_aAuSxBspTDcGk/edit#slide=id.p
 */


let port = null; // Native Messaging port
const hostName = 'openadapt';


/* 
 * Handle received messages from browser.js
*/
function onReceived(response) {
  console.log(response);
}


/* 
 * Message listener for content script
*/
function messageListener(message, sender, sendResponse) {
  // console.log({ message, sender, sendResponse });
  port.postMessage(message);
}


port = chrome.runtime.connectNative(hostName);

port.onMessage.addListener(onReceived);
chrome.runtime.onMessage.addListener(messageListener);
