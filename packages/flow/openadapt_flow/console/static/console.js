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

function runChip(run) {
  if (!run) return '<span class="muted">never run</span>';
  const cssClass = run.success ? "ok" : (run.paused ? "warn" : "err");
  const label = run.success
    ? "success"
    : (run.paused ? "paused" : (run.halted ? "halted" : "failed"));
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
    const status = run.load_error
      ? '<span class="chip err">unreadable</span>'
      : run.success
        ? '<span class="chip ok">success</span>'
        : run.paused
          ? '<span class="chip warn">paused</span>'
          : run.halted
            ? '<span class="chip err">halted</span>'
            : '<span class="chip err">failed</span>';
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
          <a class="button-link" href="#/runs/${enc(item.id)}">Review protected evidence</a>
          ${HEALTH.attended_decisions_ready && item.capability ? `
            ${HEALTH.attended_actions_ready &&
              item.capability.allowed_actions.includes("continue") ? `
              <button data-attended-action="continue" data-run-id="${esc(item.id)}"
                data-capability="${esc(item.capability.digest)}">I fixed it — Continue</button>` : ""}
            ${HEALTH.attended_actions_ready &&
              item.capability.allowed_actions.includes("skip") ? `
              <button data-attended-action="skip" data-run-id="${esc(item.id)}"
                data-capability="${esc(item.capability.digest)}">Skip / disposition</button>` : ""}
            ${item.capability.allowed_actions.includes("teach") ? `
              <button data-attended-action="teach" data-run-id="${esc(item.id)}"
                data-capability="${esc(item.capability.digest)}">Teach the fix</button>` : ""}
            ${item.capability.allowed_actions.includes("escalate") ? `
              <button data-attended-action="escalate" data-run-id="${esc(item.id)}"
                data-capability="${esc(item.capability.digest)}">Needs more time</button>` : ""}
          ` : ""}
        </div>
        <p class="attention-result muted" aria-live="polite"></p>
      </article>`).join("")}</div>`;
}

async function attendedAction(button) {
  const card = button.closest(".attention-card");
  const output = card ? card.querySelector(".attention-result") : null;
  const action = button.dataset.attendedAction;
  const runId = button.dataset.runId;
  const capability = button.dataset.capability;
  if (!action || !runId || !capability) return;
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
          idempotency_key: crypto.randomUUID().replaceAll("-", ""),
          action,
          disposition,
        }),
      },
    );
    if (output) output.textContent = result.message || result.status;
    if (result.status === "completed" || result.status === "halted") {
      await route();
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

  return `<h2>Run ${esc(summary.id)}</h2>
    <div class="cards">
      <div class="card"><div class="lbl">Outcome</div><div class="big">${
        summary.success ? "success" : summary.paused ? "paused" : summary.halted ? "halted" : "failed"}</div>
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
    <h2>Actions</h2>${renderActions(actions, `/api/runs/${id}/actions/`)}`;
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
  main.innerHTML = '<p class="muted">Loading…</p>';
  try {
    let html;
    const diffIndex = parts.indexOf("diff");
    if (nav === "attention") {
      html = await viewAttention();
    } else if (nav === "workflows" && diffIndex > 0) {
      html = await viewDiff(
        enc(parts.slice(1, diffIndex).join("/")),
        enc(parts.slice(diffIndex + 1).join("/")),
      );
    } else if (nav === "workflows" && parts.length > 1) {
      const queryParams = new URLSearchParams(query || "");
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
  } catch (error) {
    main.innerHTML = `<p class="chip err">error</p><pre class="cmd">${esc(JSON.stringify(error.body || String(error), null, 2))}</pre>`;
  }
}

document.addEventListener("click", async (event) => {
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
