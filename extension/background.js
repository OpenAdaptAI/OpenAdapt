var port = chrome.runtime.connectNative('mldsai');
port.onMessage.addListener(function(msg) {
  console.log("Received" + msg);
});
port.onDisconnect.addListener(function() {
  console.log("Disconnected");
});
port.postMessage("hello");
chrome.runtime.onMessage.addListener(function(msg) {
    port.postMessage(msg);
});
