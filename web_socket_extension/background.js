let socket;

function connectWebSocket() {
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
    setTimeout(connectWebSocket, 5000);
  };

  socket.onerror = function(error) {
    console.error("WebSocket error:", error);
    socket.close();
  };
}

connectWebSocket();

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (socket && socket.readyState === WebSocket.OPEN) {
    console.log(message);
    socket.send(JSON.stringify(message));
    sendResponse({status: "Message sent to WebSocket"});
  } else {
    sendResponse({status: "WebSocket connection not open"});
  }
});
