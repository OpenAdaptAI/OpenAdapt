function onReceived(response) {
    console.log(response);
}

// runtime.connectNative
var port = browser.runtime.connectNative("application_name");
port.onMessage.addListener(onReceived);
port.postMessage("hello");

// runtime.sendNativeMessage
browser.runtime.sendNativeMessage("application_name", "hello").then(onReceived);