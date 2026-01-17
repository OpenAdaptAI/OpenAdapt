/**
 * OpenAdapt Capture - Background Service Worker
 *
 * Manages WebSocket connection to the Python server and relays messages
 * between content scripts and the server.
 */

// WebSocket connection state
let ws = null;
let wsConnected = false;
let reconnectInterval = null;
const WS_URL = 'ws://localhost:8765';
const RECONNECT_DELAY = 1000; // 1 second

// Current mode
let currentMode = 'idle';

// Track connected tabs
const connectedTabs = new Set();

/**
 * Initialize WebSocket connection to the Python server
 */
function connectWebSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  try {
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
      console.log('[OpenAdapt] WebSocket connected to', WS_URL);
      wsConnected = true;

      // Clear reconnect interval if set
      if (reconnectInterval) {
        clearInterval(reconnectInterval);
        reconnectInterval = null;
      }

      // Update extension icon to show connected state
      updateIcon(true);
    };

    ws.onclose = () => {
      console.log('[OpenAdapt] WebSocket disconnected');
      wsConnected = false;
      ws = null;

      // Update extension icon to show disconnected state
      updateIcon(false);

      // Start reconnection attempts
      scheduleReconnect();
    };

    ws.onerror = (error) => {
      console.error('[OpenAdapt] WebSocket error:', error);
    };

    ws.onmessage = (event) => {
      handleServerMessage(event.data);
    };

  } catch (error) {
    console.error('[OpenAdapt] Failed to create WebSocket:', error);
    scheduleReconnect();
  }
}

/**
 * Schedule WebSocket reconnection
 */
function scheduleReconnect() {
  if (reconnectInterval) {
    return; // Already scheduled
  }

  reconnectInterval = setInterval(() => {
    console.log('[OpenAdapt] Attempting to reconnect...');
    connectWebSocket();
  }, RECONNECT_DELAY);
}

/**
 * Send a message to the Python server via WebSocket
 */
function sendToServer(message) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn('[OpenAdapt] WebSocket not connected, message dropped');
    return false;
  }

  try {
    ws.send(JSON.stringify(message));
    return true;
  } catch (error) {
    console.error('[OpenAdapt] Failed to send message:', error);
    return false;
  }
}

/**
 * Handle messages from the Python server
 */
function handleServerMessage(data) {
  let message;
  try {
    message = JSON.parse(data);
  } catch (error) {
    console.error('[OpenAdapt] Invalid JSON from server:', data);
    return;
  }

  const messageType = message.type;

  switch (messageType) {
    case 'SET_MODE':
      handleSetMode(message);
      break;

    case 'PING':
      handlePing(message);
      break;

    case 'EXECUTE_ACTION':
      handleExecuteAction(message);
      break;

    default:
      console.warn('[OpenAdapt] Unknown message type:', messageType);
  }
}

/**
 * Handle SET_MODE message from server
 */
function handleSetMode(message) {
  const newMode = message.payload?.mode || 'idle';
  currentMode = newMode;
  console.log('[OpenAdapt] Mode set to:', currentMode);

  // Broadcast mode change to all tabs
  broadcastToTabs({
    type: 'SET_MODE',
    payload: { mode: currentMode }
  });
}

/**
 * Handle PING message from server
 */
function handlePing(message) {
  sendToServer({
    type: 'PONG',
    timestamp: Date.now(),
    payload: {}
  });
}

/**
 * Handle EXECUTE_ACTION message from server (replay mode)
 */
function handleExecuteAction(message) {
  const action = message.payload?.action;
  if (!action) {
    console.error('[OpenAdapt] EXECUTE_ACTION missing action payload');
    return;
  }

  // Send action to the active tab for execution
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs.length > 0) {
      chrome.tabs.sendMessage(tabs[0].id, {
        type: 'EXECUTE_ACTION',
        payload: { action: action }
      });
    }
  });
}

/**
 * Broadcast a message to all content scripts
 */
function broadcastToTabs(message) {
  chrome.tabs.query({}, (tabs) => {
    for (const tab of tabs) {
      try {
        chrome.tabs.sendMessage(tab.id, message).catch(() => {
          // Tab may not have content script, ignore
        });
      } catch (error) {
        // Ignore errors for tabs without content scripts
      }
    }
  });
}

/**
 * Update the extension icon based on connection state
 */
function updateIcon(connected) {
  const iconPath = connected ? {
    16: 'icons/icon16.png',
    48: 'icons/icon48.png',
    128: 'icons/icon128.png'
  } : {
    16: 'icons/icon16.png',
    48: 'icons/icon48.png',
    128: 'icons/icon128.png'
  };

  chrome.action.setIcon({ path: iconPath });

  // Update badge to show connection status
  if (connected) {
    chrome.action.setBadgeText({ text: '' });
  } else {
    chrome.action.setBadgeText({ text: '!' });
    chrome.action.setBadgeBackgroundColor({ color: '#FF0000' });
  }
}

// ============ Message Handling from Content Scripts ============

/**
 * Handle messages from content scripts
 */
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const tabId = sender.tab?.id;

  switch (message.type) {
    case 'DOM_EVENT':
      // Add tab ID and relay to server
      message.tabId = tabId;
      sendToServer(message);
      sendResponse({ success: true });
      break;

    case 'DOM_SNAPSHOT':
      // Add tab ID and relay to server
      message.tabId = tabId;
      sendToServer(message);
      sendResponse({ success: true });
      break;

    case 'ERROR':
      // Add tab ID and relay to server
      message.tabId = tabId;
      sendToServer(message);
      sendResponse({ success: true });
      break;

    case 'GET_STATUS':
      // Return current connection and mode status
      sendResponse({
        connected: wsConnected,
        mode: currentMode
      });
      break;

    default:
      console.warn('[OpenAdapt] Unknown message type from content script:', message.type);
      sendResponse({ success: false, error: 'Unknown message type' });
  }

  return true; // Keep message channel open for async response
});

// ============ Tab Management ============

/**
 * Handle tab updates to inject content scripts if needed
 */
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url && !tab.url.startsWith('chrome://')) {
    // Send current mode to newly loaded tab
    chrome.tabs.sendMessage(tabId, {
      type: 'SET_MODE',
      payload: { mode: currentMode }
    }).catch(() => {
      // Content script not yet loaded, ignore
    });
  }
});

// ============ Extension Lifecycle ============

/**
 * Handle extension installation
 */
chrome.runtime.onInstalled.addListener(() => {
  console.log('[OpenAdapt] Extension installed');
  connectWebSocket();
});

/**
 * Handle extension startup
 */
chrome.runtime.onStartup.addListener(() => {
  console.log('[OpenAdapt] Extension started');
  connectWebSocket();
});

// ============ Keep Service Worker Alive ============

/**
 * Service workers can be terminated when idle.
 * Use periodic alarms to keep the WebSocket connection alive.
 */
chrome.alarms.create('keepAlive', { periodInMinutes: 0.5 });

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'keepAlive') {
    // Check WebSocket connection
    if (!wsConnected) {
      connectWebSocket();
    }
  }
});

// ============ Initialization ============

// Connect to WebSocket server on script load
connectWebSocket();

console.log('[OpenAdapt] Background service worker loaded');
