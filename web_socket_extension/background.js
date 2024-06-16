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

chrome.action.onClicked.addListener((tab) => {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send("ping");
    console.log("Ping message sent to WebSocket server");
  } else {
    console.error("WebSocket connection is not open");
  }
});
