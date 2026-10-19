import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { ControlOverlay } from "./overlay/ControlOverlay";
import { ProgramWorkbenchPreview } from "./ui/ProgramWorkbenchPreview";
import "./styles/app.css";

const surface = new URLSearchParams(window.location.search).get("surface");
const root = surface === "control-overlay"
  ? <ControlOverlay />
  : import.meta.env.DEV && surface === "program-workbench-preview"
    ? <ProgramWorkbenchPreview />
    : <App />;

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    {root}
  </React.StrictMode>,
);
