import React, { useEffect, useState } from "react";
import { api } from "../api.js";

function PkgRow({ p }) {
  return (
    <div className="row">
      <span className="k">
        {p.name}
        {p.version && <span className="ver">{p.version}</span>}
      </span>
      {p.installed ? (
        <span className="badge ok">installed</span>
      ) : (
        <span className="badge miss">not installed</span>
      )}
    </div>
  );
}

export default function Dashboard() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    api("/system").then(setD).catch((e) => setErr(e.message));
  }, []);

  if (err) return <div className="card err">Error: {err}</div>;
  if (!d) return <div className="card muted">Loading…</div>;

  const gpu = d.gpu.available
    ? (d.gpu.kind || "").toUpperCase() + (d.gpu.name ? " · " + d.gpu.name : "")
    : d.gpu.torch === false
      ? "PyTorch not installed"
      : "No GPU (CPU mode)";

  return (
    <>
      <h1>Dashboard</h1>
      <p className="sub">System &amp; ecosystem status</p>
      <div className="card">
        <h2>Environment</h2>
        <div className="row">
          <span className="k">Python</span>
          <span>{d.python}</span>
        </div>
        <div className="row">
          <span className="k">Platform</span>
          <span>{d.platform}</span>
        </div>
        <div className="row">
          <span className="k">GPU</span>
          <span>{gpu}</span>
        </div>
      </div>
      <div className="card">
        <h2>Core packages</h2>
        <PkgRow p={d.packages.openadapt} />
        {d.packages.core.map((p) => (
          <PkgRow key={p.name} p={p} />
        ))}
      </div>
      <div className="card">
        <h2>Optional packages</h2>
        {d.packages.optional.map((p) => (
          <PkgRow key={p.name} p={p} />
        ))}
      </div>
      <div className="card">
        <h2>API keys</h2>
        {Object.entries(d.api_keys).map(([k, v]) => (
          <div className="row" key={k}>
            <span className="k">{k}</span>
            {v ? (
              <span className="badge ok">set</span>
            ) : (
              <span className="badge miss">not set</span>
            )}
          </div>
        ))}
      </div>
    </>
  );
}
