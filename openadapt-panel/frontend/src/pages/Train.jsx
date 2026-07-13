import React, { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import JobLog from "../components/JobLog.jsx";

export default function Train() {
  const [capture, setCapture] = useState("");
  const [config, setConfig] = useState("");
  const [output, setOutput] = useState("training_output");
  const [job, setJob] = useState(null);
  const [status, setStatus] = useState(null);
  const [err, setErr] = useState(null);
  const timer = useRef(null);
  const outputRef = useRef(output);

  async function poll() {
    try {
      const s = await api(`/train/status?output=${encodeURIComponent(outputRef.current)}`);
      setStatus(s.active ? s : null);
    } catch {
      /* ignore transient status read errors */
    }
  }

  async function start() {
    if (!capture.trim()) return;
    setErr(null);
    outputRef.current = output.trim() || "training_output";
    try {
      const d = await api("/train/start", {
        method: "POST",
        body: JSON.stringify({
          capture: capture.trim(),
          config: config.trim() || null,
          output: outputRef.current,
        }),
      });
      setJob(d.job_id);
      poll();
      timer.current = setInterval(poll, 2000);
    } catch (e) {
      setErr(e.message);
    }
  }

  function onDone() {
    clearInterval(timer.current);
    poll();
  }

  useEffect(() => () => clearInterval(timer.current), []);

  return (
    <>
      <h1>Train</h1>
      <p className="sub">Start a training run and watch live progress</p>

      <div className="card">
        <h2>New run</h2>
        <label>Capture path</label>
        <input
          placeholder="./login-flow"
          value={capture}
          onChange={(e) => setCapture(e.target.value)}
        />
        <label>Config (optional — auto-selected by device)</label>
        <input
          placeholder="configs/qwen3vl_capture.yaml"
          value={config}
          onChange={(e) => setConfig(e.target.value)}
        />
        <label>Output directory</label>
        <input value={output} onChange={(e) => setOutput(e.target.value)} />
        <div className="actions">
          <button onClick={start}>Start training</button>
          <button
            className="danger"
            disabled={!job}
            onClick={() =>
              api("/train/stop", {
                method: "POST",
                body: JSON.stringify({ output: outputRef.current }),
              })
            }
          >
            Stop
          </button>
        </div>
      </div>

      {err && <div className="card err">{err}</div>}
      {status && (
        <div className="card">
          <h2>Progress</h2>
          <div className="metric-grid">
            <div>
              <span className="k">Status</span>
              <span className="metric">{status.status ?? "—"}</span>
            </div>
            <div>
              <span className="k">Epoch</span>
              <span className="metric">{status.epoch ?? "—"}</span>
            </div>
            <div>
              <span className="k">Loss</span>
              <span className="metric">
                {status.loss != null ? Number(status.loss).toFixed(4) : "—"}
              </span>
            </div>
          </div>
        </div>
      )}
      {job && <JobLog jobId={job} onDone={onDone} />}
    </>
  );
}
