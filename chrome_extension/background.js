/**
 * @file background.js
 * @description Creates a new background script that listens for messages from the content script
 * and sends them to a WebSocket server.
*/

let socket;
let timeOffset = 0; // Global variable to store the time offset

/* 
 * TODO: 
  * Ideally we read `WS_SERVER_PORT`, `WS_SERVER_ADDRESS` and 
  * `RECONNECT_TIMEOUT_INTERVAL` from config.py, 
  * or it gets passed in somehow. 
*/
let RECONNECT_TIMEOUT_INTERVAL = 1000; // ms
let WS_SERVER_PORT = 8765;
let WS_SERVER_ADDRESS = "localhost";
let WS_SERVER_URL = "ws://" + WS_SERVER_ADDRESS + ":" + WS_SERVER_PORT;


function socketSend(socket, message) {
  console.log({ message });
  socket.send(JSON.stringify(message));
}


/*
 * Function to connect to the WebSocket server.
*/
function connectWebSocket() {
  socket = new WebSocket(WS_SERVER_URL);

  socket.onopen = function() {
    console.log("WebSocket connection established");

    // Send initial sync message with the current timestamp
    const initialTimestamp = Date.now();
    socketSend(socket, { type: "SYNC", timestamp: initialTimestamp });
  };

  socket.onmessage = function(event) {
    console.log("Message from server:", event.data);
    const message = JSON.parse(event.data);

    if (message.type === "SYNC_RESPONSE") {
      const currentTimestamp = Date.now();
      const rtt = currentTimestamp - message.initialTimestamp;  // roundTripTime
      timeOffset = message.pythonTimestamp - (message.initialTimestamp + rtt / 2);
      console.log({ rtt, timeOffset });
    }
  };

  socket.onclose = function(event) {
    console.log("WebSocket connection closed", event);
    // Reconnect after 5 seconds if the connection is lost
    setTimeout(connectWebSocket, RECONNECT_TIMEOUT_INTERVAL);
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
    // Add the offset to the existing timestamp as a separate property
    message.timeOffset = timeOffset;
    socketSend(socket, message);
    sendResponse({ status: "Message sent to WebSocket" });
  } else {
    sendResponse({ status: "WebSocket connection not open" });
  }
});
