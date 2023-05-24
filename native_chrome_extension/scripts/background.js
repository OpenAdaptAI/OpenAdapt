// service-worker.js

// Establish a connection with the native app
var port = chrome.runtime.connectNative('openadapt');

port.onMessage.addListener(function (msg) {
  console.log('Received: ' + msg);
});

port.onDisconnect.addListener(function () {
  console.log('Disconnected');
});

// On a click on the action, send a message to the native app
chrome.action.onClicked.addListener(function () {
  console.log('Sending: hi');
  port.postMessage('hi');
});
