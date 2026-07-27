// The local task shell: pair this phone, show one decision, relay one answer.
//
// This file renders what the runner sends. It contains no decision model: the
// question text, the evidence rows, the expiry, and the action set all come
// from openadapt-flow's signed task projection and are displayed verbatim.
// ACTION_WIRE below is a name mapping between the portable task vocabulary and
// the engine's own action names -- Flow owns both, and this table mirrors its
// `_ACTION_MAP`. It never adds, removes, or reorders an allowed action.

const ACTION_WIRE = {
  verify_and_resume: { wire: "continue", label: "I fixed it — verify & continue" },
  skip: { wire: "skip", label: "Skip this step" },
  teach: { wire: "teach", label: "Teach the correction" },
  escalate: { wire: "escalate", label: "Needs more help" },
};

const DISPOSITION = {
  continue: "completed_by_operator",
  skip: "not_applicable",
  teach: "teach_requested",
  escalate: "needs_assistance",
};

const main = document.getElementById("main");
const actionBar = document.getElementById("actions");
const deviceLabel = document.getElementById("device");

// Session-scoped storage only: the credential dies with the tab rather than
// persisting on a personal phone.
const store = window.sessionStorage;

function esc(value) {
  return String(value == null ? "" : value).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}

function session() {
  return { token: store.getItem("portal_token"), csrf: store.getItem("portal_csrf") };
}

async function api(path, options = {}) {
  const { token, csrf } = session();
  const headers = Object.assign({ Accept: "application/json" }, options.headers || {});
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.method === "POST") {
    headers["Content-Type"] = "application/json";
    if (csrf) headers["X-OpenAdapt-Portal-CSRF"] = csrf;
  }
  const response = await fetch(path, {
    ...options,
    headers,
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
  });
  let body = null;
  try {
    body = await response.json();
  } catch (error) {
    body = null;
  }
  return { status: response.status, body };
}

// ------------------------------------------------------------------ pairing

async function claimPairing(secret) {
  // Remove the secret from the address bar before anything else can read it
  // from history, a screenshot, or a shared tab.
  history.replaceState(null, "", "/");
  const response = await fetch("/api/portal/pair/claim", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ secret, device_label: "Phone" }),
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
  });
  const body = await response.json().catch(() => null);
  if (response.status !== 200 || !body || !body.session_token) {
    render(
      `<section class="card"><h1>Pairing did not work</h1>
       <p>${esc((body && body.message) || "Show a new code on the computer running OpenAdapt.")}</p>
       </section>`,
    );
    return;
  }
  store.setItem("portal_token", body.session_token);
  store.setItem("portal_csrf", body.csrf_token);
  render(
    `<section class="card match">
       <h1>Type this code</h1>
       <p class="muted">Enter this code on the computer running OpenAdapt to
       finish pairing. Only this phone can see it.</p>
       <p class="code">${esc(body.confirm_code)}</p>
       <p class="muted" id="wait">Waiting for approval…</p>
     </section>`,
  );
  awaitApproval();
}

async function awaitApproval() {
  for (let attempt = 0; attempt < 150; attempt += 1) {
    const { status } = await api("/api/portal/session");
    if (status === 200) {
      route();
      return;
    }
    if (status !== 202) break;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  render(
    `<section class="card"><h1>Not approved</h1>
     <p>This phone was not approved in time. Show a new code on the computer
     running OpenAdapt.</p></section>`,
  );
}

// -------------------------------------------------------------------- tasks

function render(html) {
  main.innerHTML = html;
  actionBar.hidden = true;
  actionBar.innerHTML = "";
}

function unavailable(body) {
  render(
    `<section class="card"><h1>Not available right now</h1>
     <p>${esc((body && body.message) || "The local OpenAdapt decision service is not reachable.")}</p>
     </section>`,
  );
}

async function showList() {
  const { status, body } = await api("/api/portal/tasks");
  if (status === 401 || status === 202) return startPairing();
  if (status !== 200 || !Array.isArray(body)) return unavailable(body);
  if (body.length === 0) {
    render(`<section class="card"><h1>Nothing to decide</h1>
      <p class="muted">OpenAdapt has no open decisions on this computer.</p></section>`);
    return;
  }
  const rows = body
    .map(
      (item) => `<li><button class="row" data-run="${esc(item.id)}">
        <span class="row-title">${esc(item.headline || "OpenAdapt needs a decision")}</span>
        <span class="row-meta">${esc(item.category || "")}</span>
      </button></li>`,
    )
    .join("");
  render(`<h1 class="page-title">Decisions</h1><ul class="rows">${rows}</ul>`);
  main.querySelectorAll("[data-run]").forEach((node) => {
    node.addEventListener("click", () => showTask(node.dataset.run));
  });
}

function evidenceRows(task) {
  if (!task || !task.evidence) return "";
  const e = task.evidence;
  const pairs = [
    ["Identity signals confirmed", e.identity_confirmed_count, e.identity_required_count],
    ["Effect signals confirmed", e.effect_confirmed_count, e.effect_required_count],
    ["Effect tier observed", e.observed_effect_tier, e.minimum_effect_tier],
  ];
  const rendered = pairs
    .filter(([, got, need]) => got != null || need != null)
    .map(
      ([label, got, need]) =>
        `<div class="pair"><dt>${esc(label)}</dt><dd>${esc(got == null ? "—" : got)} of ${esc(
          need == null ? "—" : need,
        )}</dd></div>`,
    )
    .join("");
  return rendered ? `<dl class="pairs">${rendered}</dl>` : "";
}

async function showTask(runId) {
  const { status, body } = await api(`/api/portal/tasks/${encodeURIComponent(runId)}`);
  if (status === 401 || status === 202) return startPairing();
  if (status !== 200 || !body) return unavailable(body);
  const task = body.task;
  const presentation = body.presentation || {};
  const delivery = task ? task.delivery_state : null;
  const deliveryText = {
    not_delivered: "Not sent",
    delivered: "Sent",
    unknown: "May have been sent",
  }[delivery];
  const frameId = presentation.after_artifact_id || presentation.before_artifact_id;
  render(`
    <button class="back" id="back">← All decisions</button>
    <section class="card">
      ${deliveryText ? `<p class="chip">${esc(deliveryText)}</p>` : ""}
      <h1>${esc(presentation.question || "OpenAdapt needs a decision")}</h1>
      ${presentation.explanation ? `<p>${esc(presentation.explanation)}</p>` : ""}
      ${evidenceRows(task)}
      ${
        frameId
          ? `<details class="shot"><summary>View current screen</summary>
             <img id="frame" alt="Retained local screen" data-artifact="${esc(frameId)}">
             </details>`
          : ""
      }
      ${
        task && task.expires_at
          ? `<p class="muted">This decision is valid until ${esc(task.expires_at)}.</p>`
          : ""
      }
      <p class="muted">${esc(
        presentation.assurance ||
          "Your answer does not mark the run verified. OpenAdapt re-checks the live state before it can continue.",
      )}</p>
      <p class="outcome" id="outcome"></p>
    </section>
  `);
  document.getElementById("back").addEventListener("click", showList);
  const frame = document.getElementById("frame");
  if (frame) {
    frame.closest("details").addEventListener(
      "toggle",
      () => loadFrame(runId, frame),
      { once: true },
    );
  }
  renderActions(runId, body);
}

async function loadFrame(runId, image) {
  // Fetched as a blob so the image is held only in memory for this view; the
  // response is no-store and the service worker never sees a cacheable path.
  const { token } = session();
  const response = await fetch(
    `/api/portal/tasks/${encodeURIComponent(runId)}/evidence?id=${encodeURIComponent(
      image.dataset.artifact,
    )}`,
    { headers: { Authorization: `Bearer ${token}` }, cache: "no-store", credentials: "omit" },
  );
  if (!response.ok) return;
  // Revoke any previous object URL so a protected crop is not left reachable
  // for the life of the document.
  if (image.src.startsWith("blob:")) URL.revokeObjectURL(image.src);
  const url = URL.createObjectURL(await response.blob());
  image.src = url;
  image.addEventListener("load", () => URL.revokeObjectURL(url), { once: true });
}

function renderActions(runId, detail) {
  const task = detail.task;
  if (!task || !Array.isArray(task.allowed_actions)) return;
  actionBar.innerHTML = task.allowed_actions
    .filter((action) => ACTION_WIRE[action])
    .map(
      (action, index) =>
        `<button class="${index === 0 ? "primary" : ""}" data-action="${esc(action)}">${esc(
          ACTION_WIRE[action].label,
        )}</button>`,
    )
    .join("");
  actionBar.hidden = actionBar.innerHTML === "";
  actionBar.querySelectorAll("[data-action]").forEach((node) => {
    node.addEventListener("click", () => decide(runId, detail, node.dataset.action));
  });
}

async function decide(runId, detail, portableAction) {
  const outcome = document.getElementById("outcome");
  const buttons = Array.from(actionBar.querySelectorAll("button"));
  buttons.forEach((button) => (button.disabled = true));
  const wire = ACTION_WIRE[portableAction].wire;
  outcome.textContent = "Submitted. Waiting for this computer to check the live screen…";
  const payload = {
    capability_digest: detail.task.capability_digest,
    task_digest: detail.task_digest,
    task_signature: detail.task.signature,
    idempotency_key: crypto.randomUUID().replaceAll("-", ""),
    action: wire,
    disposition: DISPOSITION[wire],
  };
  const { status, body } = await api(
    `/api/portal/tasks/${encodeURIComponent(runId)}/actions/${encodeURIComponent(wire)}`,
    { method: "POST", body: JSON.stringify(payload) },
  );
  if (status === 200 && body && body.status) {
    // Never translate an accepted tap into success: show exactly what the
    // runner returned.
    outcome.textContent = body.message || body.status;
    if (body.status === "completed" || body.status === "halted") {
      actionBar.hidden = true;
      return;
    }
  } else if (status === 0 || status >= 500) {
    outcome.textContent =
      "The result is uncertain. Do not answer again — check this decision on the computer running OpenAdapt.";
    return;
  } else {
    outcome.textContent =
      (body && body.message) ||
      (body && body.detail) ||
      "That decision was refused. Reload and review the live state.";
  }
  buttons.forEach((button) => (button.disabled = false));
}

// ------------------------------------------------------------------- routing

function startPairing() {
  store.removeItem("portal_token");
  store.removeItem("portal_csrf");
  render(`<section class="card"><h1>Pair this phone</h1>
    <p>Scan the code shown in OpenAdapt on the computer running this workflow.</p>
    </section>`);
}

async function route() {
  const hash = window.location.hash || "";
  if (hash.startsWith("#c=")) {
    await claimPairing(hash.slice(3));
    return;
  }
  const { token } = session();
  if (!token) return startPairing();
  const { status, body } = await api("/api/portal/session");
  if (status === 202) return awaitApproval();
  if (status !== 200) return startPairing();
  deviceLabel.textContent = body && body.device_label ? body.device_label : "";
  await showList();
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

route();
