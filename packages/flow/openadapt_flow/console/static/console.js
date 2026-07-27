"use strict";

const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(
  /[&<>"']/g,
  (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character],
);
const enc = encodeURIComponent;
const TOKEN_KEY = "openadapt.console.token";
let AUTH_TOKEN = "";
let CSRF_TOKEN = "";
let HEALTH = {read_only: true};
let currentActions = {list: [], postBase: ""};
let currentSkillActions = [];
let pendingExec = null;
let artifactObjectUrls = [];
let currentJsonViewer = null;

function consumeBootstrapToken() {
  const rawFragment = window.location.hash.slice(1);
  const params = new URLSearchParams(rawFragment);
  if (params.has("token")) {
    const token = params.get("token") || "";
    if (token) {
      window.sessionStorage.setItem(TOKEN_KEY, token);
    } else {
      window.sessionStorage.removeItem(TOKEN_KEY);
    }
    window.history.replaceState(
      null,
      "",
      window.location.pathname + window.location.search,
    );
  }
  AUTH_TOKEN = window.sessionStorage.getItem(TOKEN_KEY) || "";
}

function requestHeaders(initial) {
  const headers = new Headers(initial || {});
  if (AUTH_TOKEN) {
    headers.set("Authorization", `Bearer ${AUTH_TOKEN}`);
  }
  return headers;
}

async function api(path, opts = {}) {
  const method = String(opts.method || "GET").toUpperCase();
  const headers = requestHeaders(opts.headers);
  if (method !== "GET" && method !== "HEAD" && CSRF_TOKEN) {
    headers.set("X-OpenAdapt-CSRF", CSRF_TOKEN);
  }
  const response = await fetch(path, {...opts, method, headers});
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw {status: response.status, body};
  }
  return body;
}

const fmtTime = (time) => time
  ? esc(String(time).replace("T", " ").slice(0, 19))
  : "—";
const pct = (numerator, denominator) => denominator
  ? Math.round(100 * Number(numerator) / Number(denominator))
  : null;
const safeNumber = (value, fallback = "—") => {
  const number = Number(value);
  return Number.isFinite(number) ? esc(number) : fallback;
};
const humanBytes = (value) => {
  const bytes = Number(value);
  if (!Number.isFinite(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
};

function artifactLinks(artifacts, scope, ownerId) {
  if (!artifacts.length) return '<p class="muted">no viewable local JSON artifacts</p>';
  return `<div class="artifact-links">${artifacts.map((artifact) => `
    <a class="artifact-link" href="#/${scope}/${enc(ownerId)}/artifacts/${enc(artifact.id)}">
      <span class="mono">${esc(artifact.label)}</span>
      <span class="muted">${esc(artifact.format.toUpperCase())} · ${esc(humanBytes(artifact.size_bytes))}</span>
    </a>`).join("")}</div>`;
}

function statusChip(summary) {
  if (summary.load_error) {
    return `<span class="chip err" title="${esc(summary.load_error)}">unreadable</span>`;
  }
  if (summary.certified) {
    const policy = summary.policy_name ? ` · ${esc(summary.policy_name)}` : "";
    return `<span class="chip ok">certified${policy}</span>`;
  }
  if (summary.certification_status === "failed") {
    return '<span class="chip err">cert failed</span>';
  }
  if (summary.certification_status === "expired") {
    return '<span class="chip warn">cert expired</span>';
  }
  return '<span class="chip">uncertified</span>';
}

function runPresentation(run) {
  if (run.execution_outcome === "VERIFIED") return { cssClass: "ok", label: "verified" };
  if (run.execution_outcome === "COMPLETED_UNVERIFIED") {
    return { cssClass: "warn", label: "completed unverified" };
  }
  if (run.execution_outcome === "HALTED") return { cssClass: "err", label: "halted" };
  if (run.execution_outcome === "FAILED") return { cssClass: "err", label: "failed" };
  if (run.success) return { cssClass: "ok", label: "success" };
  if (run.paused) return { cssClass: "warn", label: "paused" };
  return { cssClass: "err", label: run.halted ? "halted" : "failed" };
}

function runChip(run) {
  if (!run) return '<span class="muted">never run</span>';
  const { cssClass, label } = runPresentation(run);
  return `<a href="#/runs/${enc(run.run_id)}"><span class="chip ${cssClass}">${label}</span></a> <span class="muted">${fmtTime(run.started_at)}</span>`;
}

async function viewWorkflows() {
  const workflows = await api("/api/workflows");
  const rows = workflows.map((workflow) => `
    <tr class="rowlink" data-route="#/workflows/${enc(workflow.id)}">
      <td><strong>Workflow ${esc(String(workflow.id).slice(0, 8))}</strong><br><span class="mono muted">${esc(workflow.id)}</span></td>
      <td class="mono">${esc(workflow.compiler_version || "—")}<br>
          <span class="muted">${workflow.content_digest ? `${esc(String(workflow.content_digest).slice(0, 12))}…` : ""}</span></td>
      <td>${statusChip(workflow)}</td>
      <td>${safeNumber(workflow.n_steps)}</td>
      <td>${workflow.encrypted ? '<span class="chip info">encrypted</span>' : ""}
          ${workflow.contains_phi ? '<span class="chip warn">PHI</span>' : ""}</td>
      <td>${runChip(workflow.last_run)}</td>
    </tr>`).join("");
  return `<h2>Workflow bundles</h2>
    <table><tr><th>Bundle</th><th>Compiler / digest</th><th>Certification</th>
    <th>Steps</th><th>Flags</th><th>Last run</th></tr>${rows ||
    '<tr><td colspan="6" class="muted">no bundles found under the bundles root</td></tr>'}</table>`;
}

function covCard(label, numerator, denominator, unit) {
  const percent = pct(numerator, denominator);
  const value = percent === null || !Number.isFinite(percent) ? 0 : percent;
  const fraction = denominator
    ? `${safeNumber(numerator, "0")} / ${safeNumber(denominator, "0")} ${esc(unit)}`
    : `no ${esc(unit)}`;
  return `<div class="card"><div class="lbl">${esc(label)}</div>
    <div class="big">${percent === null ? "n/a" : `${safeNumber(percent, "0")}%`}</div>
    <div class="lbl">${fraction}</div>
    <progress class="bar" max="100" value="${safeNumber(value, "0")}">${safeNumber(value, "0")}%</progress></div>`;
}

async function viewWorkflowDetail(id, policy) {
  const detail = await api(`/api/workflows/${id}${policy ? `?policy=${enc(policy)}` : ""}`);
  const summary = detail.summary;
  const artifacts = await api(`/api/artifacts/json/workflows/${id}`);
  if (detail.load_error) {
    return `<h2>${esc(summary.id)}</h2><p class="chip err">unreadable</p>
      <pre class="cmd">${esc(detail.load_error)}</pre>
      <p class="muted">Encrypted bundles need their key at load; the console never handles keys.</p>`;
  }
  const identity = detail.identity_coverage;
  const effects = detail.effect_coverage;
  const certification = detail.certification;
  const others = (await api("/api/workflows")).filter(
    (workflow) => workflow.id !== summary.id && !workflow.load_error,
  );

  const stepRows = detail.steps.map((step) => {
    let identityBadge = "";
    if (step.identity_applicable) {
      identityBadge = step.identity_armed
        ? '<span class="chip ok">armed</span>'
        : `<span class="chip err" title="${esc(step.identity_unarmed_reason || "")}">UNARMED</span>`;
    }
    const effectBadge = step.n_effects
      ? `<span class="chip ok">${safeNumber(step.n_effects, "0")} contract${Number(step.n_effects) > 1 ? "s" : ""}</span>` +
        (step.effects.some((effect) => effect.needs_operator_confirmation)
          ? ' <span class="chip warn">needs confirmation</span>' : "")
      : (step.risk === "irreversible"
        ? '<span class="chip err">none</span>'
        : '<span class="muted">—</span>');
    const actionDetail = `${esc(step.action)}${step.parameterized ? " · parameterized" : ""}${step.secret ? " · secret" : ""}`;
    return `<tr>
      <td class="mono">${esc(step.id)}</td>
      <td>${esc(step.intent)}<br><span class="muted mono">${actionDetail}</span></td>
      <td>${step.risk === "irreversible" ? '<span class="chip warn">irreversible</span>' : '<span class="muted">reversible</span>'}</td>
      <td>${identityBadge}</td>
      <td>${effectBadge}</td>
      <td>${safeNumber(step.n_postconditions, "0")}</td>
      <td class="mono">${safeNumber(step.confidence)}</td>
    </tr>`;
  }).join("");

  const lintRows = detail.lint.findings.map((finding) => {
    const severityClass = finding.severity === "error"
      ? "err"
      : (finding.severity === "warn" ? "warn" : "info");
    return `<tr><td><span class="chip ${severityClass}">${esc(finding.severity)}</span></td>
      <td class="mono">${esc(finding.code)}</td></tr>`;
  }).join("");

  let certificationHtml = `<p>Sealed: ${statusChip(summary)} ${certification.sealed.certified_at ? `<span class="muted">at ${fmtTime(certification.sealed.certified_at)}</span>` : ""}
    ${certification.sealed.expires_at ? `<span class="muted">expires ${fmtTime(certification.sealed.expires_at)}</span>` : ""}</p>`;
  if (certification.live) {
    certificationHtml += `<p>Live evaluation vs <span class="mono">${esc(certification.live.policy_name)}</span>:
      <span class="chip ${certification.live.passed ? "ok" : "err"}">${certification.live.passed ? "PASS" : "FAIL"}</span></p>`;
    if (certification.live.violations.length) {
      certificationHtml += `<table><tr><th>Rule</th></tr>${
        certification.live.violations.map((violation) =>
          `<tr><td class="mono">${esc(violation.rule)}</td></tr>`).join("")
      }</table>`;
    }
  } else if (certification.live_error) {
    certificationHtml += `<p class="muted">live evaluation unavailable: ${esc(certification.live_error)}</p>`;
  } else {
    certificationHtml += `<p class="muted">No certifying policy recorded. Evaluate live:</p>
      <p>${certification.available_policies.map((availablePolicy) =>
        `<button data-route="#/workflows/${enc(summary.id)}?policy=${enc(availablePolicy)}">vs ${esc(availablePolicy)}</button>`).join(" ")}</p>`;
  }

  const diffSelector = others.length ? `
    <p>Compare against:
      <select id="diffsel">${others.map((other) =>
        `<option value="${enc(other.id)}">Workflow ${esc(String(other.id).slice(0, 8))}</option>`).join("")}</select>
      <button data-diff-source="${enc(summary.id)}">Diff</button>
    </p>` : '<p class="muted">no other bundle to diff against</p>';

  const actions = await api(`/api/workflows/${id}/actions`);
  return `<h2>Workflow ${esc(String(summary.id).slice(0, 8))} <span class="muted mono">(${esc(summary.id)})</span></h2>
    <div class="cards">
      ${covCard("Identity-armed steps", identity.armed, identity.applicable, "identity-applicable steps")}
      ${covCard("Effect contracts on consequential actions",
                effects.consequential_with_contract, effects.consequential, "irreversible steps")}
      <div class="card"><div class="lbl">Compiler</div><div class="big">${esc(summary.compiler_version || "—")}</div>
        <div class="lbl mono">${summary.content_digest ? `${esc(String(summary.content_digest).slice(0, 16))}…` : ""}</div></div>
      <div class="card"><div class="lbl">Params</div>
        <div class="big">${safeNumber(detail.parameter_count, "0")}</div>
        <div class="lbl">${safeNumber(detail.secret_parameter_count, "0")} secret</div></div>
    </div>
    ${identity.unarmed.length ? `<details><summary>${safeNumber(identity.unarmed.length, "0")} unarmed step(s)</summary>
      <table><tr><th>Step</th><th>Intent</th><th>Reason</th></tr>${identity.unarmed.map((item) =>
      `<tr><td class="mono">${esc(item.step_id)}</td><td>${esc(item.intent)}</td><td>${esc(item.reason)}</td></tr>`).join("")}</table></details>` : ""}
    <h2>Certification</h2>${certificationHtml}
    <h2>Compiled steps (${safeNumber(detail.steps.length, "0")}${detail.program_mode ? ", program mode" : ""})</h2>
    <table><tr><th>Id</th><th>Intent</th><th>Risk</th><th>Identity</th>
      <th>Effect contract</th><th>Postconds</th><th>Conf.</th></tr>${stepRows}</table>
    <h2>Lint (${safeNumber(detail.lint.findings.length, "0")} finding(s))</h2>
    ${lintRows ? `<table><tr><th>Severity</th><th>Code</th></tr>${lintRows}</table>`
               : '<p class="muted">no coverage gaps found</p>'}
    <h2>Versions & diffs</h2>${diffSelector}
    <h2>Local artifacts</h2>
    <p class="muted">Exact protected bundle JSON. It stays on this computer and is shown only in this authenticated console.</p>
    ${artifactLinks(artifacts, "workflows", summary.id)}
    <h2>Actions</h2>${renderActions(actions, `/api/workflows/${id}/actions/`)}`;
}

async function viewDiff(firstId, secondId) {
  const detail = await api(`/api/workflows/${firstId}/diff/${secondId}`);
  if (detail.error) return `<h2>Diff</h2><p class="chip err">${esc(detail.error)}</p>`;
  const metadata = (summary) => `<div class="card"><div class="lbl">${esc(summary.id)}</div>
    <div class="big">Workflow ${esc(String(summary.id).slice(0, 8))}</div>
    <div class="lbl mono">${esc(summary.compiler_version || "")} · ${summary.content_digest ? `${esc(String(summary.content_digest).slice(0, 12))}…` : ""}</div>
    <div class="lbl">${fmtTime(summary.created_at)} · ${summary.certified ? "certified" : "uncertified"}</div></div>`;
  return `<h2>Diff <span class="mono muted">${esc(firstId)} → ${esc(secondId)}</span></h2>
    <div class="cards">${metadata(detail.a)}${metadata(detail.b)}</div>
    ${detail.identical ? '<p class="chip ok">bundles are semantically identical (step-wise)</p>' : ""}
    ${detail.steps_added_count ? `<p>${safeNumber(detail.steps_added_count, "0")} step(s) only in the second bundle.</p>` : ""}
    ${detail.steps_removed_count ? `<p>${safeNumber(detail.steps_removed_count, "0")} step(s) only in the first bundle.</p>` : ""}
    ${detail.steps_changed_count ? `<p>${safeNumber(detail.steps_changed_count, "0")} shared step(s) changed.</p>` : ""}
    ${detail.params_changed ? '<p class="chip warn">parameter defaults differ</p>' : ""}
    <p><a href="#/workflows/${enc(firstId)}">← back</a></p>`;
}

async function viewRuns() {
  const runs = await api("/api/runs");
  const rows = runs.map((run) => {
    const presentation = runPresentation(run);
    const status = run.load_error
      ? '<span class="chip err">unreadable</span>'
      : `<span class="chip ${presentation.cssClass}">${presentation.label}</span>`;
    return `<tr class="rowlink" data-route="#/runs/${enc(run.id)}">
      <td class="mono">${esc(run.id)}</td><td>Protected workflow</td>
      <td>${fmtTime(run.started_at)}</td><td>${status}${run.approved ? ' <span class="chip info">approved</span>' : ""}</td>
      <td>${safeNumber(run.n_results, "0")}${Number(run.n_failed) ? ` <span class="chip err">${safeNumber(run.n_failed, "0")} failed</span>` : ""}</td>
      <td>${run.identity_applicable_steps != null ? `${safeNumber(run.identity_armed_steps, "0")}/${safeNumber(run.identity_applicable_steps, "0")}` : "—"}</td>
      <td>${run.total_ms != null ? `${safeNumber((Number(run.total_ms) / 1000).toFixed(1))}s` : "—"}</td></tr>`;
  }).join("");
  return `<h2>Run history</h2>
    <table><tr><th>Run</th><th>Workflow</th><th>Started</th><th>Status</th>
    <th>Steps</th><th>Identity armed</th><th>Duration</th></tr>${rows ||
    '<tr><td colspan="7" class="muted">no runs found under the runs root</td></tr>'}</table>`;
}

async function viewAttention() {
  const items = await api("/api/attention");
  if (!items.length) {
    return `<h2>Needs Attention</h2>
      <div class="attention-empty">
        <span class="chip ok">clear</span>
        <p>No halted workflow is waiting for local review.</p>
      </div>`;
  }
  return `<h2>Needs Attention <span class="chip warn">${safeNumber(items.length, "0")}</span></h2>
    <p class="muted">Protected values and paths stay in the local run artifacts.
      Only the person at this computer should complete CAPTCHA, MFA, or sign-in
      in the live application. ${HEALTH.read_only
        ? "Start with explicit action enablement to make governed decisions."
        : "Continue verifies live outcomes before deterministic resume; it never answers the challenge."}</p>
    <div class="attention-list">${items.map((item) => `
      <article class="attention-card">
        <div class="attention-card-head">
          <span class="chip ${item.human_required ? "info" : "warn"}">${esc(item.category)}</span>
          <span class="muted">${fmtTime(item.created_at)}</span>
          <span class="mono muted">${esc(String(item.id).slice(0, 8))}</span>
        </div>
        <h3>${esc(item.headline)}</h3>
        <p>${esc(item.next_action)}</p>
        <p class="muted">${safeNumber(item.observed_text_count, "0")} protected observation(s) ·
          ${safeNumber(item.completed_intent_count, "0")} prior verified step label(s)</p>
        <p>
          <span class="chip">local evidence only</span>
          ${item.encrypted_pause ? '<span class="chip info">encrypted pause</span>' : ""}
          ${item.status === "approved" ? '<span class="chip ok">approved</span>' : ""}
        </p>
        <div class="attention-actions">
          <a class="button-link" href="#/attention/${enc(item.id)}">Review decision</a>
          <a class="button-link" href="#/runs/${enc(item.id)}">Full run evidence</a>
        </div>
        <p class="attention-result muted" aria-live="polite"></p>
      </article>`).join("")}</div>`;
}

const coverageValue = (confirmed, required) => required == null
  ? "No retained check for this halt"
  : `${safeNumber(confirmed, "0")} of ${safeNumber(required, "0")} confirmed`;

function attendedDecisionButtons(detail) {
  const task = detail.task;
  if (!task || !HEALTH.attended_decisions_ready) return "";
  const allows = (action) => task.allowed_actions.includes(action);
  const common = `data-run-id="${esc(detail.item.id)}"
    data-capability="${esc(task.capability_digest)}"
    data-task-digest="${esc(detail.task_digest)}"
    data-task-signature="${esc(task.signature)}"`;
  return `
    ${HEALTH.attended_actions_ready && allows("verify_and_resume") ? `
      <button class="decision-primary" data-attended-action="continue" ${common}>
        Re-check and continue
      </button>` : ""}
    ${HEALTH.attended_actions_ready && allows("skip") ? `
      <button data-attended-action="skip" ${common}>Skip this step</button>` : ""}
    ${allows("teach") ? `
      <button data-attended-action="teach" ${common}>Teach the fix</button>` : ""}
    ${allows("escalate") ? `
      <button data-attended-action="escalate" ${common}>Needs more time</button>` : ""}`;
}

async function viewAttentionDetail(id) {
  const detail = await api(`/api/attention/${id}`);
  const item = detail.item;
  const task = detail.task;
  const evidence = task ? task.evidence : {};
  const latestFrame = detail.presentation.after_artifact_id ||
    detail.presentation.before_artifact_id;
  const latestCaption = detail.presentation.after_artifact_id
    ? "Latest retained local frame"
    : "Frame before the halted step";
  return `<div class="decision-shell">
    <a class="decision-back" href="#/attention">← Needs Attention</a>
    <div class="decision-title-row">
      <span class="chip ${item.human_required ? "info" : "warn"}">${esc(item.category)}</span>
      ${task ? `<span class="chip">expires ${fmtTime(task.expires_at)}</span>` : ""}
    </div>
    <h2>${esc(detail.presentation.question)}</h2>
    <p class="decision-explanation">${esc(detail.presentation.explanation)}</p>
    ${latestFrame ? `<div class="decision-media">
      ${shot(item.id, latestFrame, latestCaption)}
    </div>` : `<div class="attention-empty">
      <p>No retained frame is available. Review the live application and full run evidence.</p>
    </div>`}
    <section class="decision-checks" aria-label="What OpenAdapt checked">
      <h3>What OpenAdapt checked</h3>
      <div><span>Record identity</span><strong>${esc(coverageValue(
        evidence.identity_confirmed_count,
        evidence.identity_required_count,
      ))}</strong></div>
      <div><span>Business effect</span><strong>${esc(coverageValue(
        evidence.effect_confirmed_count,
        evidence.effect_required_count,
      ))}</strong></div>
      <div><span>Action delivery</span><strong>${esc(
        task ? task.delivery_state.replaceAll("_", " ") : "No signed pause capability",
      )}</strong></div>
    </section>
    <p>${esc(detail.presentation.next_action)}</p>
    <p class="decision-assurance">${esc(detail.presentation.assurance)}</p>
    <div class="decision-actions">
      ${attendedDecisionButtons(detail)}
      <a class="button-link" href="#/runs/${enc(item.id)}">Inspect full evidence</a>
    </div>
    <p class="attention-result muted" aria-live="polite"></p>
  </div>`;
}

async function attendedAction(button) {
  const card = button.closest(".attention-card, .decision-shell");
  const output = card ? card.querySelector(".attention-result") : null;
  const action = button.dataset.attendedAction;
  const runId = button.dataset.runId;
  const capability = button.dataset.capability;
  const taskDigest = button.dataset.taskDigest;
  const taskSignature = button.dataset.taskSignature;
  if (!action || !runId || !capability || !taskDigest || !taskSignature) return;
  const siblings = card ? [...card.querySelectorAll("[data-attended-action]")] : [button];
  siblings.forEach((item) => { item.disabled = true; });
  if (output) output.textContent = "Submitting governed decision…";
  const disposition = {
    continue: "completed_by_operator",
    skip: "not_applicable",
    teach: "teach_requested",
    escalate: "needs_assistance",
  }[action];
  try {
    const result = await api(
      `/api/attention/${enc(runId)}/actions/${enc(action)}`,
      {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          capability_digest: capability,
          task_digest: taskDigest,
          task_signature: taskSignature,
          idempotency_key: crypto.randomUUID().replaceAll("-", ""),
          action,
          disposition,
        }),
      },
    );
    if (output) output.textContent = result.message || result.status;
    if (result.status === "completed" || result.status === "halted") {
      window.location.hash = "#/attention";
    }
  } catch (error) {
    const detail = error.body && error.body.detail;
    if (output) output.textContent = typeof detail === "string"
      ? detail
      : "Decision refused; reload the current pause and review live state.";
  } finally {
    siblings.forEach((item) => { item.disabled = false; });
  }
}

function shot(runId, artifactId, caption) {
  if (!artifactId) return "";
  const artifactUrl = `/api/runs/${enc(runId)}/artifact?id=${enc(artifactId)}`;
  return `<figure><img loading="lazy" alt="${esc(caption)} screenshot" data-artifact-url="${esc(artifactUrl)}">
    <figcaption>${esc(caption)} screenshot</figcaption></figure>`;
}

async function viewRunDetail(id) {
  const detail = await api(`/api/runs/${id}`);
  const summary = detail.summary;
  if (summary.load_error && !detail.report) {
    return `<h2>${esc(summary.id)}</h2><pre class="cmd">${esc(summary.load_error)}</pre>`;
  }
  const actions = await api(`/api/runs/${id}/actions`);
  const artifacts = await api(`/api/artifacts/json/runs/${id}`);

  let haltHtml = "";
  if (detail.halt) {
    haltHtml = `<div class="halt"><h3>HALT</h3>
      <p>Protected details remain in the local run report.</p>
      <p class="muted">${safeNumber(detail.halt.observed_text_count, "0")} observed text item(s);
        ${safeNumber(detail.halt.completed_intent_count, "0")} completed step label(s).</p>
    </div>`;
  }
  let pauseHtml = "";
  if (detail.pending_escalation) {
    const pending = detail.pending_escalation;
    pauseHtml = `<div class="halt pause">
      <h3>PAUSED — awaiting operator (${esc(pending.category || "")})</h3>
      <p>Protected details remain in the local checkpoint.</p>
      <p class="muted">resumes from step index ${safeNumber(pending.resume_from_index)} · status: ${esc(pending.status)}</p></div>`;
  } else if (detail.pending_escalation_encrypted) {
    pauseHtml = '<p class="chip warn">paused (escalation record is encrypted at rest)</p>';
  }
  if (detail.approval) {
    pauseHtml += `<p><span class="chip info">approved</span> by the local operator
      at ${fmtTime(detail.approval.approved_at)}</p>`;
  }

  const timeline = (detail.timeline || []).map((item) => {
    const status = item.skipped
      ? '<span class="chip">skipped</span>'
      : item.ok
        ? '<span class="chip ok">ok</span>'
        : item.safety_halt
          ? '<span class="chip err">SAFETY HALT</span>'
          : '<span class="chip err">failed</span>';
    const identity = item.identity
      ? `<span class="chip ${item.identity.status === "verified" ? "ok" :
        item.identity.status === "mismatch" ? "err" : "warn"}"
        title="mode ${esc(item.identity.mode)}">${esc(item.identity.status)}</span>`
      : "";
    const effect = item.effect_verified === true
      ? '<span class="chip ok">effect confirmed</span>'
      : item.effect_verified === false
        ? '<span class="chip err">effect refused</span>'
        : item.effect_approved_unverified
          ? '<span class="chip warn">approved unverified</span>'
          : "";
    return `<tr><td class="mono">${esc(item.step_id)}</td>
      <td>${esc(item.intent)}${item.error ? `<br><span class="muted">${esc(item.error)}</span>` : ""}
        ${item.effect_results.length ? `<details><summary>effect verdicts</summary><p class="mono muted">${item.effect_results.map(esc).join("<br>")}</p></details>` : ""}
        ${(item.before_artifact_id || item.after_artifact_id) ? `<details><summary>screenshots</summary><div class="shots">
          ${shot(summary.id, item.before_artifact_id, "before")}${shot(summary.id, item.after_artifact_id, "after")}</div></details>` : ""}</td>
      <td>${status}</td><td>${identity}</td><td>${effect}</td>
      <td class="mono">${item.resolution_rung ? esc(item.resolution_rung) : "—"}</td>
      <td>${safeNumber((Number(item.elapsed_ms) / 1000).toFixed(2), "0")}s</td></tr>`;
  }).join("");

  const presentation = runPresentation(summary);
  return `<h2>Run ${esc(summary.id)}</h2>
    <div class="cards">
      <div class="card"><div class="lbl">Outcome</div><div class="big">${
        presentation.label}</div>
        <div class="lbl">${fmtTime(summary.started_at)}</div></div>
      <div class="card"><div class="lbl">Identity armed</div>
        <div class="big">${summary.identity_applicable_steps != null ? `${safeNumber(summary.identity_armed_steps, "0")}/${safeNumber(summary.identity_applicable_steps, "0")}` : "n/a"}</div>
        <div class="lbl">applicable steps</div></div>
      <div class="card"><div class="lbl">Egress</div>
        <div class="big">${summary.screenshots_may_leave_box ? "possible" : "none"}</div>
        <div class="lbl">${summary.screenshots_may_leave_box ? "model grounding enabled" : "fully local replay"}</div></div>
      <div class="card"><div class="lbl">Bundle digest</div>
        <div class="big mono compact">${summary.bundle_content_digest ? `${esc(String(summary.bundle_content_digest).slice(0, 16))}…` : "—"}</div>
        <div class="lbl">${detail.manifest ? "checkpoint manifest present" : ""}</div></div>
    </div>
    ${haltHtml}${pauseHtml}
    <h2>Timeline (${safeNumber((detail.timeline || []).length, "0")} steps)</h2>
    <table><tr><th>Step</th><th>Intent / evidence</th><th>Status</th><th>Identity</th>
    <th>Effect</th><th>Rung</th><th>Elapsed</th></tr>${timeline}</table>
    ${detail.checkpoints.length ? `<details><summary>${safeNumber(detail.checkpoints.length, "0")} durable checkpoint(s)</summary>
      <table><tr><th>File</th><th>Step</th><th>At</th></tr>${detail.checkpoints.map((checkpoint) =>
      `<tr><td class="mono">protected</td><td class="mono">${safeNumber(checkpoint.step_index)}</td><td>${fmtTime(checkpoint.created_at)}</td></tr>`).join("")}</table></details>` : ""}
    <h2>Local evidence artifacts</h2>
    <p class="muted">Exact protected run JSON/JSONL. It stays on this computer and is shown only in this authenticated console.</p>
    ${artifactLinks(artifacts, "runs", summary.id)}
    <h2>Actions</h2>${renderActions(actions, `/api/runs/${id}/actions/`)}`;
}

function pointerToken(value) {
  return String(value).replaceAll("~", "~0").replaceAll("/", "~1");
}

function pointerTokens(pointer) {
  if (pointer === "") return [];
  if (!pointer.startsWith("/")) throw new Error("JSON Pointer must start with /");
  return pointer.slice(1).split("/").map((token) => {
    if (/~(?:[^01]|$)/.test(token)) throw new Error("JSON Pointer escape is invalid");
    return token.replaceAll("~1", "/").replaceAll("~0", "~");
  });
}

function resolveJsonPointer(document, pointer) {
  let value = document;
  for (const token of pointerTokens(pointer)) {
    if (Array.isArray(value)) {
      if (!/^(0|[1-9][0-9]*)$/.test(token) || Number(token) >= value.length) {
        throw new Error("JSON Pointer does not exist");
      }
      value = value[Number(token)];
    } else if (value && typeof value === "object" && Object.hasOwn(value, token)) {
      value = value[token];
    } else {
      throw new Error("JSON Pointer does not exist");
    }
  }
  return value;
}

function childPointer(parent, key) {
  return `${parent}/${pointerToken(key)}`;
}

function jsonValueClass(value) {
  if (value === null) return "null";
  if (typeof value === "string") return "string";
  if (typeof value === "number") return "number";
  if (typeof value === "boolean") return "boolean";
  return "compound";
}

function jsonPrimitive(value) {
  return value === null ? "null" : JSON.stringify(value);
}

function pointerHref(baseRoute, pointer) {
  return `${baseRoute}?pointer=${enc(pointer)}`;
}

function renderJsonChildren(value, pointer, targetPointer, baseRoute) {
  if (Array.isArray(value)) {
    let html = "";
    for (let index = 0; index < value.length; index += 1) {
      html += renderJsonNode(String(index), value[index], childPointer(pointer, index), targetPointer, baseRoute);
    }
    return html;
  }
  let html = "";
  for (const key of Object.keys(value)) {
    html += renderJsonNode(key, value[key], childPointer(pointer, key), targetPointer, baseRoute);
  }
  return html;
}

function renderJsonNode(key, value, pointer, targetPointer, baseRoute) {
  const keyHtml = key === null
    ? '<span class="json-root-label">document</span>'
    : `<span class="json-key">${esc(JSON.stringify(key))}</span><span class="json-colon">:</span>`;
  const deepLink = `<a class="json-deep-link" href="${esc(pointerHref(baseRoute, pointer))}"
    aria-label="Link to JSON Pointer ${esc(pointer || "root")}" title="Copyable JSON Pointer">#</a>`;
  if (value === null || typeof value !== "object") {
    return `<div class="json-leaf" data-json-pointer="${esc(pointer)}">${keyHtml}
      <span class="json-value ${jsonValueClass(value)}">${esc(jsonPrimitive(value))}</span>${deepLink}</div>`;
  }
  const count = Array.isArray(value) ? value.length : Object.keys(value).length;
  const preview = Array.isArray(value) ? `Array(${count})` : `{${count}}`;
  const open = pointer === "" || targetPointer === pointer || targetPointer.startsWith(`${pointer}/`);
  return `<details class="json-node" data-json-pointer="${esc(pointer)}" ${open ? "open" : ""}>
    <summary>${keyHtml}<span class="json-preview">${esc(preview)}</span>${deepLink}</summary>
    <div class="json-children" data-json-children data-hydrated="${open ? "true" : "false"}">${open ? renderJsonChildren(value, pointer, targetPointer, baseRoute) : ""}</div>
  </details>`;
}

function searchJsonDocument(document, query, baseRoute) {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return "";
  const matches = [];
  const stack = [{value: document, pointer: "", key: "document"}];
  while (stack.length && matches.length < 100) {
    const item = stack.pop();
    const primitive = item.value === null || typeof item.value !== "object"
      ? jsonPrimitive(item.value)
      : "";
    if (`${item.key} ${item.pointer} ${primitive}`.toLocaleLowerCase().includes(needle)) {
      matches.push(item);
    }
    if (Array.isArray(item.value)) {
      for (let index = item.value.length - 1; index >= 0; index -= 1) {
        stack.push({
          value: item.value[index],
          pointer: childPointer(item.pointer, index),
          key: String(index),
        });
      }
    } else if (item.value && typeof item.value === "object") {
      const keys = Object.keys(item.value);
      for (let index = keys.length - 1; index >= 0; index -= 1) {
        const key = keys[index];
        stack.push({value: item.value[key], pointer: childPointer(item.pointer, key), key});
      }
    }
  }
  if (!matches.length) return '<p class="muted">No matches.</p>';
  return `<div class="json-search-matches">${matches.map((match) => `
    <a href="${esc(pointerHref(baseRoute, match.pointer))}">
      <span class="mono">${esc(match.pointer || "/")}</span>
      <span>${esc(match.key)}</span>
      ${match.value === null || typeof match.value !== "object"
        ? `<span class="muted">${esc(jsonPrimitive(match.value).slice(0, 120))}</span>`
        : ""}
    </a>`).join("")}</div>${matches.length === 100 ? '<p class="muted">Showing the first 100 matches.</p>' : ""}`;
}

async function viewJsonArtifact(scope, ownerId, artifactId, queryParams) {
  const artifact = await api(`/api/artifacts/json/${scope}/${ownerId}/${artifactId}`);
  const section = scope === "runs" ? "runs" : "workflows";
  const backRoute = `#/${section}/${enc(ownerId)}`;
  const baseRoute = `${backRoute}/artifacts/${enc(artifactId)}`;
  const requestedPointer = queryParams.get("pointer") || "";
  let targetPointer = requestedPointer;
  let pointerError = "";
  try {
    resolveJsonPointer(artifact.document, requestedPointer);
  } catch (error) {
    targetPointer = "";
    pointerError = error.message || "JSON Pointer is invalid";
  }
  currentJsonViewer = {
    scope,
    ownerId,
    artifactId,
    artifact,
    baseRoute,
    targetPointer,
  };
  return `<p><a href="${backRoute}">← Back to ${section === "runs" ? "run" : "workflow"}</a></p>
    <div class="json-viewer-head">
      <div><h2>${esc(artifact.label)}</h2>
        <p class="muted mono">sha256 ${esc(artifact.sha256)}</p></div>
      <div class="json-viewer-actions">
        <button data-json-action="copy">Copy</button>
        <button data-json-action="raw">Raw</button>
        <button data-json-action="download">Download</button>
        <span id="json-action-status" class="muted" aria-live="polite"></span>
      </div>
    </div>
    <div class="json-viewer-meta">
      <span class="chip info">${esc(artifact.format.toUpperCase())}</span>
      <span>${esc(humanBytes(artifact.size_bytes))}</span>
      <span>${safeNumber(artifact.node_count, "0")} nodes</span>
      <span>depth ${safeNumber(artifact.max_depth, "0")}</span>
      <span class="chip">local only</span>
    </div>
    ${pointerError ? `<p class="chip err">${esc(pointerError)}</p>` : ""}
    <label class="json-search-label" for="json-search">Search keys, values, or JSON Pointers</label>
    <input id="json-search" class="json-search" type="search" autocomplete="off" placeholder="Search this artifact…">
    <div id="json-search-results" aria-live="polite"></div>
    <pre id="json-raw" class="json-raw" hidden></pre>
    <div class="json-tree" id="json-tree">${renderJsonNode(null, artifact.document, "", targetPointer, baseRoute)}</div>`;
}

async function fetchCurrentJsonRaw() {
  if (!currentJsonViewer) throw new Error("No JSON artifact is open");
  const {scope, ownerId, artifactId} = currentJsonViewer;
  const response = await fetch(
    `/api/artifacts/json/${scope}/${enc(ownerId)}/${enc(artifactId)}/raw`,
    {headers: requestHeaders()},
  );
  if (!response.ok) throw new Error("The local artifact is no longer available");
  return response;
}

function activateJsonViewer() {
  if (!currentJsonViewer || !currentJsonViewer.targetPointer) return;
  requestAnimationFrame(() => {
    const target = [...document.querySelectorAll("[data-json-pointer]")].find(
      (node) => node.dataset.jsonPointer === currentJsonViewer.targetPointer,
    );
    if (target) {
      target.classList.add("json-target");
      target.scrollIntoView({block: "center"});
    }
  });
}

async function viewSkills() {
  const libraries = await api("/api/skills");
  currentSkillActions = [];
  if (!libraries.length) return `<h2>Skill libraries</h2>
    <p class="muted">No skills.json found under the scanned roots. A library is
    created by <span class="mono">openadapt-flow teach</span> next to the updated bundle.</p>`;
  return `<h2>Skill libraries</h2>${libraries.map((library) => {
    if (library.error) {
      return `<p class="chip err">${esc(library.id)}: ${esc(library.error)}</p>`;
    }
    return `<h2 class="mono library-path">library ${esc(library.id)}</h2>${library.skills.map((skill) => `
      <table><tr><th colspan="6">Skill ${esc(String(skill.id).slice(0, 8))}</th></tr>
      <tr><th>Version</th><th>Status</th><th>Score</th><th>Provenance</th><th>Note</th><th>Actions</th></tr>
      ${skill.versions.map((version) => {
        const actions = [];
        if (version.status === "candidate") {
          const index = currentSkillActions.push({
            library: library.id,
            skillId: skill.id,
            version: version.version,
            actionId: "promote",
          }) - 1;
          actions.push(`<button data-skill-action="${index}">Promote</button>`);
        }
        if (version.status !== "rolled_back") {
          const index = currentSkillActions.push({
            library: library.id,
            skillId: skill.id,
            version: version.version,
            actionId: "rollback",
          }) - 1;
          actions.push(`<button class="danger" data-skill-action="${index}">Roll back</button>`);
        }
        const statusClass = version.status === "active"
          ? "ok"
          : version.status === "candidate"
            ? "info"
            : version.status === "rolled_back"
              ? "err"
              : "";
        return `<tr>
          <td>v${safeNumber(version.version)}${version.parent_version != null ? `<span class="muted"> ← v${safeNumber(version.parent_version)}</span>` : ""}</td>
          <td><span class="chip ${statusClass}">${esc(version.status)}</span></td>
          <td>${version.validation_score != null ? safeNumber(version.validation_score) : "—"}</td>
          <td class="muted">${fmtTime(version.created_at)} · ${safeNumber(version.n_traces, "0")} trace(s)</td>
          <td class="muted">protected</td>
          <td>${actions.join(" ")}</td>
        </tr>`;
      }).join("")}</table>`).join("")}`;
  }).join("")}`;
}

function renderActions(actions, postBase) {
  if (!actions.length) return '<p class="muted">no governance actions apply here</p>';
  currentActions = {list: actions, postBase};
  return actions.map((action, index) => `<div class="action">
    <h3>${esc(action.title)} ${action.mutating ? "" : '<span class="chip">read-only</span>'}
        ${action.executable ? "" : '<span class="chip warn">copy to terminal</span>'}</h3>
    <p>${esc(action.description)}</p>
    <pre class="cmd">${esc(action.command)}</pre>
    <button class="copy" data-copy-action="${index}">Copy command</button>
    ${action.executable ? `<button ${HEALTH.read_only && action.mutating ? 'title="server is read-only; restart with --allow-actions"' : ""}
        data-confirm-action="${index}">${action.mutating ? "Run…" : "Evaluate…"}</button>` : ""}
  </div>`).join("");
}

function confirmAction(index) {
  const action = currentActions.list[index];
  if (!action) return;
  $("#modal-title").textContent = action.title;
  $("#modal-desc").textContent = HEALTH.read_only && action.mutating
    ? "Server is READ-ONLY: this will be refused; the command below is what you would run."
    : action.description;
  $("#modal-cmd").textContent = action.command;
  const inputs = $("#modal-inputs");
  inputs.replaceChildren();
  if (action.id === "approve") {
    const resolutionLabel = document.createElement("label");
    resolutionLabel.textContent = "Resolution";
    const resolutionInput = document.createElement("input");
    resolutionInput.id = "in-resolution";
    resolutionInput.placeholder = "what you decided to do";
    resolutionLabel.appendChild(resolutionInput);
    inputs.append(resolutionLabel);
  }
  pendingExec = async () => {
    const payload = {};
    if (action.id === "approve") {
      payload.resolution = ($("#in-resolution") || {}).value || "";
    }
    try {
      const result = await api(currentActions.postBase + action.id, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
      window.alert(`${action.id}: exit ${result.returncode}\n\n${result.stdout || ""}${result.stderr || ""}`);
      await route();
    } catch (error) {
      const detail = error.body && error.body.detail;
      window.alert(detail && detail.error
        ? `${detail.error}\n\nCopy instead:\n${detail.command}`
        : JSON.stringify(detail || error));
    }
    closeModal();
  };
  $("#modal").hidden = false;
}

async function skillAction(spec) {
  const reason = spec.actionId === "rollback"
    ? window.prompt("Rollback reason:", "rolled back from operator console")
    : null;
  if (spec.actionId === "rollback" && reason === null) return;
  try {
    const result = await api(`/api/skills/${enc(spec.skillId)}/actions/${spec.actionId}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        library: spec.library,
        version: spec.version,
        reason: reason || "",
      }),
    });
    window.alert(`${spec.actionId}: exit ${result.returncode}\n${result.stdout || result.stderr || ""}`);
    await route();
  } catch (error) {
    const detail = error.body && error.body.detail;
    window.alert(detail && detail.error
      ? `${detail.error}\n\nRun instead:\n${detail.command}`
      : JSON.stringify(detail || error));
  }
}

function closeModal() {
  $("#modal").hidden = true;
  pendingExec = null;
}

function clearArtifactObjectUrls() {
  artifactObjectUrls.forEach((url) => URL.revokeObjectURL(url));
  artifactObjectUrls = [];
}

async function hydrateAuthenticatedImages() {
  const images = [...document.querySelectorAll("img[data-artifact-url]")];
  await Promise.all(images.map(async (image) => {
    const response = await fetch(image.dataset.artifactUrl, {
      method: "GET",
      headers: requestHeaders(),
    });
    if (!response.ok) {
      image.alt = `${image.alt} (unavailable)`;
      return;
    }
    const objectUrl = URL.createObjectURL(await response.blob());
    artifactObjectUrls.push(objectUrl);
    image.src = objectUrl;
  }));
}

async function route() {
  const hash = window.location.hash || (HEALTH.attend ? "#/attention" : "#/workflows");
  const [path, query] = hash.slice(2).split("?");
  const parts = path.split("/").map(decodeURIComponent);
  const nav = parts[0] || "workflows";
  document.querySelectorAll("[data-nav]").forEach((link) =>
    link.classList.toggle("active", link.dataset.nav === nav));
  const main = $("#main");
  clearArtifactObjectUrls();
  currentJsonViewer = null;
  main.innerHTML = '<p class="muted">Loading…</p>';
  try {
    let html;
    const diffIndex = parts.indexOf("diff");
    const artifactIndex = parts.indexOf("artifacts");
    const queryParams = new URLSearchParams(query || "");
    if (nav === "attention" && parts.length > 1) {
      html = await viewAttentionDetail(enc(parts.slice(1).join("/")));
    } else if (nav === "attention") {
      html = await viewAttention();
    } else if ((nav === "workflows" || nav === "runs") && artifactIndex > 0) {
      html = await viewJsonArtifact(
        nav,
        enc(parts.slice(1, artifactIndex).join("/")),
        enc(parts[artifactIndex + 1] || ""),
        queryParams,
      );
    } else if (nav === "workflows" && diffIndex > 0) {
      html = await viewDiff(
        enc(parts.slice(1, diffIndex).join("/")),
        enc(parts.slice(diffIndex + 1).join("/")),
      );
    } else if (nav === "workflows" && parts.length > 1) {
      html = await viewWorkflowDetail(
        enc(parts.slice(1).join("/")),
        queryParams.get("policy"),
      );
    } else if (nav === "runs" && parts.length > 1) {
      html = await viewRunDetail(enc(parts.slice(1).join("/")));
    } else if (nav === "runs") {
      html = await viewRuns();
    } else if (nav === "skills") {
      html = await viewSkills();
    } else {
      html = await viewWorkflows();
    }
    main.innerHTML = html;
    await hydrateAuthenticatedImages();
    activateJsonViewer();
  } catch (error) {
    main.innerHTML = `<p class="chip err">error</p><pre class="cmd">${esc(JSON.stringify(error.body || String(error), null, 2))}</pre>`;
  }
}

document.addEventListener("click", async (event) => {
  const jsonAction = event.target.closest("[data-json-action]");
  if (jsonAction && currentJsonViewer) {
    const action = jsonAction.dataset.jsonAction;
    const actionStatus = $("#json-action-status");
    if (actionStatus) actionStatus.textContent = "";
    jsonAction.disabled = true;
    const originalLabel = jsonAction.textContent;
    try {
      const response = await fetchCurrentJsonRaw();
      if (action === "copy") {
        await navigator.clipboard.writeText(await response.text());
        jsonAction.textContent = "Copied";
        if (actionStatus) actionStatus.textContent = "Exact artifact copied.";
      } else if (action === "raw") {
        const raw = $("#json-raw");
        raw.textContent = await response.text();
        raw.hidden = !raw.hidden;
        jsonAction.textContent = raw.hidden ? "Raw" : "Hide raw";
      } else if (action === "download") {
        const objectUrl = URL.createObjectURL(await response.blob());
        const link = document.createElement("a");
        link.href = objectUrl;
        link.download = currentJsonViewer.artifact.download_name || "openadapt-artifact.json";
        link.hidden = true;
        document.body.append(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
        if (actionStatus) actionStatus.textContent = "Download started.";
      }
    } catch (error) {
      jsonAction.textContent = "Unavailable";
      if (actionStatus) actionStatus.textContent = "The protected local artifact could not be read.";
    } finally {
      jsonAction.disabled = false;
      if (action !== "raw") {
        window.setTimeout(() => { jsonAction.textContent = originalLabel; }, 1200);
      }
    }
    return;
  }
  const routeTarget = event.target.closest("[data-route]");
  if (routeTarget && !event.target.closest("a")) {
    window.location.hash = routeTarget.dataset.route;
    return;
  }
  const diffTarget = event.target.closest("[data-diff-source]");
  if (diffTarget) {
    const selected = $("#diffsel");
    if (selected) {
      window.location.hash = `#/workflows/${diffTarget.dataset.diffSource}/diff/${selected.value}`;
    }
    return;
  }
  const copyTarget = event.target.closest("[data-copy-action]");
  if (copyTarget) {
    const action = currentActions.list[Number(copyTarget.dataset.copyAction)];
    if (action) await navigator.clipboard.writeText(action.command);
    return;
  }
  const confirmTarget = event.target.closest("[data-confirm-action]");
  if (confirmTarget) {
    confirmAction(Number(confirmTarget.dataset.confirmAction));
    return;
  }
  const skillTarget = event.target.closest("[data-skill-action]");
  if (skillTarget) {
    const spec = currentSkillActions[Number(skillTarget.dataset.skillAction)];
    if (spec) await skillAction(spec);
    return;
  }
  const attendedTarget = event.target.closest("[data-attended-action]");
  if (attendedTarget) {
    await attendedAction(attendedTarget);
    return;
  }
});

document.addEventListener("input", (event) => {
  if (event.target.id !== "json-search" || !currentJsonViewer) return;
  const output = $("#json-search-results");
  output.innerHTML = searchJsonDocument(
    currentJsonViewer.artifact.document,
    event.target.value,
    currentJsonViewer.baseRoute,
  );
});

document.addEventListener("toggle", (event) => {
  const node = event.target;
  if (!node.matches || !node.matches("details.json-node") || !node.open || !currentJsonViewer) return;
  const children = node.querySelector(":scope > [data-json-children]");
  if (!children || children.dataset.hydrated === "true") return;
  const pointer = node.dataset.jsonPointer || "";
  try {
    const value = resolveJsonPointer(currentJsonViewer.artifact.document, pointer);
    children.innerHTML = renderJsonChildren(value, pointer, "", currentJsonViewer.baseRoute);
    children.dataset.hydrated = "true";
  } catch (error) {
    children.textContent = "This JSON Pointer is no longer available.";
  }
}, true);

$("#modal-cancel").addEventListener("click", closeModal);
$("#modal-go").addEventListener("click", () => pendingExec && pendingExec());
window.addEventListener("hashchange", route);

async function bootstrap() {
  consumeBootstrapToken();
  try {
    const session = await api("/api/session");
    CSRF_TOKEN = session.csrf_token || session.csrf || "";
    if (!CSRF_TOKEN) throw new Error("session response did not include a CSRF token");
    HEALTH = await api("/api/health");
    const mode = $("#mode");
    mode.replaceChildren();
    if (HEALTH.read_only) {
      mode.textContent = `read-only · v${HEALTH.version}`;
    } else {
      const enabled = document.createElement("span");
      enabled.className = "rw";
      enabled.textContent = "actions enabled";
      mode.append(enabled, document.createTextNode(` · v${HEALTH.version}`));
    }
    if (HEALTH.attend && !window.location.hash) {
      window.location.hash = "#/attention";
    } else {
      await route();
    }
  } catch (error) {
    $("#mode").textContent = "authentication required";
    $("#main").textContent = error.status === 401 || error.status === 403
      ? "Open this console with the authenticated launch URL."
      : "The operator console API is unavailable.";
  }
}

bootstrap();
