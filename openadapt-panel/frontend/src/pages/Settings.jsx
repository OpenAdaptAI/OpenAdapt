import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Settings() {
  const [data, setData] = useState(null);
  const [values, setValues] = useState({});
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState(null);

  useEffect(() => {
    api("/settings").then(setData).catch((e) => setErr(e.message));
  }, []);

  if (err) return <div className="card err">Error: {err}</div>;
  if (!data) return <div className="card muted">Loading…</div>;

  const groups = {};
  data.fields.forEach((f) => {
    (groups[f.group] = groups[f.group] || []).push(f);
  });

  async function save() {
    setMsg("");
    try {
      const r = await api("/settings", {
        method: "PUT",
        body: JSON.stringify({ values }),
      });
      setMsg(`Saved: ${r.updated.join(", ") || "(no changes)"}`);
      setValues({});
      setData(await api("/settings"));
    } catch (e) {
      setErr(e.message);
    }
  }

  return (
    <>
      <h1>Settings</h1>
      <p className="sub">
        Written to <span className="mono">{data.env_path}</span>
      </p>
      {Object.entries(groups).map(([g, fs]) => (
        <div className="card" key={g}>
          <h2>{g}</h2>
          {fs.map((f) => (
            <div key={f.key}>
              <label>
                {f.label}{" "}
                {f.secret &&
                  (f.is_set ? (
                    <span className="badge ok">set</span>
                  ) : (
                    <span className="badge miss">not set</span>
                  ))}
              </label>
              <input
                type={f.secret ? "password" : "text"}
                placeholder={
                  f.secret ? (f.is_set ? "•••••• (unchanged)" : "not set") : ""
                }
                defaultValue={f.secret ? "" : (f.value ?? "")}
                onChange={(e) =>
                  setValues((v) => ({ ...v, [f.key]: e.target.value }))
                }
              />
            </div>
          ))}
        </div>
      ))}
      <div className="actions">
        <button onClick={save}>Save</button>
        <span className="muted">{msg}</span>
      </div>
    </>
  );
}
