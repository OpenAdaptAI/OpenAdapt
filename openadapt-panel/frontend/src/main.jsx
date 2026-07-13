import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles.css";

// Capture the launch token, then scrub it from the address bar.
(function initToken() {
  const params = new URLSearchParams(location.search);
  const t = params.get("token");
  if (t) {
    sessionStorage.setItem("panel_token", t);
    history.replaceState({}, "", location.pathname + location.hash);
  }
})();

createRoot(document.getElementById("root")).render(<App />);
