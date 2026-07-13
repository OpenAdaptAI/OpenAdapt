import React, { useEffect, useState } from "react";
import { api } from "./api.js";
import Dashboard from "./pages/Dashboard.jsx";
import Captures from "./pages/Captures.jsx";
import Train from "./pages/Train.jsx";
import Eval from "./pages/Eval.jsx";
import Models from "./pages/Models.jsx";
import Settings from "./pages/Settings.jsx";

const PAGES = [
  ["dashboard", "Dashboard", Dashboard],
  ["captures", "Captures", Captures],
  ["train", "Train", Train],
  ["eval", "Eval", Eval],
  ["models", "Models", Models],
  ["settings", "Settings", Settings],
];

export default function App() {
  const [route, setRoute] = useState(location.hash.replace("#", "") || "dashboard");
  const [conn, setConn] = useState(null);

  useEffect(() => {
    api("/health")
      .then((h) => setConn({ ok: true, version: h.version }))
      .catch(() => setConn({ ok: false }));
  }, []);

  useEffect(() => {
    const onHash = () => setRoute(location.hash.replace("#", "") || "dashboard");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const Page = (PAGES.find((p) => p[0] === route) || PAGES[0])[2];

  return (
    <div id="layout">
      <nav id="sidebar">
        <div className="brand">OpenAdapt</div>
        {PAGES.map(([key, label]) => (
          <button
            key={key}
            className={"nav" + (key === route ? " active" : "")}
            onClick={() => {
              location.hash = key;
            }}
          >
            {label}
          </button>
        ))}
        <div className="nav-footer">
          {conn == null ? (
            "…"
          ) : conn.ok ? (
            <>
              <span className="badge ok">connected</span> v{conn.version}
            </>
          ) : (
            <span className="badge warn">no token / offline</span>
          )}
        </div>
      </nav>
      <main>
        <Page />
      </main>
    </div>
  );
}
