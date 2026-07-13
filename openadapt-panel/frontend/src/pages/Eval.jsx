import React, { useState } from "react";
import { api } from "../api.js";
import JobLog from "../components/JobLog.jsx";

function Metrics({ m }) {
  return (
    <div className="card">
      <h2>Results</h2>
      <div className="metric-grid">
        <div>
          <span className="k">Success rate</span>
          <span className="metric">{(m.success_rate * 100).toFixed(1)}%</span>
        </div>
        <div>
          <span className="k">Avg steps</span>
          <span className="metric">{Number(m.avg_steps).toFixed(1)}</span>
        </div>
        <div>
          <span className="k">Total tasks</span>
          <span className="metric">{m.total_tasks ?? "—"}</span>
        </div>
      </div>
    </div>
  );
}

export default function Eval() {
  const [mtasks, setMtasks] = useState(10);
  const [agent, setAgent] = useState("");
  const [ckpt, setCkpt] = useState("");
  const [server, setServer] = useState("");
  const [tasks, setTasks] = useState(10);
  const [job, setJob] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [err, setErr] = useState(null);

  function run(promise) {
    setErr(null);
    setMetrics(null);
    setJob(null);
    promise.then((d) => setJob(d.job_id)).catch((e) => setErr(e.message));
  }
  const onDone = (msg) => {
    if (msg.result) setMetrics(msg.result);
  };

  return (
    <>
      <h1>Eval</h1>
      <p className="sub">Run a mock or live benchmark evaluation</p>

      <div className="card">
        <h2>Mock eval</h2>
        <label>Tasks</label>
        <input
          type="number"
          value={mtasks}
          onChange={(e) => setMtasks(+e.target.value)}
        />
        <div className="actions">
          <button
            onClick={() =>
              run(
                api("/eval/mock", {
                  method: "POST",
                  body: JSON.stringify({ tasks: mtasks }),
                })
              )
            }
          >
            Run mock
          </button>
        </div>
      </div>

      <div className="card">
        <h2>Live / checkpoint eval</h2>
        <label>Agent</label>
        <select value={agent} onChange={(e) => setAgent(e.target.value)}>
          <option value="">— checkpoint —</option>
          <option value="api-claude">api-claude</option>
          <option value="api-openai">api-openai</option>
        </select>
        <label>Checkpoint (if no agent)</label>
        <input
          placeholder="model.pt"
          value={ckpt}
          onChange={(e) => setCkpt(e.target.value)}
        />
        <label>WAA server URL (blank = mock adapter)</label>
        <input
          placeholder="http://…"
          value={server}
          onChange={(e) => setServer(e.target.value)}
        />
        <label>Tasks</label>
        <input
          type="number"
          value={tasks}
          onChange={(e) => setTasks(+e.target.value)}
        />
        <div className="actions">
          <button
            onClick={() =>
              run(
                api("/eval/run", {
                  method: "POST",
                  body: JSON.stringify({
                    agent: agent || null,
                    checkpoint: ckpt.trim() || null,
                    server: server.trim() || null,
                    tasks,
                  }),
                })
              )
            }
          >
            Run eval
          </button>
        </div>
      </div>

      {err && <div className="card err">{err}</div>}
      {job && <JobLog jobId={job} onDone={onDone} />}
      {metrics && <Metrics m={metrics} />}
    </>
  );
}
