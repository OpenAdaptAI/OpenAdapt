/**
 * @file background.js
 * @description Background script that maintains the current mode and communicates with content scripts.
 */

let socket;
let currentMode = null; // Maintain the current mode here
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
  };

  socket.onmessage = function(event) {
    console.log("Message from server:", event.data);
    const message = JSON.parse(event.data);

    // Handle mode messages
    if (message.type === 'SET_MODE') {
      currentMode = message.mode; // Update the current mode
      console.log(`Mode set to: ${currentMode}`);

      // Send the mode to all active tabs
      chrome.tabs.query(
        {
          active: true,
        },
        function(tabs) {
          tabs.forEach(function(tab) {
            chrome.tabs.sendMessage(tab.id, message, function(response) {
              if (chrome.runtime.lastError) {
                console.error("Error sending message to content script in tab " + tab.id, chrome.runtime.lastError.message);
              } else {
                console.log("Message sent to content script in tab " + tab.id, response);
              }
            });
          });
        }
      );
    }
  };

  socket.onclose = function(event) {
    console.log("WebSocket connection closed", event);
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
  const tabId = sender.tab.id;
  message.tabId = tabId;
  if (socket && socket.readyState === WebSocket.OPEN) {
    socketSend(socket, message);
    sendResponse({ status: "Message sent to WebSocket" });
  } else {
    sendResponse({ status: "WebSocket connection not open" });
  }
});

/* Listen for tab activation */
chrome.tabs.onActivated.addListener((activeInfo) => {
  // Send current mode to the newly active tab if it's not null
  if (currentMode) {
    const message = { type: 'SET_MODE', mode: currentMode };
    chrome.tabs.sendMessage(activeInfo.tabId, message, function(response) {
      if (chrome.runtime.lastError) {
        console.error("Error sending message to content script in tab " + activeInfo.tabId, chrome.runtime.lastError.message);
      } else {
        console.log("Message sent to content script in tab " + activeInfo.tabId, response);
      }
    });
  }
});

/* Listen for tab updates to handle new pages or reloading */
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && currentMode) {
    const message = { type: 'SET_MODE', mode: currentMode };
    chrome.tabs.sendMessage(tabId, message, function(response) {
      if (chrome.runtime.lastError) {
        console.error("Error sending message to content script in tab " + tabId, chrome.runtime.lastError.message);
      } else {
        console.log("Message sent to content script in tab " + tabId, response);
      }
    });
  }
});
