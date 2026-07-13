import React, { useEffect, useState } from "react";
import { api, getToken } from "../api.js";
import JobLog from "../components/JobLog.jsx";

export default function Captures() {
  const [path, setPath] = useState("");
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [name, setName] = useState("");
  const [video, setVideo] = useState(true);
  const [audio, setAudio] = useState(false);
  const [recJob, setRecJob] = useState(null);
  const [viewerSrc, setViewerSrc] = useState(null);
  const [viewerErr, setViewerErr] = useState(null);

  async function load() {
    setErr(null);
    try {
      const q = path.trim() ? `?path=${encodeURIComponent(path.trim())}` : "";
      setData(await api("/captures" + q));
    } catch (e) {
      setErr(e.message);
    }
  }
  useEffect(() => {
    load();
  }, []);

  async function view(p) {
    setViewerErr(null);
    setViewerSrc(null);
    try {
      const d = await api("/captures/viewer", {
        method: "POST",
        body: JSON.stringify({ path: p }),
      });
      setViewerSrc(d.viewer_url + `&token=${encodeURIComponent(getToken())}`);
    } catch (e) {
      setViewerErr(e.message);
    }
  }

  async function startRec() {
    if (!name.trim()) return;
    setErr(null);
    try {
      const d = await api("/captures/record", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), video, audio }),
      });
      setRecJob(d.job_id);
    } catch (e) {
      setErr(e.message);
    }
  }

  return (
    <>
      <h1>Captures</h1>
      <p className="sub">List recordings, view them, start a new recording</p>

      <div className="card">
        <h2>Recordings</h2>
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
          ) : data.captures.length === 0 ? (
            <span className="muted">No captures in {data.root}</span>
          ) : (
            <table>
              <tbody>
                <tr>
                  <th>Name</th>
                  <th>Actions</th>
                  <th>Description</th>
                  <th></th>
                </tr>
                {data.captures.map((c) => (
                  <tr key={c.path}>
                    <td>{c.name}</td>
                    <td>{c.actions}</td>
                    <td className="muted">{c.description}</td>
                    <td>
                      <button className="secondary" onClick={() => view(c.path)}>
                        View
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="card">
        <h2>New recording</h2>
        <label>Name</label>
        <input
          placeholder="login-flow"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <div className="field-inline" style={{ marginTop: 12 }}>
          <label style={{ margin: 0 }}>
            <input
              type="checkbox"
              checked={video}
              onChange={(e) => setVideo(e.target.checked)}
            />{" "}
            Video
          </label>
          <label style={{ margin: 0 }}>
            <input
              type="checkbox"
              checked={audio}
              onChange={(e) => setAudio(e.target.checked)}
            />{" "}
            Audio
          </label>
        </div>
        <div className="actions">
          <button onClick={startRec}>Start recording</button>
        </div>
      </div>

      {recJob && <JobLog jobId={recJob} onDone={load} />}
      {viewerErr && <div className="card err">{viewerErr}</div>}
      {viewerSrc && (
        <div className="card">
          <h2>Viewer</h2>
          <iframe className="viewer-frame" src={viewerSrc} title="capture-viewer" />
        </div>
      )}
    </>
  );
}
