import React, { useEffect, useRef, useState } from "react";
import { streamJob, stopJob } from "../api.js";

// Subscribes to a job's SSE stream, renders the live log, and reports the
// terminal message to onDone (kept in a ref so changing the callback doesn't
// re-open the stream).
export default function JobLog({ jobId, onDone }) {
  const [lines, setLines] = useState([]);
  const [status, setStatus] = useState("running…");
  const logRef = useRef(null);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;

  useEffect(() => {
    setLines([]);
    setStatus("running…");
    const es = streamJob(jobId, {
      onLine: (line) => setLines((prev) => [...prev, line]),
      onDone: (msg) => {
        setStatus(msg.error ? `failed: ${msg.error}` : msg.status);
        if (doneRef.current) doneRef.current(msg);
      },
    });
    return () => es.close();
  }, [jobId]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [lines]);

  return (
    <div className="card">
      <h2>
        Job {jobId}
        <button
          className="danger"
          style={{ float: "right", padding: "4px 12px" }}
          onClick={() => stopJob(jobId)}
        >
          Stop
        </button>
      </h2>
      <div className="log" ref={logRef}>
        {lines.join("\n")}
      </div>
      <div className="muted" style={{ marginTop: 8 }}>
        {status}
      </div>
    </div>
  );
}
