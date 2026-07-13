import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Models() {
  const [path, setPath] = useState("");
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  async function load() {
    setErr(null);
    try {
      const q = path.trim() ? `?path=${encodeURIComponent(path.trim())}` : "";
      setData(await api("/models" + q));
    } catch (e) {
      setErr(e.message);
    }
  }
  useEffect(() => {
    load();
  }, []);

  return (
    <>
      <h1>Models</h1>
      <p className="sub">Trained checkpoints on disk</p>
      <div className="card">
        <div className="field-inline">
          <input
            placeholder="Path (blank = default)"
            value={path}
            onChange={(e) => setPath(e.target.value)}
          />
          <button className="secondary" onClick={load}>
            Refresh
          </button>
        </div>
        <div style={{ marginTop: 14 }}>
          {err ? (
            <span className="err">{err}</span>
          ) : !data ? (
            <span className="muted">Loading…</span>
          ) : data.models.length === 0 ? (
            <span className="muted">No checkpoints in {data.root}</span>
          ) : (
            <table>
              <tbody>
                <tr>
                  <th>Name</th>
                  <th>Kind</th>
                  <th>Size</th>
                </tr>
                {data.models.map((m) => (
                  <tr key={m.path}>
                    <td className="mono">{m.name}</td>
                    <td>{m.kind}</td>
                    <td>
                      {m.size_bytes != null
                        ? (m.size_bytes / 1e6).toFixed(1) + " MB"
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </>
  );
}
