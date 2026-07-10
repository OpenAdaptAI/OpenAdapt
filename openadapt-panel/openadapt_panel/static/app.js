"use strict";

// --- token handling ---------------------------------------------------------
(function initToken() {
  const params = new URLSearchParams(location.search);
  const t = params.get("token");
  if (t) {
    sessionStorage.setItem("panel_token", t);
    // Scrub the token from the address bar.
    history.replaceState({}, "", location.pathname + location.hash);
  }
})();
const TOKEN = sessionStorage.getItem("panel_token") || "";

// --- api client -------------------------------------------------------------
async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    ...opts,
    headers: {
      "X-Panel-Token": TOKEN,
      "Content-Type": "application/json",
      ...(opts.headers || {}),
    },
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    throw new Error((data && data.detail) || res.statusText);
  }
  return data;
}

const $ = (sel, root = document) => root.querySelector(sel);
const view = () => document.getElementById("view");
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );

function setError(e) {
  view().innerHTML = `<div class="card err">Error: ${esc(e.message || e)}</div>`;
}

// --- job streaming ----------------------------------------------------------
function streamJob(jobId, { onLine, onDone }) {
  const es = new EventSource(`/api/jobs/${jobId}/stream?token=${encodeURIComponent(TOKEN)}`);
  es.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.line != null && onLine) onLine(msg.line);
    if (msg.done) {
      es.close();
      if (onDone) onDone(msg);
    }
  };
  es.onerror = () => es.close();
  return es;
}

async function stopJob(jobId) {
  try { await api(`/jobs/${jobId}/stop`, { method: "POST" }); } catch (_) {}
}

// --- pages ------------------------------------------------------------------
const pages = {};

pages.dashboard = async function () {
  const d = await api("/system");
  const pkgRow = (p) =>
    `<div class="row"><span class="k">${esc(p.name)}${
      p.version ? `<span class="ver">${esc(p.version)}</span>` : ""
    }</span>${
      p.installed
        ? '<span class="badge ok">installed</span>'
        : '<span class="badge miss">not installed</span>'
    }</div>`;
  const gpu = d.gpu.available
    ? (d.gpu.kind || "").toUpperCase() + (d.gpu.name ? " · " + d.gpu.name : "")
    : d.gpu.torch === false ? "PyTorch not installed" : "No GPU (CPU mode)";
  const keys = Object.entries(d.api_keys)
    .map(([k, v]) => `<div class="row"><span class="k">${esc(k)}</span>${
      v ? '<span class="badge ok">set</span>' : '<span class="badge miss">not set</span>'
    }</div>`).join("");
  view().innerHTML = `
    <h1>Dashboard</h1><p class="sub">System & ecosystem status</p>
    <div class="card"><h2>Environment</h2>
      <div class="row"><span class="k">Python</span><span>${esc(d.python)}</span></div>
      <div class="row"><span class="k">Platform</span><span>${esc(d.platform)}</span></div>
      <div class="row"><span class="k">GPU</span><span>${esc(gpu)}</span></div>
    </div>
    <div class="card"><h2>Core packages</h2>${pkgRow(d.packages.openadapt)}${d.packages.core.map(pkgRow).join("")}</div>
    <div class="card"><h2>Optional packages</h2>${d.packages.optional.map(pkgRow).join("")}</div>
    <div class="card"><h2>API keys</h2>${keys}</div>`;
};

pages.captures = async function () {
  view().innerHTML = `
    <h1>Captures</h1><p class="sub">List recordings, view them, start a new recording</p>
    <div class="card"><h2>Recordings</h2>
      <div class="field-inline"><input id="cap-path" placeholder="Path (blank = default)" />
      <button id="cap-refresh" class="secondary">Refresh</button></div>
      <div id="cap-list" style="margin-top:14px"><span class="muted">Loading…</span></div>
    </div>
    <div class="card"><h2>New recording</h2>
      <label>Name</label><input id="rec-name" placeholder="login-flow" />
      <div class="field-inline" style="margin-top:12px">
        <label style="margin:0"><input type="checkbox" id="rec-video" checked /> Video</label>
        <label style="margin:0"><input type="checkbox" id="rec-audio" /> Audio</label>
      </div>
      <div class="actions"><button id="rec-start">Start recording</button></div>
      <div id="rec-out"></div>
    </div>
    <div id="cap-viewer"></div>`;

  async function loadList() {
    const box = $("#cap-list");
    try {
      const path = $("#cap-path").value.trim();
      const d = await api("/captures" + (path ? `?path=${encodeURIComponent(path)}` : ""));
      if (!d.captures.length) {
        box.innerHTML = `<span class="muted">No captures in ${esc(d.root)}</span>`;
        return;
      }
      box.innerHTML = `<table><tr><th>Name</th><th>Actions</th><th>Description</th><th></th></tr>${d.captures
        .map((c) => `<tr><td>${esc(c.name)}</td><td>${c.actions}</td><td class="muted">${esc(c.description)}</td>
        <td><button class="secondary view-btn" data-path="${esc(c.path)}">View</button></td></tr>`)
        .join("")}</table>`;
      box.querySelectorAll(".view-btn").forEach((b) =>
        b.addEventListener("click", () => viewCapture(b.dataset.path)));
    } catch (e) {
      box.innerHTML = `<span class="err">${esc(e.message)}</span>`;
    }
  }

  async function viewCapture(path) {
    const box = $("#cap-viewer");
    box.innerHTML = `<div class="card muted">Generating viewer…</div>`;
    try {
      const d = await api("/captures/viewer", { method: "POST", body: JSON.stringify({ path }) });
      const src = d.viewer_url + `&token=${encodeURIComponent(TOKEN)}`;
      box.innerHTML = `<div class="card"><h2>Viewer — ${esc(path)}</h2><iframe class="viewer-frame" src="${esc(src)}"></iframe></div>`;
    } catch (e) {
      box.innerHTML = `<div class="card err">${esc(e.message)}</div>`;
    }
  }

  $("#cap-refresh").addEventListener("click", loadList);
  $("#rec-start").addEventListener("click", async () => {
    const name = $("#rec-name").value.trim();
    if (!name) return;
    const out = $("#rec-out");
    try {
      const d = await api("/captures/record", {
        method: "POST",
        body: JSON.stringify({ name, video: $("#rec-video").checked, audio: $("#rec-audio").checked }),
      });
      renderJob(out, d.job_id, { onDone: loadList });
    } catch (e) {
      out.innerHTML = `<div class="err" style="margin-top:12px">${esc(e.message)}</div>`;
    }
  });
  loadList();
};

pages.train = async function () {
  view().innerHTML = `
    <h1>Train</h1><p class="sub">Start a training run and watch live progress</p>
    <div class="card"><h2>New run</h2>
      <label>Capture path</label><input id="tr-capture" placeholder="./login-flow" />
      <label>Config (optional — auto-selected by device)</label><input id="tr-config" placeholder="configs/qwen3vl_capture.yaml" />
      <label>Output directory</label><input id="tr-output" value="training_output" />
      <div class="actions"><button id="tr-start">Start training</button>
        <button id="tr-stop" class="danger" disabled>Stop</button></div>
    </div>
    <div id="tr-status"></div>
    <div id="tr-out"></div>`;

  let output = "training_output";
  let statusTimer = null;

  async function pollStatus() {
    try {
      const s = await api(`/train/status?output=${encodeURIComponent(output)}`);
      const box = $("#tr-status");
      if (!s.active) { box.innerHTML = ""; return; }
      box.innerHTML = `<div class="card"><h2>Progress</h2><div class="metric-grid">
        <div><span class="k">Status</span><span class="metric">${esc(s.status || "—")}</span></div>
        <div><span class="k">Epoch</span><span class="metric">${esc(s.epoch ?? "—")}</span></div>
        <div><span class="k">Loss</span><span class="metric">${s.loss != null ? Number(s.loss).toFixed(4) : "—"}</span></div>
      </div></div>`;
    } catch (_) {}
  }

  $("#tr-start").addEventListener("click", async () => {
    output = $("#tr-output").value.trim() || "training_output";
    const body = {
      capture: $("#tr-capture").value.trim(),
      config: $("#tr-config").value.trim() || null,
      output,
    };
    if (!body.capture) return;
    try {
      const d = await api("/train/start", { method: "POST", body: JSON.stringify(body) });
      $("#tr-stop").disabled = false;
      statusTimer = setInterval(pollStatus, 2000);
      pollStatus();
      renderJob($("#tr-out"), d.job_id, {
        onDone: () => { clearInterval(statusTimer); $("#tr-stop").disabled = true; pollStatus(); },
      });
      $("#tr-stop").onclick = () => api("/train/stop", { method: "POST", body: JSON.stringify({ output }) });
    } catch (e) {
      $("#tr-out").innerHTML = `<div class="card err">${esc(e.message)}</div>`;
    }
  });
};

pages.eval = async function () {
  view().innerHTML = `
    <h1>Eval</h1><p class="sub">Run a mock or live benchmark evaluation</p>
    <div class="card"><h2>Mock eval</h2>
      <label>Tasks</label><input id="ev-mtasks" type="number" value="10" />
      <div class="actions"><button id="ev-mock">Run mock</button></div>
    </div>
    <div class="card"><h2>Live / checkpoint eval</h2>
      <label>Agent</label>
      <select id="ev-agent"><option value="">— checkpoint —</option>
        <option value="api-claude">api-claude</option><option value="api-openai">api-openai</option></select>
      <label>Checkpoint (if no agent)</label><input id="ev-ckpt" placeholder="model.pt" />
      <label>WAA server URL (blank = mock adapter)</label><input id="ev-server" placeholder="http://…" />
      <label>Tasks</label><input id="ev-tasks" type="number" value="10" />
      <div class="actions"><button id="ev-run">Run eval</button></div>
    </div>
    <div id="ev-out"></div>`;

  const showMetrics = (box, m) => {
    box.innerHTML = `<div class="card"><h2>Results</h2><div class="metric-grid">
      <div><span class="k">Success rate</span><span class="metric">${(m.success_rate * 100).toFixed(1)}%</span></div>
      <div><span class="k">Avg steps</span><span class="metric">${Number(m.avg_steps).toFixed(1)}</span></div>
      <div><span class="k">Total tasks</span><span class="metric">${esc(m.total_tasks ?? "—")}</span></div>
    </div></div>`;
  };

  $("#ev-mock").addEventListener("click", async () => {
    try {
      const d = await api("/eval/mock", { method: "POST", body: JSON.stringify({ tasks: Number($("#ev-mtasks").value) }) });
      renderJob($("#ev-out"), d.job_id, { onDone: (m) => m.result && showMetrics($("#ev-out"), m.result) });
    } catch (e) { $("#ev-out").innerHTML = `<div class="card err">${esc(e.message)}</div>`; }
  });

  $("#ev-run").addEventListener("click", async () => {
    const body = {
      agent: $("#ev-agent").value || null,
      checkpoint: $("#ev-ckpt").value.trim() || null,
      server: $("#ev-server").value.trim() || null,
      tasks: Number($("#ev-tasks").value),
    };
    try {
      const d = await api("/eval/run", { method: "POST", body: JSON.stringify(body) });
      renderJob($("#ev-out"), d.job_id, { onDone: (m) => m.result && showMetrics($("#ev-out"), m.result) });
    } catch (e) { $("#ev-out").innerHTML = `<div class="card err">${esc(e.message)}</div>`; }
  });
};

pages.models = async function () {
  view().innerHTML = `
    <h1>Models</h1><p class="sub">Trained checkpoints on disk</p>
    <div class="card">
      <div class="field-inline"><input id="mdl-path" placeholder="Path (blank = default)" />
      <button id="mdl-refresh" class="secondary">Refresh</button></div>
      <div id="mdl-list" style="margin-top:14px"><span class="muted">Loading…</span></div>
    </div>`;
  async function load() {
    const box = $("#mdl-list");
    try {
      const path = $("#mdl-path").value.trim();
      const d = await api("/models" + (path ? `?path=${encodeURIComponent(path)}` : ""));
      box.innerHTML = d.models.length
        ? `<table><tr><th>Name</th><th>Kind</th><th>Size</th></tr>${d.models
            .map((m) => `<tr><td class="mono">${esc(m.name)}</td><td>${esc(m.kind)}</td>
            <td>${m.size_bytes != null ? (m.size_bytes / 1e6).toFixed(1) + " MB" : "—"}</td></tr>`)
            .join("")}</table>`
        : `<span class="muted">No checkpoints in ${esc(d.root)}</span>`;
    } catch (e) { box.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  }
  $("#mdl-refresh").addEventListener("click", load);
  load();
};

pages.settings = async function () {
  const d = await api("/settings");
  const groups = {};
  d.fields.forEach((f) => { (groups[f.group] = groups[f.group] || []).push(f); });
  const fieldHtml = (f) => {
    const state = f.secret
      ? (f.is_set ? '<span class="badge ok">set</span>' : '<span class="badge miss">not set</span>')
      : "";
    const ph = f.secret ? (f.is_set ? "•••••• (unchanged)" : "not set") : "";
    return `<label>${esc(f.label)} ${state}</label>
      <input data-key="${esc(f.key)}" type="${f.secret ? "password" : "text"}"
        value="${f.secret ? "" : esc(f.value ?? "")}" placeholder="${esc(ph)}" />`;
  };
  view().innerHTML = `
    <h1>Settings</h1><p class="sub">Written to <span class="mono">${esc(d.env_path)}</span></p>
    ${Object.entries(groups).map(([g, fs]) =>
      `<div class="card"><h2>${esc(g)}</h2>${fs.map(fieldHtml).join("")}</div>`).join("")}
    <div class="actions"><button id="set-save">Save</button><span id="set-msg" class="muted"></span></div>`;
  $("#set-save").addEventListener("click", async () => {
    const values = {};
    view().querySelectorAll("input[data-key]").forEach((i) => { values[i.dataset.key] = i.value; });
    try {
      const r = await api("/settings", { method: "PUT", body: JSON.stringify({ values }) });
      $("#set-msg").textContent = `Saved: ${r.updated.join(", ") || "(no changes)"}`;
    } catch (e) { $("#set-msg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  });
};

// --- shared job renderer ----------------------------------------------------
function renderJob(box, jobId, { onDone } = {}) {
  box.innerHTML = `<div class="card"><h2>Job ${esc(jobId)}
    <button class="danger" id="job-stop-${jobId}" style="float:right;padding:4px 12px">Stop</button></h2>
    <div class="log" id="job-log-${jobId}"></div>
    <div id="job-status-${jobId}" class="muted" style="margin-top:8px">running…</div></div>`;
  const log = box.querySelector(`#job-log-${jobId}`);
  box.querySelector(`#job-stop-${jobId}`).addEventListener("click", () => stopJob(jobId));
  streamJob(jobId, {
    onLine: (line) => { log.textContent += line + "\n"; log.scrollTop = log.scrollHeight; },
    onDone: (msg) => {
      const st = box.querySelector(`#job-status-${jobId}`);
      if (st) st.textContent = msg.error ? `failed: ${msg.error}` : msg.status;
      if (onDone) onDone(msg);
    },
  });
}

// --- router -----------------------------------------------------------------
function currentRoute() { return (location.hash.replace("#", "") || "dashboard"); }
async function render() {
  const route = currentRoute();
  document.querySelectorAll(".nav").forEach((a) =>
    a.classList.toggle("active", a.dataset.route === route));
  const page = pages[route] || pages.dashboard;
  try { await page(); } catch (e) { setError(e); }
}
document.querySelectorAll(".nav").forEach((a) =>
  a.addEventListener("click", () => { location.hash = a.dataset.route; }));
window.addEventListener("hashchange", render);

// --- connection indicator ---------------------------------------------------
async function checkConn() {
  const el = document.getElementById("conn");
  try {
    const h = await api("/health");
    el.innerHTML = `<span class="badge ok">connected</span> v${esc(h.version)}`;
  } catch (_) {
    el.innerHTML = `<span class="badge warn">no token / offline</span>`;
  }
}

checkConn();
render();
