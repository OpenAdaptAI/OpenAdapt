/**
 * @file background.js
 * @description Creates a new background script that listens for messages from the content script
 * and sends them to a WebSocket server.
*/

let socket;
let TIMEOUT_INTERVAL = 5000; // ms

/*
 * Function to connect to the WebSocket server.
*/
function connectWebSocket() {
  // TODO: Ideally we read "ws://localhost:8765" and `TIMEOUT_INTERVAL` from config.py, or it gets passed in somehow. 
  socket = new WebSocket("ws://localhost:8765");

  socket.onopen = function() {
    console.log("WebSocket connection established");
  };

  socket.onmessage = function(event) {
    console.log("Message from server:", event.data);
  };

  socket.onclose = function(event) {
    console.log("WebSocket connection closed", event);
    // Reconnect after 5 seconds if the connection is lost
    setTimeout(connectWebSocket, TIMEOUT_INTERVAL);
  };

  socket.onerror = function(error) {
    console.error("WebSocket error:", error);
    socket.close();
  };
}

// Create a connection to the WebSocket server
connectWebSocket();

/* Listen for messages from the content script */
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (socket && socket.readyState === WebSocket.OPEN) {
    console.log(message);
    socket.send(JSON.stringify(message));
    sendResponse({status: "Message sent to WebSocket"});
  } else {
    sendResponse({status: "WebSocket connection not open"});
  }
});
