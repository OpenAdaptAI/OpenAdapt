// The local task shell: pair this phone, show one decision, relay one answer.
//
// This file renders what the runner sends. It contains no decision model: the
// question text, the evidence rows, the expiry, and the action set all come
// from openadapt-flow's signed task projection and are displayed verbatim.
// ACTION_WIRE below is a name mapping between the portable task vocabulary and
// the engine's own action names -- Flow owns both, and this table mirrors its
// `_ACTION_MAP`. It never adds, removes, or reorders an allowed action.
//
// The operator copy in this file is the shell's own job. Flow deliberately
// sends closed enums and counts and no prose, because a free-text explanation
// field is how protected content escapes a closed contract. Turning those
// enums into sentences therefore happens here, on the phone, from values that
// cannot carry a patient's name.

const ACTION_WIRE = {
  verify_and_resume: {
    wire: "continue",
    label: "I fixed it — check and continue",
    // What actually changes. This is the distinction an operator could not
    // make from three similar-looking buttons.
    brief: "This run only",
    consequence:
      "OpenAdapt re-reads the live screen right now and continues only if its " +
      "checks pass. The saved workflow is not changed, so the same drift will " +
      "stop the next run too.",
  },
  skip: {
    wire: "skip",
    label: "Skip this step",
    brief: "Leaves this step undone",
    consequence:
      "The workflow continues without this step. Offered only when the " +
      "workflow itself declares the step skippable.",
  },
  reject: {
    wire: "reject",
    // The disagreement answer. Its label is the assertion the operator makes,
    // and it is about THIS RUN -- not about the saved workflow, which is what
    // "Teach the correction" is for and which carries a requalification gate
    // this button does not.
    label: "Stop — this is wrong",
    brief: "Ends this run",
    consequence:
      "Ends this run now. Nothing in the application is touched and nothing " +
      "continues. Use it when you have looked at the live application and " +
      "OpenAdapt was right to stop. The saved workflow is not changed, and " +
      "this run cannot be resumed afterwards.",
  },
  teach: {
    wire: "teach",
    label: "Teach the correction",
    brief: "Changes future runs",
    consequence:
      "Records that this drift needs a corrective demonstration. Once someone " +
      "records it, future runs handle this on their own. Nothing continues " +
      "right now, and the run stays paused.",
  },
  escalate: {
    wire: "escalate",
    label: "Needs more help",
    brief: "Hands this to someone else",
    consequence:
      "Passes the decision on. The run stays paused exactly where it is and " +
      "nothing in the application is touched.",
  },
  reconcile: {
    // Reconciliation is not a retry. The runner looks for the declared
    // persisted effect of the earlier delivery and reports what it finds. It
    // never dispatches that earlier action again from this decision path.
    wire: "reconcile",
    label: "Check what happened",
    brief: "Checks, never resends",
    consequence:
      "OpenAdapt checks whether the earlier action already changed the system " +
      "of record. It does not send that action again. If it cannot prove the " +
      "result, the run stays paused for reconciliation.",
  },
};

const DISPOSITION = {
  continue: "completed_by_operator",
  skip: "not_applicable",
  reject: "rejected_by_operator",
  teach: "teach_requested",
  escalate: "needs_assistance",
  reconcile: "reconcile_requested",
};

// -------------------------------------------------- the terminal receipt shape
//
// Flow returns a closed `HumanDecisionReceiptV1`: a `state` and a `reason_code`
// from fixed enums, and deliberately NO message -- prose from the runner could
// carry an operator name, an exception string, or an observed value. The phone
// therefore owns the wording, and this is the only place it is decided.
//
// Every (state, reason_code) pair Flow can emit needs an entry. A pair with no
// entry falls through to an explicitly unknown outcome rather than to a
// refusal: rendering a real `halted` as "that decision was refused" is exactly
// the defect this table exists to prevent.

const RECEIPT_COPY = {
  "completed/verified_and_resumed": {
    label: "CHECKS PASSED",
    tone: "success",
    text:
      "Checked and continued. OpenAdapt re-read the live screen, its checks " +
      "passed, and the workflow moved on.",
    terminal: true,
  },
  "completed/skipped_and_resumed": {
    label: "STEP SKIPPED",
    tone: "neutral",
    text: "Step skipped. The workflow continued from the next step.",
    terminal: true,
  },
  "completed/reconciled_and_resumed": {
    label: "RESULT PROVED",
    tone: "success",
    text:
      "Reconciled and continued. OpenAdapt proved the earlier action already " +
      "changed the system of record. It did not send that action again.",
    terminal: true,
    reconciliation: true,
  },
  "halted/continuation_halted": {
    label: "PAUSED AGAIN",
    tone: "halted",
    text:
      "OpenAdapt continued and then stopped again further on. Nothing was " +
      "guessed. Open this run on the computer to see where it stopped.",
    terminal: true,
  },
  "refused/revalidation_refused": {
    label: "CHECK DID NOT PASS",
    tone: "halted",
    text:
      "OpenAdapt re-read the live screen and it is still not in the state this " +
      "step needs, so it did nothing. Fix it in the application, then answer " +
      "again.",
    terminal: false,
  },
  "expired/expired": {
    label: "DECISION EXPIRED",
    tone: "neutral",
    text:
      "This decision expired before it reached the computer. Reload for a " +
      "fresh one.",
    terminal: true,
  },
  "delivery_uncertain/delivery_uncertain": {
    label: "RESULT UNCERTAIN",
    tone: "uncertain",
    text:
      "This may already have been sent. Do not answer again — reconcile it on " +
      "the computer running OpenAdapt.",
    terminal: true,
  },
  "accepted_pending_runner/pending_runner": {
    label: "CHECKING LIVE STATE",
    tone: "pending",
    text: "Submitted. Waiting for this computer to check the live screen…",
    terminal: false,
    pending: true,
  },
  "demonstration_requested/demonstration_requested": {
    label: "CORRECTION REQUESTED",
    tone: "teach",
    text:
      "Saved as a correction to teach. The run stays paused until someone " +
      "records the new way on the computer.",
    terminal: true,
  },
  "escalated/escalation_recorded": {
    label: "ROUTED FOR HELP",
    tone: "teach",
    text:
      "Escalated. The run stays paused exactly where it is until someone picks " +
      "it up.",
    terminal: true,
  },
  // Deliberately not worded like the escalation above. That one says someone
  // will pick this up; this one says nobody will, because there is no longer a
  // run to pick up. An operator told the wrong one of those acts on it.
  "rejected/rejected_by_operator": {
    label: "RUN STOPPED",
    tone: "halted",
    text:
      "Stopped. This run is over and cannot be resumed, and nothing in the " +
      "application was touched. Start it again on the computer if it should be " +
      "attempted.",
    terminal: true,
  },
};

// Flow's own `_RECEIPT_STATE`: engine decision status -> receipt state. Kept so
// this shell renders a runner that has not yet adopted the closed receipt
// exactly as it renders one that has, instead of treating the older reply as a
// refusal.
const LEGACY_RECEIPT_STATE = {
  prepared: ["accepted_pending_runner", "pending_runner"],
  delivery_started: ["delivery_uncertain", "delivery_uncertain"],
  delivery_uncertain: ["delivery_uncertain", "delivery_uncertain"],
  completed: ["completed", "verified_and_resumed"],
  refused: ["refused", "revalidation_refused"],
  halted: ["halted", "continuation_halted"],
  needs_demonstration: ["demonstration_requested", "demonstration_requested"],
  escalated: ["escalated", "escalation_recorded"],
  rejected: ["rejected", "rejected_by_operator"],
};

// ------------------------------------------------- describing the halted step
//
// Flow sends `presentation.halt`: a typed category, the step's ordinal, its
// action kind, the target's role, and the target's label ONLY when the engine
// proved that label is static control chrome rather than record content. When
// it is withheld, these nouns still name the SHAPE, so "the field where you
// typed <value>" becomes "the text field" instead of leaking.

const ROLE_NOUN = {
  button: "button",
  checkbox: "checkbox",
  combobox: "dropdown",
  edit: "text field",
  hyperlink: "link",
  link: "link",
  listbox: "list",
  menubutton: "menu button",
  menuitem: "menu item",
  menuitemcheckbox: "menu item",
  menuitemradio: "menu item",
  radio: "option",
  radiobutton: "option",
  searchbox: "search box",
  slider: "slider",
  spinbutton: "number field",
  splitbutton: "button",
  switch: "switch",
  tab: "tab",
  tabitem: "tab",
  textbox: "text field",
  togglebutton: "switch",
  article: "panel",
  cell: "table cell",
  columnheader: "column heading",
  dataitem: "row",
  document: "page",
  grid: "table",
  gridcell: "table cell",
  group: "section",
  heading: "heading",
  image: "image",
  img: "image",
  list: "list",
  listitem: "list item",
  menu: "menu",
  option: "list option",
  region: "section",
  row: "row",
  rowheader: "row heading",
  table: "table",
  text: "text",
  tree: "tree",
  treeitem: "tree item",
};

const ACTION_VERB = {
  click: "click",
  double_click: "double-click",
  right_click: "right-click",
  drag: "drag",
  type: "type into",
  key: "press a key in",
  hotkey: "press a shortcut in",
  scroll: "scroll",
  wait: "wait for",
};

const RUNG_NOUN = {
  structural: "the page's own element identity",
  template: "the recorded picture of the target",
  template_global: "that picture anywhere on the screen",
  ocr: "the target's text label",
  geometry: "its position next to nearby text",
  grounder: "the configured fallback locator",
};

const RUNG_VERDICT = {
  resolved: "found it",
  failed: "did not find it",
  not_attempted: "nothing recorded to try",
  not_reached: "not needed",
  unavailable: "not available on this screen",
  unknown: "no result recorded",
};

const RECHECK_NOUN = {
  target_resolution: "find the target again on the live screen",
  record_identity: "confirm the record on screen is the intended one",
  postconditions: "confirm the screen reaches the expected state",
  system_of_record_effects: "confirm the result in the system of record",
  delivery_reconciliation: "reconcile an action that may already have been sent",
};

// ------------------------------------------------------------------- stakes
//
// Flow computes `risk_class` and seals it into the signed task, and this shell
// used to drop it. It is the one field that says what a wrong answer costs, so
// it goes at the very top -- above the question, above everything.
//
// Only `irreversible` and `consequential` are rendered. `unknown` is the
// majority case, and a row that says nothing on most halts is the mechanism by
// which its neighbours become ignorable too.

const RISK_COPY = {
  irreversible: {
    title: "This cannot be undone",
    body:
      "The step OpenAdapt stopped on writes something that cannot be taken " +
      "back. If you are not certain, hand it on instead of continuing.",
  },
  consequential: {
    title: "This writes to the system of record",
    body:
      "The step OpenAdapt stopped on changes stored data, and the result has " +
      "to be proved afterwards rather than assumed.",
  },
};

const TASK_KIND_COPY = {
  identity: "Record identity",
  effect: "Saved-result check",
  ambiguity: "Target ambiguity",
  human_step: "Human-only step",
  delivery_uncertain: "Delivery uncertainty",
  halt: "Workflow pause",
  operator_review: "Operator review",
};

const RISK_LABEL = {
  read_only: "Read-only",
  state_changing: "Changes application state",
  consequential: "Consequential write",
  irreversible: "Irreversible action",
  unknown: "Review required",
};

// --------------------------------------------------- what the engine cannot do
//
// Flow sends a one-sided `presentation.assurance`: "your answer does not mark
// the run verified, OpenAdapt re-checks the live state". Read on a phone that
// says *the computer will catch me*, which is an over-trust message -- it names
// the half of the boundary that protects the operator and omits the half they
// are solely responsible for. The engine can re-prove its own postconditions,
// identity contract, and effect contract. It cannot observe whether a person
// looked. So the shell states both halves, and states them above the actions
// rather than under them, because a sentence below the buttons is read after
// the decision if it is read at all.

const LIMIT_COPY =
  "OpenAdapt re-checks what it can measure and stops again if that fails. It " +
  "cannot check whether you actually looked. Answer from the live " +
  "application, not from this page.";

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
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
  } catch (error) {
    // The request left the phone and the answer did not come back. That is
    // status 0: uncertain, never failed and never retried on its own.
    return { status: 0, body: null };
  }
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
  hideActions();
}

// The action bar is `position: fixed`, so the page must reserve exactly its
// height or the last line of the card -- which is the outcome the operator just
// asked for -- sits underneath it. A constant guess was too small once the bar
// carried three two-line buttons, so measure the real element instead.
function syncActionBarSpace() {
  const height = actionBar.hidden ? 0 : actionBar.getBoundingClientRect().height;
  document.documentElement.style.setProperty(
    "--action-bar-height",
    `${Math.ceil(height)}px`,
  );
}

// `hidden` alone does not hide `.actions`: the stylesheet gives it a `display`,
// which beats the user-agent `[hidden] { display: none }` rule. The stylesheet
// now carries an explicit `.actions[hidden]`, and every hide goes through here
// so the reserved space is released at the same moment.
function hideActions() {
  closeGate();
  actionBar.hidden = true;
  actionBar.innerHTML = "";
  syncActionBarSpace();
}

if (typeof ResizeObserver === "function") {
  new ResizeObserver(syncActionBarSpace).observe(actionBar);
}
window.addEventListener("resize", syncActionBarSpace);

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

function describeTarget(halt) {
  const noun = ROLE_NOUN[halt.target_role] || "target";
  // A label is present only when Flow proved it is the control's own static
  // label. Otherwise the noun alone is the whole description, on purpose.
  return halt.target_label ? `the ${noun} labelled “${halt.target_label}”` : `the ${noun}`;
}

function describeStop(halt) {
  if (!halt) return "";
  const where =
    halt.step_ordinal && halt.step_count
      ? `Step ${halt.step_ordinal} of ${halt.step_count}`
      : "This step";
  const target = describeTarget(halt);
  const verb = ACTION_VERB[halt.action_kind] || "act on";
  switch (halt.category) {
    case "resolution":
      return `${where} could not start: OpenAdapt could not find ${target} on the screen, so it did not ${verb} anything.`;
    case "disambiguation":
      return `${where} found more than one thing that looked like ${target}, so it refused to pick one.`;
    case "identity":
      return `${where} could not confirm that the record on screen is the intended one, so it did not ${verb} ${target}.`;
    case "human_required":
      return `${where} needs a person: complete the sign-in, challenge, or verification in the live application yourself.`;
    case "unmet_guard":
    case "postcondition":
      return `${where} did not leave the application in the state the workflow expects after it tried to ${verb} ${target}.`;
    case "effect_refuted":
    case "effect_indeterminate":
    case "effect_escalated":
    case "effect_unverifiable":
    case "placeholder_effect":
      return `${where} acted, but the result could not be confirmed in the system of record.`;
    default:
      return `${where} stopped instead of guessing.`;
  }
}

function ladderRows(halt) {
  const rows = (halt && halt.resolution_ladder) || [];
  const tried = rows.filter((row) => row.verdict !== "not_attempted");
  if (tried.length === 0) return "";
  const items = tried
    .map(
      (row) =>
        `<li><span>${esc(RUNG_NOUN[row.rung] || row.rung)}</span>
         <span class="verdict">${esc(RUNG_VERDICT[row.verdict] || row.verdict)}</span></li>`,
    )
    .join("");
  return `<details class="tried"><summary>What OpenAdapt tried</summary>
    <ul class="ladder">${items}</ul></details>`;
}

function recheckBlock(halt, task) {
  const checks = (halt && halt.will_recheck) || [];
  if (checks.length === 0) return "";
  const items = checks
    .map((row) => {
      const noun = RECHECK_NOUN[row.check];
      if (!noun) return "";
      const count = typeof row.count === "number" ? ` (${row.count})` : "";
      return `<li>${esc(noun)}${esc(count)}</li>`;
    })
    .filter(Boolean)
    .join("");
  if (!items) return "";
  const reconciling = task && task.task_kind === "delivery_uncertain";
  const heading = reconciling
    ? "If you select “Check what happened”"
    : "If you answer “I fixed it”";
  const introduction = reconciling
    ? "OpenAdapt will not send the earlier action again. It checks the live application and must still:"
    : "OpenAdapt will not repeat the step blindly. It re-reads the live screen and must still:";
  return `<section class="recheck">
      <h2>${esc(heading)}</h2>
      <p class="muted">${esc(introduction)}</p>
      <ul>${items}</ul>
      <p class="muted">If any of those fail it stops again and changes nothing.</p>
    </section>`;
}

function stakesBlock(task) {
  const copy = RISK_COPY[task && task.risk_class];
  if (!copy) return "";
  return `<section class="stakes">
      <h2>${esc(copy.title)}</h2>
      <p>${esc(copy.body)}</p>
    </section>`;
}

// "about 14 minutes" / "about 3 hours" / "about 2 days". Deliberately coarse:
// the point is that the picture is old, not exactly how old, and a precise
// figure invites the reader to treat it as a live reading.
function describeAge(iso) {
  const then = Date.parse(iso || "");
  if (!Number.isFinite(then)) return "";
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 0) return "";
  if (seconds < 90) return "less than a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `about ${minutes} minutes`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `about ${hours} hour${hours === 1 ? "" : "s"}`;
  return `about ${Math.round(hours / 24)} days`;
}

// The frame is a RE-ENTRY CUE, not evidence. This operator was not doing this
// task when it stopped, so they have no goal to retrieve and the picture is the
// only orientation the interface offers -- which is why it stays. But it used
// to be introduced as "View current screen" while showing a retained artifact,
// on a product whose whole thesis is that the screen is not the record. An
// operator who reads "current", looks, and answers has produced a confident
// wrong answer by doing what the label told them to.
//
// So: name it as history, say how old it is, and point at the live application.
// There is deliberately NO refresh control. The runner does not re-observe on
// demand, and a refresh that re-fetched this same stored artifact would
// manufacture the exact illusion of liveness this wording exists to remove.
function frameBlock(task, presentation) {
  const frameId = presentation.after_artifact_id || presentation.before_artifact_id;
  if (!frameId) return "";
  const age = describeAge(task && task.created_at);
  const stopped = age ? `when it stopped ${age} ago` : "when it stopped";
  return `<figure class="shot">
      <div class="shot-head">
        <strong>Screen when OpenAdapt stopped</strong>
        <span>RETAINED FRAME</span>
      </div>
      <img id="frame" alt="The screen OpenAdapt retained when it stopped"
           data-artifact="${esc(frameId)}">
      <figcaption>OpenAdapt kept this picture ${esc(stopped)}. It is not live.
      Check the application before you answer.</figcaption>
    </figure>`;
}

function consequenceBlock(task) {
  const actions = (task && task.allowed_actions) || [];
  const items = actions
    .filter((action) => ACTION_WIRE[action])
    .map(
      (action) =>
        `<li><strong>${esc(ACTION_WIRE[action].label)}</strong>
         <span class="muted">${esc(ACTION_WIRE[action].consequence)}</span></li>`,
    )
    .join("");
  if (!items) return "";
  return `<section class="consequences"><h2>What each answer does</h2>
    <ul>${items}</ul></section>`;
}

function actionIsValidForTask(action, task) {
  // Types v0.7 closes this too, but preserve the boundary in Desktop: an old
  // or malformed runner must not turn a definitely un-sent action into a
  // reconciliation UI. The signed task remains the source of authority; this
  // is a presentation refusal, never an addition to its action set.
  return (
    action !== "reconcile" ||
    (task.task_kind === "delivery_uncertain" &&
      ["delivered", "unknown"].includes(task.delivery_state))
  );
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
  const halt = presentation.halt;
  const stopped = describeStop(halt);
  const taskKind = TASK_KIND_COPY[task && task.task_kind] || "Operator decision";
  const risk = RISK_LABEL[task && task.risk_class] || "Review required";
  const reason =
    presentation.explanation || stopped || "OpenAdapt stopped instead of guessing.";
  const nextCheck =
    task && task.task_kind === "delivery_uncertain"
      ? "OpenAdapt will check the saved result. It will not send the action again."
      : "OpenAdapt will re-read the live application before it continues.";
  render(`
    <button class="back" id="back">← All decisions</button>
    <section class="card">
      <div id="decision">
        <div class="task-kicker">
          <span>${esc(taskKind)}</span>
          <span>${esc(deliveryText || "Paused")}</span>
        </div>
        <section class="task-hero">
          <span aria-hidden="true">!</span>
          <div>
            <p>OpenAdapt needs your help</p>
            <h1>${esc(presentation.question || "Review the live application.")}</h1>
          </div>
        </section>
        <p class="task-reason">${esc(reason)}</p>
        <div class="task-signals">
          <span><small>Delivery</small><strong>${esc(deliveryText || "Paused")}</strong></span>
          <span><small>Risk</small><strong>${esc(risk)}</strong></span>
        </div>
        ${frameBlock(task, presentation)}
        <section class="task-next">
          <span aria-hidden="true">↻</span>
          <p><strong>After your answer</strong>${esc(nextCheck)}</p>
        </section>
        <details class="task-details">
          <summary>Technical details and action effects</summary>
          ${stakesBlock(task)}
          ${stopped && stopped !== reason ? `<p class="muted">${esc(stopped)}</p>` : ""}
          ${ladderRows(halt)}
          ${recheckBlock(halt, task)}
          ${evidenceRows(task)}
          ${
            task && task.expires_at
              ? `<p class="muted">This decision is valid until ${esc(task.expires_at)}.</p>`
              : ""
          }
          <p class="limit">${esc(LIMIT_COPY)}</p>
          ${consequenceBlock(task)}
        </details>
      </div>
      <section class="outcome" id="outcome" hidden>
        <strong></strong>
        <span></span>
      </section>
    </section>
    <div id="gate" class="gate-mark" aria-hidden="true"></div>
  `);
  document.getElementById("back").addEventListener("click", showList);
  const frame = document.getElementById("frame");
  if (frame) loadFrame(runId, frame);
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

// -------------------------------------------------------- the action gate
//
// The bar is `position: fixed`, which is correct mobile engineering and stays
// exactly where it is. What changes is the GATING. Previously the buttons were
// equally available at scroll position 0 and at scroll position 100%, so a
// complete tappable decision fitted in one viewport with no evidence consulted
// and there was no scroll depth at which reading was required. Everything that
// calibrates the answer was present on the page and none of it was on the path
// to the action.
//
// `#gate` is the last element in the document, so the answers become available
// only once the operator has reached the end of what the engine had to say.
// When the engine had little to say the gate is already on screen and the
// answers appear at once: this is meant to be fast when the answer is obvious
// and structurally slower when it is not, in proportion to the evidence, rather
// than a fixed tax on every decision.
//
// It fails OPEN. Without `IntersectionObserver` the answers render immediately;
// an operator who cannot answer at all is a worse outcome than one who answers
// early.

let gateObserver = null;

function closeGate() {
  if (gateObserver) {
    gateObserver.disconnect();
    gateObserver = null;
  }
}

function gatePrompt() {
  actionBar.innerHTML =
    `<p class="gate" role="status">Read this decision to the end — the answers ` +
    `are below it.</p>`;
  actionBar.hidden = false;
  syncActionBarSpace();
}

function renderActions(runId, detail) {
  const task = detail.task;
  if (!task || !Array.isArray(task.allowed_actions)) return;
  const buttons = task.allowed_actions
    .filter((action) => ACTION_WIRE[action] && actionIsValidForTask(action, task))
    .map(
      (action) =>
        // Each button carries its consequence, because "verify & continue",
        // "teach" and "needs more help" are indistinguishable from their names
        // alone -- and they differ in whether the saved workflow changes.
        //
        // Every button is styled identically, on purpose. The accent used to
        // follow `allowed_actions[0]`, and Flow puts `continue` at index 0
        // exactly when the step has enough contract for the engine to re-verify
        // a continue. That is a statement about what the engine can CATCH, and
        // rendering it as a filled primary turned it into a recommendation to
        // the operator -- emphasising continue precisely where a wrong answer
        // is catchable, which is the inverse of what a person needs, and in a
        // place they cannot see the difference. Which option looks like the
        // default is the largest measured lever on this kind of screen, so it
        // must not be set by a predicate that does not mean "recommended". No
        // field here means that, so nothing is emphasised.
        `<button data-action="${esc(action)}">
           <span class="label">${esc(ACTION_WIRE[action].label)}</span>
           <span class="brief">${esc(ACTION_WIRE[action].brief)}</span>
         </button>`,
    )
    .join("");
  if (!buttons) {
    hideActions();
    return;
  }
  const open = () => {
    closeGate();
    // The bar grows when the one-line prompt becomes three two-line buttons,
    // and it grows upward over content the operator was already reading. Take
    // the scroll position down by exactly that much so nothing that was on
    // screen is swallowed -- the same defect as the outcome line disappearing
    // under a taller-than-guessed bar.
    const wasHidden = actionBar.hidden;
    const before = wasHidden ? 0 : actionBar.getBoundingClientRect().height;
    actionBar.innerHTML = buttons;
    actionBar.hidden = false;
    syncActionBarSpace();
    const growth = actionBar.getBoundingClientRect().height - before;
    const scroller = document.scrollingElement || document.documentElement;
    // On first render the bar was absent. Moving the document by the new bar
    // height there hides the question under the sticky heading before the
    // operator reads it. Preserve the viewport only when an already-visible
    // gate or action bar grew.
    if (growth > 0 && scroller && !wasHidden) scroller.scrollTop += growth;
    actionBar.querySelectorAll("[data-action]").forEach((node) => {
      node.addEventListener("click", () => decide(runId, detail, node.dataset.action));
    });
  };
  const gate = document.getElementById("gate");
  if (!gate || typeof IntersectionObserver !== "function") {
    open();
    return;
  }
  gatePrompt();
  gateObserver = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) open();
  });
  gateObserver.observe(gate);
}

// Translate one reply into operator copy. Returns `terminal` (the decision is
// over -- take the buttons away) and `retry` (the runner refused without acting,
// so answering again after fixing the application is legitimate).
function hasReconciliationSuccessProof(body) {
  return (
    body &&
    body.report_success === true &&
    typeof body.transition_receipt_digest === "string" &&
    /^sha256:[0-9a-f]{64}$/.test(body.transition_receipt_digest)
  );
}

function interpretReply(status, body, portableAction) {
  if (status === 0 || status >= 500) {
    return {
      label: "RESULT UNCERTAIN",
      tone: "uncertain",
      text:
        "The result is uncertain. Do not answer again — check this decision on " +
        "the computer running OpenAdapt.",
      terminal: true,
    };
  }
  let state = body && body.state;
  let reason = body && body.reason_code;
  if (!state && body && body.status) {
    // A runner that still returns its own decision record rather than the
    // closed receipt. Map it through Flow's own status -> state table.
    const mapped = LEGACY_RECEIPT_STATE[body.status];
    if (mapped) {
      state = mapped[0];
      reason = mapped[1];
      if (state === "completed" && portableAction === "skip") {
        reason = "skipped_and_resumed";
      }
    }
  }
  if (state && reason) {
    const copy = RECEIPT_COPY[`${state}/${reason}`];
    if (copy) {
      // `reconciled_and_resumed` is a success-shaped outcome only when the
      // exact receipt commits to both a successful report and the transition
      // that consumed the pause. Do not translate a partial reply into proof.
      if (copy.reconciliation && !hasReconciliationSuccessProof(body)) {
        return {
          label: "PROOF INCOMPLETE",
          tone: "uncertain",
          text:
            "OpenAdapt returned an incomplete reconciliation receipt. It did " +
            "not claim the earlier action succeeded. Check this decision on " +
            "the computer running OpenAdapt.",
          terminal: true,
        };
      }
      if (
        portableAction === "reconcile" &&
        state === "refused" &&
        reason === "revalidation_refused"
      ) {
        return {
          label: "RESULT NOT PROVED",
          tone: "halted",
          text:
            "OpenAdapt could not yet prove the saved result. It did not send " +
            "the earlier action again. Review the live record, then check " +
            "again or hand this case to someone else.",
          terminal: false,
        };
      }
      return {
        label: copy.label,
        tone: copy.tone,
        text: copy.text,
        terminal: copy.terminal,
        pending: copy.pending,
      };
    }
    // A state this build has no wording for is reported as exactly that. It is
    // NOT a refusal: showing a real terminal outcome as "refused" is the defect
    // this branch exists to prevent.
    return {
      label: "UNKNOWN OUTCOME",
      tone: "uncertain",
      text:
        `OpenAdapt returned an outcome this phone has no wording for (${state}). ` +
        "Check this decision on the computer running OpenAdapt.",
      terminal: true,
    };
  }
  // No receipt at all: a pre-admission refusal (expired task, wrong binding,
  // action not allowed, another operator already answered).
  return {
    label: "DECISION NOT ACCEPTED",
    tone: "halted",
    text:
      (body && body.detail) ||
      "That decision was not accepted. Reload and review the live state.",
    terminal: false,
  };
}

function renderOutcome(outcome, reply) {
  outcome.hidden = false;
  outcome.className = `outcome ${reply.tone || "neutral"}`;
  outcome.querySelector("strong").textContent = reply.label || "RESULT";
  outcome.querySelector("span").textContent = reply.text;
}

async function decide(runId, detail, portableAction) {
  const outcome = document.getElementById("outcome");
  const buttons = Array.from(actionBar.querySelectorAll("button"));
  buttons.forEach((button) => (button.disabled = true));
  const wire = ACTION_WIRE[portableAction].wire;
  renderOutcome(outcome, {
    label: "CHECKING LIVE STATE",
    tone: "pending",
    text: "Submitted. Waiting for this computer to check the live screen…",
  });
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
  // Never translate an accepted tap into success: the runner's own terminal
  // state decides what this says.
  const reply = interpretReply(status, body, portableAction);
  renderOutcome(outcome, reply);
  if (reply.terminal) {
    const decision = document.getElementById("decision");
    if (decision) decision.hidden = true;
    outcome.classList.add("terminal");
    outcome.closest(".card").classList.add("result-card");
    hideActions();
    const scroller = document.scrollingElement || document.documentElement;
    if (scroller) scroller.scrollTop = 0;
  } else if (!reply.pending) {
    buttons.forEach((button) => (button.disabled = false));
  }
  // With the bar gone or resized, put the answer the operator is waiting for
  // back in view rather than leaving it below the fold.
  if (!reply.terminal && typeof outcome.scrollIntoView === "function") {
    outcome.scrollIntoView({ block: "nearest" });
  }
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
