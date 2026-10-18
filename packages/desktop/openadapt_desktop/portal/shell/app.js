// The local task shell: pair this phone, show one decision card at a time,
// relay one answer.
//
// This file renders what the runner sends. It contains no decision model: the
// question, the field values, the expiry and the action set all come from
// openadapt-flow's signed task projection and are displayed verbatim. Two kinds
// of decision share one card:
//
//   * a paused run (`/api/portal/tasks`), answered with the engine's own
//     action names through ACTION_WIRE below, and
//   * a save waiting for approval (`/api/portal/save-approvals`), answered
//     with `verify_and_resume` (save) or `reject` (don't save) plus one closed
//     reason.
//
// Interaction rules, each enforced here:
//
//   * Visible buttons are the primary way to answer. Swiping the card is a
//     shortcut for the same two buttons (right = the continuing answer, left =
//     stop or don't save), and the arrow keys do the same on a keyboard.
//   * An answer is held on this phone for UNDO_MS before it is sent. Nothing
//     is queued: if the phone is offline, the page is left, or the request
//     expires during that time, the answer is dropped and nothing is sent.
//   * When the step cannot be undone (`risk_class: irreversible`), a swipe or
//     tap only arms the answer. The person then matches one fact from the card
//     and holds a button for HOLD_MS. There is no undo after the hold.
//   * A save is approved only after the exact screenshot has loaded and its
//     SHA-256 matches `presentation.screenshot_sha256`. No picture, no
//     approval. Declining stays possible.
//
// ACTION_WIRE is a name mapping between the portable task vocabulary and the
// engine's own action names. Flow owns both, and this table mirrors its
// `_ACTION_MAP`. It never adds, removes, or reorders an allowed action.
//
// The operator copy in this file is the shell's own job. Flow deliberately
// sends closed enums and counts and no prose, because a free-text explanation
// field is how protected content escapes a closed contract. Turning those
// enums into sentences therefore happens here, on the phone, from values that
// cannot carry a patient's name. The one exception is `presentation.fields`
// on a save: the values that will be saved, served only by this runner, never
// cached, and never put in a notification.

const UNDO_MS = 5000;
const HOLD_MS = 1200;
// Flow refuses a decision body over 16 KiB, and so does the portal. The phone
// checks first so an oversized body is never sent at all.
const MAX_BODY_BYTES = 16 * 1024;
const MAX_FIELDS = 24;

const ACTION_WIRE = {
  verify_and_resume: {
    wire: "continue",
    short: "Continue",
    label: "I fixed it. Check and continue",
    toast: "Continuing this run",
    consequence:
      "OpenAdapt re-reads the live screen right now and continues only if its " +
      "checks pass. The saved workflow is not changed, so the same drift will " +
      "stop the next run too.",
  },
  skip: {
    wire: "skip",
    short: "Skip step",
    label: "Skip this step",
    toast: "Skipping this step",
    consequence:
      "The workflow continues without this step. Offered only when the " +
      "workflow itself declares the step skippable.",
  },
  reject: {
    wire: "reject",
    // The disagreement answer. It is about THIS RUN, not about the saved
    // workflow, which is what "Teach the fix" is for.
    short: "Stop run",
    label: "Stop. This is wrong",
    toast: "Stopping this run",
    consequence:
      "Ends this run now. Nothing in the application is touched and nothing " +
      "continues. The saved workflow is not changed, and this run cannot be " +
      "resumed afterwards.",
  },
  teach: {
    wire: "teach",
    short: "Teach the fix",
    label: "Teach the correction",
    toast: "Asking for a correction",
    consequence:
      "Records that this drift needs a corrective demonstration. Nothing " +
      "continues right now, and the run stays paused.",
  },
  escalate: {
    wire: "escalate",
    short: "Hand it on",
    label: "Needs more help",
    toast: "Handing this on",
    consequence:
      "Passes the decision on. The run stays paused exactly where it is and " +
      "nothing in the application is touched.",
  },
  reconcile: {
    // Reconciliation is not a retry. The runner looks for the declared
    // persisted effect of the earlier delivery and reports what it finds. It
    // never dispatches that earlier action again from this decision path.
    wire: "reconcile",
    short: "Check result",
    label: "Check what happened",
    toast: "Checking what happened",
    consequence:
      "OpenAdapt checks whether the earlier action already changed the system " +
      "of record. It does not send that action again. If it cannot prove the " +
      "result, the run stays paused.",
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

// ------------------------------------------------------------- emphasis
//
// The filled answer is the one answer that lets the work go on, and only when
// OpenAdapt re-checks the live application before anything happens. Two rules
// turn emphasis off: an irreversible step (a wrong "continue" there may be
// uncatchable, so no answer looks like the default) and more than one
// continuing answer (the shell would be choosing between them). Set
// EMPHASIS_ENABLED to false to emphasise nothing.
const EMPHASIS_ENABLED = true;
const CONTINUING = new Set(["verify_and_resume", "skip", "reconcile"]);

function continuingActions(actions) {
  return actions.filter((action) => CONTINUING.has(action));
}

function primaryAction(task, actions) {
  if (!EMPHASIS_ENABLED || !task || task.risk_class === "irreversible") return null;
  const continuing = continuingActions(actions);
  return continuing.length === 1 ? continuing[0] : null;
}

// -------------------------------------------------- the terminal receipt shape
//
// Flow returns a closed `HumanDecisionReceiptV1`: a `state` and a `reason_code`
// from fixed enums, and deliberately NO message. The phone owns the wording.
// Every (state, reason_code) pair Flow can emit needs an entry. A pair with no
// entry falls through to an explicitly unknown outcome rather than to a
// refusal.
//
// `advance` means the decision is over and nothing needs the person: the deck
// moves to the next card and the result shows in a short note. Everything else
// that is terminal stays on screen until the person acknowledges it.

const RECEIPT_COPY = {
  "completed/verified_and_resumed": {
    label: "Checks passed",
    tone: "success",
    text: "OpenAdapt re-read the live screen, its checks passed, and the workflow moved on.",
    terminal: true,
    advance: true,
  },
  "completed/skipped_and_resumed": {
    label: "Step skipped",
    tone: "neutral",
    text: "The workflow continued from the next step.",
    terminal: true,
    advance: true,
  },
  "completed/reconciled_and_resumed": {
    label: "Result proved",
    tone: "success",
    text:
      "OpenAdapt proved the earlier action already changed the system of " +
      "record. It did not send that action again.",
    terminal: true,
    advance: true,
    reconciliation: true,
  },
  "halted/continuation_halted": {
    label: "Paused again",
    tone: "halted",
    text:
      "OpenAdapt continued and then stopped again further on. Nothing was " +
      "guessed. Open this run on the computer to see where it stopped.",
    terminal: true,
  },
  "refused/revalidation_refused": {
    label: "Check didn't pass",
    tone: "halted",
    text:
      "OpenAdapt re-read the live screen and it is still not in the state this " +
      "step needs, so it did nothing. Fix it in the application, then answer again.",
    terminal: false,
  },
  "expired/expired": {
    label: "Expired",
    tone: "neutral",
    text: "This decision expired before it reached the computer. Nothing was done.",
    terminal: true,
  },
  "delivery_uncertain/delivery_uncertain": {
    label: "Result uncertain",
    tone: "uncertain",
    text:
      "This may already have been sent. Don't answer again. Check it on the " +
      "computer running OpenAdapt.",
    terminal: true,
  },
  "accepted_pending_runner/pending_runner": {
    label: "Checking the live screen",
    tone: "pending",
    text: "Accepted. The computer checks the live screen before anything continues.",
    terminal: true,
    advance: true,
  },
  "demonstration_requested/demonstration_requested": {
    label: "Correction requested",
    tone: "teach",
    text: "The run stays paused until someone records the new way on the computer.",
    terminal: true,
    advance: true,
  },
  "escalated/escalation_recorded": {
    label: "Handed on",
    tone: "teach",
    text: "The run stays paused exactly where it is until someone picks it up.",
    terminal: true,
    advance: true,
  },
  "rejected/rejected_by_operator": {
    label: "Run stopped",
    tone: "halted",
    text: "This run is over and nothing in the application was touched.",
    terminal: true,
    advance: true,
  },
};

// Flow's own `_RECEIPT_STATE`: engine decision status -> receipt state, so a
// runner that has not adopted the closed receipt renders the same way.
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

// ------------------------------------------------------- describing a halt
//
// Flow sends `presentation.halt`: a typed category, the step's ordinal, its
// action kind, the target's role, and the target's label ONLY when the engine
// proved that label is static control chrome rather than record content.

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
  radio: "option",
  radiobutton: "option",
  searchbox: "search box",
  slider: "slider",
  spinbutton: "number field",
  switch: "switch",
  tab: "tab",
  tabitem: "tab",
  textbox: "text field",
  cell: "table cell",
  grid: "table",
  gridcell: "table cell",
  heading: "heading",
  image: "image",
  img: "image",
  list: "list",
  listitem: "list item",
  menu: "menu",
  option: "list option",
  row: "row",
  table: "table",
  text: "text",
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
  structural: "The page's own element identity",
  template: "The recorded picture of the target",
  template_global: "That picture anywhere on the screen",
  ocr: "The target's text label",
  geometry: "Its position next to nearby text",
  grounder: "The configured fallback locator",
};

const RUNG_VERDICT = {
  resolved: "found it",
  failed: "didn't find it",
  not_attempted: "nothing recorded to try",
  not_reached: "not needed",
  unavailable: "not available on this screen",
  unknown: "no result recorded",
};

const RECHECK_NOUN = {
  target_resolution: "Find the target again on the live screen",
  record_identity: "Confirm the record on screen is the intended one",
  postconditions: "Confirm the screen reaches the expected state",
  system_of_record_effects: "Confirm the result in the system of record",
  delivery_reconciliation: "Reconcile an action that may already have been sent",
};

const LIMIT_COPY =
  "OpenAdapt re-checks what it can measure and stops again if that fails. It " +
  "can't check whether you looked. Answer from the live application, not " +
  "from this phone.";

// ------------------------------------------------------------ saves

const SAVE_ANSWER = {
  verify_and_resume: { short: "Save", toast: "Approving this save" },
  reject: { short: "Don't save", toast: "Not saving" },
};

// Flow's closed decline reasons. A reason the phone has no wording for is not
// offered: the phone never sends a code it can't name to the person.
const DECLINE_REASON = {
  wrong_values: { label: "The values are wrong", then: "Nothing is saved. The run stops at this step." },
  wrong_record: { label: "It's the wrong record", then: "Nothing is saved. The run stops and is flagged." },
  screen_problem: { label: "Something on the screen is off", then: "Nothing is saved. The run stops at this step." },
};

// Flow's closed refusal codes. A bare 404 is NOT one of these: it means the
// computer isn't answering this route (an older console, or nothing attending
// it), not that the decision is gone.
const SAVE_GONE = new Set(["request_not_waiting", "request_unknown", "already_answered"]);
const RUN_GONE = new Set(["no open attention item"]);

// ------------------------------------------------------------------ icons

const ICON = {
  yes: '<path d="M5 12.5l4.2 4.2L19 7"/>',
  no: '<path d="M6 6l12 12M18 6L6 18"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2.5"/><path d="M8.5 11V8a3.5 3.5 0 017 0v3"/>',
};

function icon(name) {
  return (
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">` +
    `${ICON[name]}</svg>`
  );
}

const MORE_ICON =
  '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false">' +
  '<circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>';

const RING_GLYPH = {
  success: '<path class="glyph" d="M42 61l12 12 25-26" fill="none" stroke-width="4"/>',
  halted: '<path class="glyph" d="M47 47h26v26H47z" fill="none" stroke-width="4"/>',
  uncertain: '<path class="glyph" d="M60 40v24M60 78v2" fill="none" stroke-width="5"/>',
  neutral: '<path class="glyph" d="M44 60h32" fill="none" stroke-width="4"/>',
  pending: '<path class="glyph" d="M60 44v17l11 7" fill="none" stroke-width="4"/>',
  teach: '<path class="glyph" d="M46 74l28-28M52 46h22v22" fill="none" stroke-width="4"/>',
};

function ring(tone) {
  const glyph = RING_GLYPH[tone] || RING_GLYPH.neutral;
  return `<div class="ring ${esc(tone)}" aria-hidden="true"><svg viewBox="0 0 120 120"
    stroke-linecap="round" stroke-linejoin="round">
    <circle class="r1" cx="60" cy="60" r="54" fill="none" stroke-width="3"/>
    <circle class="r2" cx="60" cy="60" r="54" fill="none" stroke-width="3" transform="rotate(-90 60 60)"/>
    ${glyph}</svg></div>`;
}

// ------------------------------------------------------------------ plumbing

const main = document.getElementById("main");
const actionBar = document.getElementById("actions");
const deviceLabel = document.getElementById("device");
const hint = document.getElementById("hint");
const announcer = document.getElementById("announce");

// Session-scoped storage only: the credential dies with the tab rather than
// persisting on a personal phone.
const store = window.sessionStorage;

function esc(value) {
  return String(value == null ? "" : value).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}

function $(id) {
  return document.getElementById(id);
}

function announce(text) {
  if (!announcer) return;
  announcer.textContent = "";
  announcer.textContent = text;
}

function reducedMotion() {
  return Boolean(
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
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

function flowCode(body) {
  return body && typeof body.detail === "string" ? body.detail : "";
}

// 401 is an unknown session and 403 a refused one (wrong origin, lost CSRF
// token). Either way this phone has to pair again; neither recorded anything.
function needsPairing(status) {
  return status === 401 || status === 403;
}

function bodyBytes(json) {
  return new Blob([json]).size;
}

// ------------------------------------------------------------- connection
//
// Offline is shown, and it closes the answers: a tap that can't reach the
// computer is not an answer. Nothing is queued to send later.

function isOffline() {
  return typeof navigator !== "undefined" && navigator.onLine === false;
}

function syncConnection() {
  const offline = isOffline();
  document.body.classList.toggle("offline", offline);
  $("offline-bar").hidden = !offline;
  actionBar.querySelectorAll("[data-answer], [data-opt], #hold").forEach((button) => {
    if (button.dataset.locked === "true") return;
    button.disabled = offline;
  });
  if (offline) {
    hint.textContent = "Reconnect to answer.";
  } else if (deck.card) {
    setHint();
  }
}

window.addEventListener("online", syncConnection);
window.addEventListener("offline", syncConnection);

// ------------------------------------------------------------------ state

const deck = {
  queue: [], // [{ kind: "run" | "save", id }]
  total: 0,
  savesAttended: true,
  runsAttended: true,
  card: null, // the decision on top of the deck
  armed: false,
  matched: false,
  pending: null, // an answer held for undo
  busy: false,
  receipts: [],
};

let tick = null;
let toastTimer = null;
let holdTimer = null;

function stopTimers() {
  clearInterval(tick);
  tick = null;
  clearTimeout(holdTimer);
  holdTimer = null;
}

function releaseFrame(card) {
  // A protected image is held only while its decision is on screen.
  if (card && card.frameUrl) URL.revokeObjectURL(card.frameUrl);
  if (card) card.frameUrl = null;
}

window.addEventListener("pagehide", () => {
  // Leaving the page drops a held answer. It is never sent later.
  if (deck.pending) dropPending();
  releaseFrame(deck.card);
});

function setQueueRow() {
  const row = $("queue-row");
  const waiting = deck.queue.length;
  row.hidden = waiting === 0;
  $("count").textContent = waiting === 1 ? "1 waiting" : `${waiting} waiting`;
  const shown = deck.total < 2 ? 0 : Math.min(deck.total, 8);
  const on = Math.min(deck.total - waiting, shown - 1);
  $("pips").innerHTML = Array.from(
    { length: shown },
    (_, i) => `<span class="pip${i === on ? " on" : ""}"></span>`,
  ).join("");
}

// A full-deck screen: pairing, empty, a result, an error. Never a decision.
function screen(html, { focus = true } = {}) {
  stopTimers();
  closeSheet(false);
  releaseFrame(deck.card);
  deck.card = null;
  deck.armed = false;
  hideActions();
  hint.textContent = "";
  main.innerHTML = `<section class="panel">${html}</section>`;
  const heading = main.querySelector("h1");
  if (heading) {
    heading.setAttribute("tabindex", "-1");
    if (focus) heading.focus({ preventScroll: true });
    announce(heading.textContent.trim());
  }
}

function hideActions() {
  actionBar.hidden = true;
  actionBar.innerHTML = "";
}

// ------------------------------------------------------------------ pairing

async function claimPairing(secret) {
  // Remove the secret from the address bar before anything else can read it
  // from history, a screenshot, or a shared tab.
  history.replaceState(null, "", "/");
  let response = null;
  try {
    response = await fetch("/api/portal/pair/claim", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ secret, device_label: "Phone" }),
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
  } catch (error) {
    response = null;
  }
  const body = response ? await response.json().catch(() => null) : null;
  if (!response || response.status !== 200 || !body || !body.session_token) {
    screen(
      `<h1>Pairing didn't work</h1>
       <p>${esc((body && body.message) || "Show a new code on the computer running OpenAdapt, then scan it again.")}</p>`,
    );
    return;
  }
  store.setItem("portal_token", body.session_token);
  store.setItem("portal_csrf", body.csrf_token);
  const code = String(body.confirm_code || "");
  screen(
    `<span class="label">One more step</span>
     <h1>Type this code on the computer</h1>
     <p class="code" aria-label="Code ${esc(code.split("").join(" "))}">${esc(code)}</p>
     <p>Only this phone can see it. Typing it there proves this is your phone.</p>`,
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
    if (status !== 202 && status !== 0) break;
    await wait(2000);
  }
  screen(
    `<h1>Not approved</h1>
     <p>This phone wasn't approved in time. Show a new code on the computer
     running OpenAdapt and scan it again.</p>`,
  );
}

function startPairing(note) {
  store.removeItem("portal_token");
  store.removeItem("portal_csrf");
  deck.pending = null;
  deviceLabel.textContent = "";
  $("status").hidden = true;
  $("queue-row").hidden = true;
  screen(`<span class="label">Not paired</span>
    <h1>Pair this phone</h1>
    ${note ? `<p>${esc(note)}</p>` : ""}
    <ol>
      <li>On the computer, open OpenAdapt and choose Pair a phone.</li>
      <li>Scan the code it shows with this phone's camera.</li>
      <li>Type the code this phone then shows into the computer.</li>
    </ol>`);
}

// ------------------------------------------------------------- the queue

function unreachable() {
  screen(
    `<h1>Can't reach OpenAdapt</h1>
     <p>This phone can't reach the computer running OpenAdapt. Check the
     connection. Nothing was sent.</p>
     <button class="btn" id="retry" type="button">Try again</button>`,
  );
  $("retry").addEventListener("click", route);
}

function notAttended(body) {
  screen(
    `<h1>OpenAdapt isn't answering</h1>
     <p>${esc(
       (body && body.message) ||
         "The computer running OpenAdapt isn't attending decisions right now. Nothing was sent.",
     )}</p>
     <button class="btn" id="retry" type="button">Try again</button>`,
  );
  $("retry").addEventListener("click", loadQueue);
}

async function loadQueue() {
  const [runs, saves] = await Promise.all([
    api("/api/portal/tasks"),
    api("/api/portal/save-approvals"),
  ]);
  if (needsPairing(runs.status) || needsPairing(saves.status)) return startPairing();
  if (runs.status === 202 || saves.status === 202) return awaitApproval();
  if (runs.status === 0 && saves.status === 0) return unreachable();
  const runList = runs.status === 200 && Array.isArray(runs.body) ? runs.body : null;
  const saveList = saves.status === 200 && Array.isArray(saves.body) ? saves.body : null;
  // A 404 or 503 for one list means that half isn't being answered here. It
  // is not "nothing waiting", so the empty screen says so instead of claiming
  // all clear.
  deck.runsAttended = runList !== null;
  deck.savesAttended = saveList !== null;
  if (!runList && !saveList) return notAttended(runs.body || saves.body);
  const items = [];
  (saveList || []).forEach((item) => {
    const id = String((item && item.request_id) || "");
    if (/^[0-9a-f]{32}$/.test(id)) items.push({ kind: "save", id });
  });
  (runList || []).forEach((item) => {
    if (item && item.id) items.push({ kind: "run", id: String(item.id) });
  });
  deck.queue = items;
  deck.total = items.length;
  setQueueRow();
  if (items.length === 0) return renderDone();
  return openTop();
}

async function advance() {
  deck.queue.shift();
  setQueueRow();
  if (deck.queue.length === 0) return renderDone();
  return openTop();
}

async function openTop() {
  const item = deck.queue[0];
  if (!item) return renderDone();
  return item.kind === "save" ? openSave(item) : openRun(item);
}

function renderDone() {
  const partial = !deck.savesAttended || !deck.runsAttended;
  const note = !deck.savesAttended
    ? "No paused runs. Saves waiting for approval aren't reaching this phone right now, so check the computer for those."
    : !deck.runsAttended
      ? "No saves waiting. Paused runs aren't reaching this phone right now, so check the computer for those."
      : "Runs continue on their own. This phone hears from OpenAdapt only when something needs a person.";
  const receipts = deck.receipts
    .map(
      (r) => `<div class="receipt">
        <span class="rv">${esc(r.what)}</span><span class="ro ${esc(r.tone)}">${esc(r.result)}</span>
        <span class="rk">${esc(r.meta)}</span></div>`,
    )
    .join("");
  screen(
    `${ring(partial ? "neutral" : "success")}
     <h1>${partial ? "Nothing here right now" : "All clear"}</h1>
     <p>${esc(note)}</p>
     ${receipts ? `<div class="receipts">${receipts}</div>` : ""}
     <button class="btn" id="retry" type="button">Check again</button>`,
    { focus: false },
  );
  $("retry").addEventListener("click", loadQueue);
}

// --------------------------------------------------------- paused runs

function describeTarget(halt) {
  const noun = ROLE_NOUN[halt.target_role] || "target";
  // A label is present only when Flow proved it is the control's own static
  // label. Otherwise the noun alone is the whole description, on purpose.
  return halt.target_label ? `the ${noun} labelled “${halt.target_label}”` : `the ${noun}`;
}

function stepWhere(halt) {
  return halt && halt.step_ordinal && halt.step_count
    ? `Step ${halt.step_ordinal} of ${halt.step_count}`
    : "";
}

function describeStop(halt) {
  if (!halt) return "";
  const where = stepWhere(halt) || "This step";
  const target = describeTarget(halt);
  const verb = ACTION_VERB[halt.action_kind] || "act on";
  switch (halt.category) {
    case "resolution":
      return `${where} couldn't find ${target} on the screen, so OpenAdapt didn't ${verb} anything.`;
    case "disambiguation":
      return `${where} found more than one thing that looked like ${target}, so OpenAdapt picked none.`;
    case "identity":
      return `${where} couldn't confirm the record on screen is the intended one, so OpenAdapt didn't ${verb} ${target}.`;
    case "human_required":
      return `${where} needs a person: complete the sign-in, challenge, or check in the live application yourself.`;
    case "unmet_guard":
    case "postcondition":
      return `${where} didn't leave the application in the state the workflow expects after OpenAdapt tried to ${verb} ${target}.`;
    case "effect_refuted":
    case "effect_indeterminate":
    case "effect_escalated":
    case "effect_unverifiable":
    case "placeholder_effect":
      return `${where} acted, but the result couldn't be confirmed in the system of record.`;
    default:
      return `${where} stopped instead of guessing.`;
  }
}

function ladderBlock(halt) {
  const rows = ((halt && halt.resolution_ladder) || []).filter(
    (row) => row.verdict !== "not_attempted",
  );
  if (rows.length === 0) return "";
  return `<h3>What OpenAdapt tried</h3><ul>${rows
    .map((row) => `<li>${esc(RUNG_NOUN[row.rung] || row.rung)}: ${esc(RUNG_VERDICT[row.verdict] || row.verdict)}</li>`)
    .join("")}</ul>`;
}

function recheckBlock(halt) {
  const items = ((halt && halt.will_recheck) || [])
    .map((row) => {
      const noun = RECHECK_NOUN[row.check];
      if (!noun) return "";
      const count = typeof row.count === "number" ? ` (${row.count})` : "";
      return `<li>${esc(noun)}${esc(count)}</li>`;
    })
    .filter(Boolean)
    .join("");
  return items ? `<h3>Before anything continues, OpenAdapt must</h3><ul>${items}</ul>` : "";
}

function evidenceBlock(task) {
  const e = (task && task.evidence) || {};
  const rows = [
    ["Identity signals confirmed", e.identity_confirmed_count, e.identity_required_count],
    ["Effect signals confirmed", e.effect_confirmed_count, e.effect_required_count],
  ]
    .filter(([, got, need]) => got != null || need != null)
    .map(
      ([label, got, need]) =>
        `<div><dt>${esc(label)}</dt><dd>${esc(got == null ? "?" : got)} of ${esc(need == null ? "?" : need)}</dd></div>`,
    )
    .join("");
  return rows ? `<dl class="detail-list">${rows}</dl>` : "";
}

// "about 14 minutes". Deliberately coarse: the point is that the picture is
// old, not exactly how old.
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

function actionIsValidForTask(action, task) {
  // An old or malformed runner must not turn a definitely un-sent action into
  // a reconciliation UI. The signed task remains the source of authority;
  // this is a presentation refusal, never an addition to its action set.
  return (
    action !== "reconcile" ||
    (task.task_kind === "delivery_uncertain" &&
      ["delivered", "unknown"].includes(task.delivery_state))
  );
}

function runActions(task) {
  if (!task || !Array.isArray(task.allowed_actions)) return [];
  return task.allowed_actions.filter(
    (action) => ACTION_WIRE[action] && actionIsValidForTask(action, task),
  );
}

async function openRun(item) {
  const { status, body } = await api(`/api/portal/tasks/${encodeURIComponent(item.id)}`);
  if (needsPairing(status) || status === 202) return startPairing();
  if (status === 404) {
    return RUN_GONE.has(flowCode(body)) ? showGone("run") : notAttendedCard(body);
  }
  if (status !== 200 || !body || !body.task) return status === 0 ? unreachable() : notAttendedCard(body);
  const task = body.task;
  const presentation = body.presentation || {};
  const actions = runActions(task);
  const continuing = continuingActions(actions);
  const halt = presentation.halt;
  const frameId = presentation.after_artifact_id || presentation.before_artifact_id || null;
  const card = {
    kind: "run",
    id: item.id,
    detail: body,
    task,
    presentation,
    actions,
    left: actions.includes("reject") ? "reject" : null,
    right: continuing.length === 1 ? continuing[0] : null,
    primary: primaryAction(task, actions),
    irreversible: task.risk_class === "irreversible",
    expiresAt: expiryOf(task),
    title: "Run paused",
    step: stepWhere(halt),
    question: presentation.question || "Check the live application.",
    why: describeStop(halt) || presentation.explanation || "OpenAdapt stopped instead of guessing.",
    then:
      task.task_kind === "delivery_uncertain"
        ? "OpenAdapt checks the saved result. It won't send the action again."
        : "After your answer, OpenAdapt re-reads the live application before it continues.",
    fields: null,
    frameId,
    frameUrl: null,
    frameOk: false,
    frameState: frameId ? "loading" : "none",
    notice: null,
  };
  showCard(card);
  if (frameId) loadRunFrame(card);
}

async function loadRunFrame(card) {
  // Fetched as a blob so the image is held only in memory for this view; the
  // response is no-store and the service worker never sees a cacheable path.
  const { token } = session();
  let response = null;
  try {
    response = await fetch(
      `/api/portal/tasks/${encodeURIComponent(card.id)}/evidence?id=${encodeURIComponent(card.frameId)}`,
      {
        headers: { Authorization: `Bearer ${token}` },
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
      },
    );
  } catch (error) {
    response = null;
  }
  if (deck.card !== card) return;
  if (!response || !response.ok) {
    card.frameState = "missing";
  } else {
    card.frameUrl = URL.createObjectURL(await response.blob());
    card.frameOk = true;
    card.frameState = "ready";
  }
  if (deck.card === card) refreshEvidence();
}

// ------------------------------------------------------------- save approvals

function saveActions(task) {
  if (!task || task.task_kind !== "save_approval" || !Array.isArray(task.allowed_actions)) return [];
  return task.allowed_actions.filter((action) => SAVE_ANSWER[action]);
}

function declineReasons(presentation) {
  const offered = Array.isArray(presentation.decline_reasons) ? presentation.decline_reasons : [];
  return offered.filter((reason) => DECLINE_REASON[reason]);
}

// `presentation.fields`: what the save writes, as `{label, before, after}`.
// Anything malformed drops the whole list and the card falls back to the
// screenshot. The phone never shows half a list as if it were the whole.
function saveFields(presentation) {
  const raw = presentation && presentation.fields;
  if (!Array.isArray(raw) || raw.length === 0 || raw.length > MAX_FIELDS) return null;
  const text = (value) => value === null || value === undefined || typeof value === "string";
  const ok = raw.every(
    (field) =>
      field &&
      typeof field.label === "string" &&
      field.label.trim() !== "" &&
      text(field.before) &&
      text(field.after),
  );
  if (!ok) return null;
  return raw.map((field) => ({
    label: field.label,
    before: field.before == null ? null : field.before,
    after: field.after == null ? "" : field.after,
  }));
}

async function openSave(item) {
  const { status, body } = await api(`/api/portal/save-approvals/${encodeURIComponent(item.id)}`);
  if (needsPairing(status) || status === 202) return startPairing();
  if (status === 404) {
    return SAVE_GONE.has(flowCode(body)) ? showGone("save") : notAttendedCard(body);
  }
  if (status !== 200 || !body || !body.task) return status === 0 ? unreachable() : notAttendedCard(body);
  const task = body.task;
  const presentation = body.presentation || {};
  const actions = saveActions(task);
  if (actions.length === 0) {
    return resultCard({
      tone: "neutral",
      label: "Answer on the computer",
      title: "This phone can't show that request",
      text: "Answer it on the computer running OpenAdapt. Nothing was sent.",
    });
  }
  const step = Number.isInteger(presentation.step_index) ? `Step ${presentation.step_index + 1}` : "";
  const card = {
    kind: "save",
    id: item.id,
    detail: body,
    task,
    presentation,
    actions,
    left: actions.includes("reject") ? "reject" : null,
    right: actions.includes("verify_and_resume") ? "verify_and_resume" : null,
    primary:
      EMPHASIS_ENABLED && task.risk_class !== "irreversible" && actions.includes("verify_and_resume")
        ? "verify_and_resume"
        : null,
    irreversible: task.risk_class === "irreversible",
    expiresAt: expiryOf(task),
    title: "Approve a save",
    step,
    question: presentation.question || "Save this?",
    why: String(presentation.step_intent || "").trim() || "OpenAdapt filled the form and stopped before saving.",
    then:
      presentation.explanation ||
      "Before it saves, OpenAdapt reads the form again. If anything changed, it stops and asks again.",
    fields: saveFields(presentation),
    frameUrl: null,
    frameOk: false,
    frameState: "loading",
    notice: null,
  };
  showCard(card);
  loadSaveFrame(card);
}

function hex(buffer) {
  return Array.from(new Uint8Array(buffer), (b) => b.toString(16).padStart(2, "0")).join("");
}

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

// No picture, no approval. The frame must be a PNG whose SHA-256 is the
// digest Flow bound into the request. Anything else is shown as no picture.
async function loadSaveFrame(card) {
  const expected = String(card.presentation.screenshot_sha256 || "").toLowerCase();
  const { token } = session();
  let ok = false;
  let bytes = null;
  try {
    const response = await fetch(`/api/portal/save-approvals/${encodeURIComponent(card.id)}/frame`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
    const type = (response.headers && response.headers.get && response.headers.get("content-type")) || "";
    if (response.ok && type.startsWith("image/png") && /^[0-9a-f]{64}$/.test(expected)) {
      bytes = await response.arrayBuffer();
      const head = new Uint8Array(bytes.slice(0, 8));
      const isPng = PNG_SIGNATURE.every((value, i) => head[i] === value);
      const subtle = window.crypto && window.crypto.subtle;
      if (isPng && subtle) {
        ok = hex(await subtle.digest("SHA-256", bytes)) === expected;
      }
    }
  } catch (error) {
    ok = false;
  }
  if (deck.card !== card) return;
  if (ok) {
    card.frameUrl = URL.createObjectURL(new Blob([bytes], { type: "image/png" }));
    card.frameOk = true;
    card.frameState = "ready";
  } else {
    card.frameState = "missing";
    card.notice = {
      tone: "halted",
      label: "No picture, so no approval",
      text: "The exact screen couldn't be shown or didn't match the request. You can still choose Don't save, or answer on the computer.",
    };
  }
  refreshEvidence();
  refreshNotice();
  renderAnswers();
}

// ------------------------------------------------------------- expiry

function expiryOf(task) {
  const at = Date.parse((task && task.expires_at) || "");
  return Number.isFinite(at) ? at : null;
}

function clockTime(ms) {
  try {
    return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch (error) {
    return new Date(ms).toISOString().slice(11, 16);
  }
}

function countdown(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function timerMarkup(card) {
  if (card.expiresAt == null) return "";
  const created = Date.parse(card.task.created_at || "");
  const span = Number.isFinite(created) ? Math.max(1, card.expiresAt - created) : 3600000;
  const left = card.expiresAt - Date.now();
  const offset = (50.3 * (1 - Math.max(0, Math.min(1, left / span)))).toFixed(2);
  return `<div class="timer${left < 120000 ? " low" : ""}" id="timer" role="timer"
      aria-label="Closes at ${esc(clockTime(card.expiresAt))}">
      <svg viewBox="0 0 20 20" aria-hidden="true"><circle class="track" cx="10" cy="10" r="8" fill="none" stroke-width="2.4"/>
      <circle class="left" id="timer-left" cx="10" cy="10" r="8" fill="none" stroke-width="2.4"
        stroke-dasharray="50.3" stroke-dashoffset="${offset}" stroke-linecap="round"/></svg>
      <span id="timer-text">${esc(countdown(left))}</span></div>`;
}

function startTick(card) {
  clearInterval(tick);
  if (card.expiresAt == null) return;
  tick = setInterval(() => {
    if (deck.card !== card) return;
    const left = card.expiresAt - Date.now();
    if (left <= 0) return expire(card);
    const text = $("timer-text");
    if (text) text.textContent = countdown(left);
    const created = Date.parse(card.task.created_at || "");
    const span = Number.isFinite(created) ? Math.max(1, card.expiresAt - created) : 3600000;
    const ring = $("timer-left");
    if (ring) ring.setAttribute("stroke-dashoffset", (50.3 * (1 - Math.max(0, left / span))).toFixed(2));
    const timer = $("timer");
    if (timer) timer.classList.toggle("low", left < 120000);
  }, 1000);
}

function expire(card) {
  if (deck.pending && deck.pending.card === card) dropPending();
  return resultCard({
    tone: "neutral",
    label: "Expired",
    title: card.kind === "save" ? "This save expired" : "This decision expired",
    text: "Nothing was sent from this phone. OpenAdapt doesn't act on an expired request.",
  });
}

// ------------------------------------------------------------- the card

function evidenceMarkup(card) {
  if (card.frameState === "none") return "";
  const big = card.kind === "save" && !card.fields;
  const title = card.kind === "save" ? "Exactly what will be saved" : "Screen when it stopped";
  let sub;
  if (card.frameState === "loading") sub = "Loading the picture";
  else if (card.frameState === "missing") sub = card.kind === "save" ? "Couldn't be shown" : "Not available";
  else if (card.kind === "save") sub = "Tap to see the full screen";
  else {
    const age = describeAge(card.task.created_at);
    sub = `${age ? `${age.charAt(0).toUpperCase()}${age.slice(1)} ago` : "Kept earlier"} · not live`;
  }
  const image = card.frameOk
    ? `<img class="thumb" src="${esc(card.frameUrl)}" alt="">`
    : `<span class="thumb" aria-hidden="true"></span>`;
  const label = `<span><span class="t">${esc(title)}</span><span class="s">${esc(sub)}</span></span>`;
  if (!card.frameOk) {
    return `<div class="evidence missing${big ? " big" : ""}" id="evidence">${image}${label}</div>`;
  }
  return `<button class="evidence${big ? " big" : ""}" id="evidence" type="button" data-screen>${image}${label}</button>`;
}

function fieldsMarkup(card) {
  if (!card.fields) return "";
  return `<dl class="fields" aria-label="What this save writes">${card.fields
    .map(
      (field) => `<div><dt>${esc(field.label)}</dt><dd>${
        field.before != null && field.before !== field.after
          ? `<s><span class="sr">Was </span>${esc(field.before)}</s>`
          : ""
      }${field.after === "" ? '<span class="empty">(empty)</span>' : esc(field.after)}</dd></div>`,
    )
    .join("")}</dl>`;
}

function noticeMarkup(card) {
  const n = card.notice;
  if (!n) return "";
  return `<p class="notice ${esc(n.tone || "neutral")}" role="status"><strong>${esc(n.label)}</strong>${esc(n.text)}</p>`;
}

function yesLabel(card) {
  if (!card.right) return "";
  return card.kind === "save" ? SAVE_ANSWER.verify_and_resume.short : ACTION_WIRE[card.right].short;
}

function noLabel(card) {
  if (!card.left) return "";
  return card.kind === "save" ? SAVE_ANSWER.reject.short : ACTION_WIRE.reject.short;
}

function cardMarkup(card) {
  return `<div class="tint yes"></div><div class="tint no"></div>
    ${card.right ? `<div class="stamp yes" aria-hidden="true">${esc(yesLabel(card))}</div>` : ""}
    ${card.left ? `<div class="stamp no" aria-hidden="true">${esc(noLabel(card))}</div>` : ""}
    <div class="card-body" id="decision">
      <div class="meta"><div class="kind"><b>${esc(card.title)}</b>${
        card.step ? `<span aria-hidden="true">·</span><span>${esc(card.step)}</span>` : ""
      }</div>${timerMarkup(card)}</div>
      <h1 class="q" id="q" tabindex="-1">${esc(card.question)}</h1>
      <p class="why">${esc(card.why)}</p>
      ${card.irreversible ? `<span class="chip-risk">Can't be undone</span>` : ""}
      <div id="notice">${noticeMarkup(card)}</div>
      ${fieldsMarkup(card)}
      <div id="evidence-slot">${evidenceMarkup(card)}</div>
      <div class="spacer"></div>
      <p class="then">${esc(card.then)}</p>
    </div>`;
}

function showCard(card) {
  stopTimers();
  closeSheet(false);
  if (deck.card && deck.card !== card) releaseFrame(deck.card);
  deck.card = card;
  deck.armed = false;
  deck.matched = false;
  renderDeck();
  startTick(card);
  const q = $("q");
  if (q) q.focus({ preventScroll: true });
  announce(`${card.title}. ${card.question}`);
}

function renderDeck() {
  const card = deck.card;
  const behind = Math.min(2, Math.max(0, deck.queue.length - 1));
  let html = "";
  for (let depth = behind; depth >= 1; depth -= 1) {
    html += `<div class="card behind behind-${depth}" aria-hidden="true"></div>`;
  }
  html += `<article class="card${card.irreversible ? " irreversible" : ""}${card.fields ? " has-fields" : ""}" id="card"
    tabindex="0" aria-labelledby="q" aria-describedby="hint">${cardMarkup(card)}</article>`;
  main.innerHTML = html;
  setQueueRow();
  bindSwipe($("card"), card);
  renderAnswers();
}

function refreshEvidence() {
  const slot = $("evidence-slot");
  if (slot && deck.card) slot.innerHTML = evidenceMarkup(deck.card);
}

function refreshNotice() {
  const slot = $("notice");
  if (slot && deck.card) slot.innerHTML = noticeMarkup(deck.card);
}

function canApprove(card) {
  if (!card.right) return false;
  return card.kind !== "save" || card.frameOk;
}

function setHint() {
  const card = deck.card;
  if (!card) {
    hint.textContent = "";
    return;
  }
  if (isOffline()) {
    hint.textContent = "Reconnect to answer.";
  } else if (deck.armed) {
    hint.textContent = "Hold for about a second. Let go to cancel.";
  } else if (card.irreversible && card.right) {
    hint.textContent = `Swipe right or tap ${yesLabel(card)} to arm. A hold sends it.`;
  } else if (card.left && canApprove(card)) {
    hint.textContent =
      card.kind === "save"
        ? "Swipe right to save, left to decline."
        : `Swipe right to ${yesLabel(card).toLowerCase()}, left to stop.`;
  } else if (card.left) {
    hint.textContent = `Swipe left or tap ${noLabel(card)}.`;
  } else {
    hint.textContent = "";
  }
}

function renderAnswers() {
  const card = deck.card;
  if (!card || deck.pending) return;
  if (deck.armed) return renderArmed(card);
  $("app").classList.remove("armed");
  const yesDisabled = !canApprove(card);
  const left = card.left
    ? `<button class="ans no" type="button" data-answer="${esc(card.left)}">${icon("no")}<span>${esc(noLabel(card))}</span></button>`
    : `<span class="ans slot" aria-hidden="true"></span>`;
  const right = card.right
    ? `<button class="ans yes${card.primary === card.right && !yesDisabled ? " primary" : ""}" type="button"
        data-answer="${esc(card.right)}"${yesDisabled ? " disabled data-locked=\"true\"" : ""}>${icon("yes")}<span>${esc(yesLabel(card))}</span></button>`
    : `<span class="ans slot" aria-hidden="true"></span>`;
  actionBar.innerHTML = `${left}
    <button class="more" type="button" id="more" aria-label="More answers and details">${MORE_ICON}</button>
    ${right}`;
  actionBar.hidden = false;
  actionBar.querySelectorAll("[data-answer]").forEach((node) => {
    node.addEventListener("click", () => choose(node.dataset.answer));
  });
  $("more").addEventListener("click", openMore);
  setHint();
  syncConnection();
}

// ------------------------------------------------------- the irreversible path
//
// A swipe never commits a step that can't be undone. It arms the answer; the
// person then taps one fact that is on the card and holds the button.

function matchQuestion(card) {
  const fields = (card.fields || []).filter((field) => field.after !== "");
  const distinct = [];
  fields.forEach((field) => {
    if (!distinct.includes(field.after)) distinct.push(field.after);
  });
  if (fields.length > 0 && distinct.length >= 2) {
    // Prefer a date, then any value with a digit in it (an ID, an amount).
    // Those are the facts people misread.
    const target =
      fields.find((field) => /\d{4}-\d{2}-\d{2}|\d{1,2}\/\d{1,2}\/\d{2,4}/.test(field.after)) ||
      fields.find((field) => /\d/.test(field.after)) ||
      fields[0];
    const options = distinct.slice(0, 3);
    if (!options.includes(target.after)) options[0] = target.after;
    return {
      question: `Tap the “${target.label}” shown on the card.`,
      answer: target.after,
      options: rotate(options, card.id),
    };
  }
  // No fields to match: match the step number instead.
  const number = Number.parseInt(String(card.step).replace(/\D+/g, " ").trim().split(" ")[0], 10);
  if (Number.isInteger(number) && number > 0) {
    const options = [number - 1, number, number + 1].filter((n) => n > 0).map((n) => `Step ${n}`);
    return { question: "Which step is this?", answer: `Step ${number}`, options: rotate(options, card.id) };
  }
  return null;
}

// A stable, card-specific order, so the right answer isn't always in the
// same place and doesn't move between renders.
function rotate(options, seed) {
  const shift = String(seed)
    .split("")
    .reduce((sum, ch) => sum + ch.charCodeAt(0), 0) % options.length;
  return options.slice(shift).concat(options.slice(0, shift));
}

function renderArmed(card) {
  $("app").classList.add("armed");
  const match = matchQuestion(card);
  if (!match) deck.matched = true;
  const ready = deck.matched;
  const verb = yesLabel(card).toLowerCase();
  actionBar.innerHTML = `${
    match
      ? `<div class="match"><p class="mq" id="mq">${esc(match.question)}</p>
         <div class="opts" role="group" aria-labelledby="mq">${match.options
           .map(
             (option) =>
               `<button type="button" data-opt="${esc(option)}"${
                 ready && option === match.answer ? ' class="right" aria-pressed="true"' : ""
               }>${esc(option)}</button>`,
           )
           .join("")}</div></div>`
      : ""
  }
    <button class="hold" id="hold" type="button"${ready ? "" : ' disabled data-locked="true"'}
      aria-describedby="hint">
      <span class="fill" id="hold-fill"></span><span>${icon("lock")}<span id="hold-label">${
        ready ? `Hold to ${esc(verb)}` : "Match the fact first"
      }</span></span></button>
    <button class="quiet" type="button" id="disarm">Back</button>`;
  actionBar.hidden = false;
  if (match) {
    actionBar.querySelectorAll("[data-opt]").forEach((button) => {
      button.addEventListener("click", () => {
        if (button.dataset.opt === match.answer) {
          deck.matched = true;
          renderArmed(card);
          $("hold").focus({ preventScroll: true });
          announce(`Matched. Hold to ${verb}.`);
        } else {
          button.classList.remove("wrong");
          void button.offsetWidth;
          button.classList.add("wrong");
          announce("That isn't what the card shows. Check the card and try again.");
        }
      });
    });
  }
  $("disarm").addEventListener("click", () => {
    deck.armed = false;
    deck.matched = false;
    renderAnswers();
  });
  if (ready) bindHold($("hold"), card);
  setHint();
  syncConnection();
}

function bindHold(button, card) {
  button.style.setProperty("--hold-ms", `${HOLD_MS}ms`);
  let holding = false;
  const start = (event) => {
    if (button.disabled || holding || deck.busy || isOffline()) return;
    if (event && event.preventDefault) event.preventDefault();
    holding = true;
    button.classList.add("filling");
    $("hold-label").textContent = "Keep holding";
    clearTimeout(holdTimer);
    holdTimer = setTimeout(() => {
      holding = false;
      $("hold-label").textContent = "Sending";
      send(card, card.right, null);
    }, HOLD_MS);
  };
  const stop = () => {
    if (!holding) return;
    holding = false;
    clearTimeout(holdTimer);
    holdTimer = null;
    button.classList.remove("filling");
    $("hold-label").textContent = `Hold to ${yesLabel(card).toLowerCase()}`;
  };
  button.addEventListener("pointerdown", start);
  ["pointerup", "pointerleave", "pointercancel"].forEach((name) => button.addEventListener(name, stop));
  button.addEventListener("keydown", (event) => {
    if ((event.key === " " || event.key === "Enter") && !event.repeat) start(event);
  });
  button.addEventListener("keyup", (event) => {
    if (event.key === " " || event.key === "Enter") stop();
  });
  // A click alone (no hold) does nothing, but say so.
  button.addEventListener("click", (event) => {
    event.preventDefault();
    if (!holding && !deck.busy) announce("Press and hold for about a second.");
  });
}

// ------------------------------------------------------------- swipe

function bindSwipe(el, card) {
  let x0 = 0;
  let y0 = 0;
  let dx = 0;
  let dy = 0;
  let active = false;
  let pointer = null;
  const threshold = () => Math.max(60, Math.min(120, (el.offsetWidth || 0) * 0.3));
  const tintYes = el.querySelector(".tint.yes");
  const tintNo = el.querySelector(".tint.no");
  const stampYes = el.querySelector(".stamp.yes");
  const stampNo = el.querySelector(".stamp.no");
  const paint = (p) => {
    const yes = String(card.right && canApprove(card) ? Math.max(0, p) : 0);
    const no = String(card.left ? Math.max(0, -p) : 0);
    tintYes.style.opacity = yes;
    if (stampYes) stampYes.style.opacity = yes;
    tintNo.style.opacity = no;
    if (stampNo) stampNo.style.opacity = no;
  };

  el.addEventListener("pointerdown", (event) => {
    if (isOffline() || deck.pending || deck.busy || deck.armed) return;
    if (event.target && event.target.closest && event.target.closest("button")) return;
    active = true;
    pointer = event.pointerId;
    x0 = event.clientX;
    y0 = event.clientY;
    dx = 0;
    dy = 0;
    if (el.setPointerCapture && pointer != null) {
      try {
        el.setPointerCapture(pointer);
      } catch (error) {
        // A synthetic pointer has nothing to capture.
      }
    }
    el.classList.add("dragging");
    el.style.transition = "none";
  });
  el.addEventListener("pointermove", (event) => {
    if (!active || event.pointerId !== pointer) return;
    dx = event.clientX - x0;
    dy = event.clientY - y0;
    // A mostly vertical move is a scroll inside the card, not an answer.
    if (Math.abs(dy) > Math.abs(dx) * 1.5 && Math.abs(dx) < 24) return;
    el.style.transform = `translate(${dx}px, ${dy * 0.12}px) rotate(${dx / 22}deg)`;
    paint(Math.max(-1, Math.min(1, dx / threshold())));
  });
  const end = (event) => {
    if (!active || (event && event.pointerId !== pointer)) return;
    active = false;
    el.classList.remove("dragging");
    if (dx > threshold() && card.right) return choose(card.right, { swiped: true, dx, dy });
    if (dx < -threshold() && card.left) return choose(card.left, { swiped: true, dx, dy });
    springBack(el);
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
  el.addEventListener("click", (event) => {
    if (event.target && event.target.closest && event.target.closest("[data-screen]")) openScreen();
  });
  el.addEventListener("keydown", (event) => {
    if (event.target !== el || isOffline() || deck.pending || deck.busy) return;
    if (event.key === "ArrowRight" && card.right) {
      event.preventDefault();
      choose(card.right);
    } else if (event.key === "ArrowLeft" && card.left) {
      event.preventDefault();
      choose(card.left);
    }
  });
}

function springBack(el) {
  if (!el) return;
  el.style.transition = reducedMotion() ? "none" : "transform .45s cubic-bezier(.2,1.4,.3,1)";
  el.style.transform = "";
  el.querySelectorAll(".tint, .stamp").forEach((node) => {
    node.style.opacity = "0";
  });
}

function flyOut(side, dy = 0) {
  const el = $("card");
  return new Promise((resolve) => {
    if (!el) return resolve();
    const dir = side === "yes" ? 1 : -1;
    const tint = el.querySelector(`.tint.${side}`);
    const stamp = el.querySelector(`.stamp.${side}`);
    if (tint) tint.style.opacity = "1";
    if (stamp) stamp.style.opacity = "1";
    if (reducedMotion()) {
      el.style.opacity = "0";
      return resolve();
    }
    el.style.transition = "transform .36s cubic-bezier(.4,0,.6,1), opacity .36s";
    const width = el.offsetWidth || 390;
    el.style.transform = `translate(${dir * width * 1.4}px, ${dy * 0.3 + 30}px) rotate(${dir * 18}deg)`;
    el.style.opacity = "0";
    setTimeout(resolve, 360);
  });
}

// ------------------------------------------------------------- choosing

function choose(action, { swiped = false } = {}) {
  const card = deck.card;
  if (!card || deck.pending || deck.busy) return;
  if (isOffline()) {
    springBack($("card"));
    announce("This phone is offline. Nothing was sent.");
    return;
  }
  if (!card.actions.includes(action)) return;
  const continuing = card.kind === "save" ? action === "verify_and_resume" : CONTINUING.has(action);
  if (card.kind === "save" && action === "verify_and_resume" && !card.frameOk) {
    springBack($("card"));
    announce("No picture, so this phone can't approve. You can still choose Don't save.");
    return;
  }
  if (card.irreversible && continuing) {
    // Arm only. The hold sends it.
    springBack($("card"));
    closeSheet(false);
    deck.armed = true;
    deck.matched = false;
    card.right = action;
    renderAnswers();
    announce(`Armed. ${matchQuestion(card) ? matchQuestion(card).question : "Hold to send."}`);
    return;
  }
  if (card.kind === "save" && action === "reject") {
    if (swiped) springBack($("card"));
    return openReasons();
  }
  return holdForUndo(card, action, null);
}

async function holdForUndo(card, action, reason) {
  closeSheet(false);
  deck.pending = { card, action, reason, timer: null };
  const side = action === card.left ? "no" : "yes";
  hideActions();
  hint.textContent = "";
  await flyOut(side);
  if (!deck.pending || deck.pending.card !== card) return;
  // The card has left the deck. It comes back on Undo; nothing is sent yet.
  const gone = $("card");
  if (gone) gone.hidden = true;
  main.insertAdjacentHTML(
    "beforeend",
    `<div class="held" id="held"><p>Held on this phone.<br>Nothing is sent until the bar runs out.</p></div>`,
  );
  const message =
    card.kind === "save"
      ? reason
        ? `${SAVE_ANSWER.reject.toast}: ${DECLINE_REASON[reason].label.toLowerCase()}`
        : SAVE_ANSWER.verify_and_resume.toast
      : ACTION_WIRE[action].toast;
  showToast(message, `Sends in ${Math.round(UNDO_MS / 1000)} seconds`, UNDO_MS, {
    onDone: () => {
      if (!deck.pending || deck.pending.card !== card) return;
      deck.pending = null;
      send(card, action, reason);
    },
    onUndo: () => {
      if (!deck.pending || deck.pending.card !== card) return;
      deck.pending = null;
      announce("Undone. Nothing was sent.");
      restoreCard(card);
    },
  });
}

function dropPending() {
  const { card } = deck.pending;
  deck.pending = null;
  hideToast();
  return card;
}

function restoreCard(card) {
  if (deck.card !== card) return;
  renderDeck();
  const el = $("card");
  if (el) el.focus({ preventScroll: true });
}

// ------------------------------------------------------------- sending

function buildPayload(card, action, reason) {
  if (card.kind === "save") {
    // Exactly the fields Flow accepts: the task it showed, and one answer.
    const payload = {
      action,
      task_digest: card.detail.task_digest,
      task_signature: card.task.signature,
    };
    if (action === "reject") payload.decline_reason = reason;
    return payload;
  }
  const wire = ACTION_WIRE[action].wire;
  return {
    capability_digest: card.task.capability_digest,
    task_digest: card.detail.task_digest,
    task_signature: card.task.signature,
    idempotency_key: crypto.randomUUID().replaceAll("-", ""),
    action: wire,
    disposition: DISPOSITION[wire],
  };
}

async function send(card, action, reason) {
  if (deck.card !== card || deck.busy) return;
  if (isOffline()) {
    card.notice = {
      tone: "halted",
      label: "Not sent",
      text: "This phone went offline, so nothing was sent and nothing is queued. Answer again once it reconnects.",
    };
    deck.armed = false;
    return restoreCard(card);
  }
  if (card.expiresAt != null && card.expiresAt <= Date.now()) return expire(card);
  const json = JSON.stringify(buildPayload(card, action, reason));
  if (bodyBytes(json) > MAX_BODY_BYTES) {
    card.notice = { tone: "halted", label: "Not sent", text: "This answer was too large to send. Answer on the computer." };
    deck.armed = false;
    return restoreCard(card);
  }
  deck.busy = true;
  hideActions();
  hint.textContent = "Sending to the computer…";
  const held = $("held");
  if (held) held.innerHTML = "<p>Sending to the computer…</p>";
  announce("Sending your answer to the computer.");
  const path =
    card.kind === "save"
      ? `/api/portal/save-approvals/${encodeURIComponent(card.id)}/actions/${encodeURIComponent(action)}`
      : `/api/portal/tasks/${encodeURIComponent(card.id)}/actions/${encodeURIComponent(ACTION_WIRE[action].wire)}`;
  const { status, body } = await api(path, { method: "POST", body: json });
  deck.busy = false;
  if (deck.card !== card) return;
  if (needsPairing(status)) return startPairing("Pair again to answer. Nothing was sent from this phone.");
  const reply = card.kind === "save" ? interpretSaveReply(status, body, action) : interpretReply(status, body, action);
  return applyReply(card, action, reason, reply, body);
}

function hasReconciliationSuccessProof(body) {
  return (
    body &&
    body.report_success === true &&
    typeof body.transition_receipt_digest === "string" &&
    /^sha256:[0-9a-f]{64}$/.test(body.transition_receipt_digest)
  );
}

// Translate one reply to a paused-run answer into operator copy.
function interpretReply(status, body, portableAction) {
  if (status === 0 || status >= 500) {
    return {
      label: "Result uncertain",
      tone: "uncertain",
      text: "The result is uncertain. Don't answer again. Check this decision on the computer running OpenAdapt.",
      terminal: true,
    };
  }
  if (status === 404) {
    return RUN_GONE.has(flowCode(body))
      ? { gone: true }
      : {
          label: "Not sent to a run",
          tone: "halted",
          text: "The computer isn't attending this run right now, so nothing was done. Try again, or answer on the computer.",
          terminal: false,
        };
  }
  let state = body && body.state;
  let reason = body && body.reason_code;
  if (!state && body && body.status) {
    // A runner that still returns its own decision record rather than the
    // closed receipt. Map it through Flow's own status -> state table.
    const mapped = LEGACY_RECEIPT_STATE[body.status];
    if (mapped) {
      [state, reason] = mapped;
      if (state === "completed" && portableAction === "skip") reason = "skipped_and_resumed";
    }
  }
  if (state && reason) {
    const copy = RECEIPT_COPY[`${state}/${reason}`];
    if (copy) {
      // `reconciled_and_resumed` is a success only when the receipt commits to
      // both a successful report and the transition that consumed the pause.
      if (copy.reconciliation && !hasReconciliationSuccessProof(body)) {
        return {
          label: "Proof incomplete",
          tone: "uncertain",
          text:
            "OpenAdapt returned an incomplete reconciliation receipt and didn't " +
            "claim the earlier action succeeded. Check it on the computer running OpenAdapt.",
          terminal: true,
        };
      }
      if (portableAction === "reconcile" && state === "refused" && reason === "revalidation_refused") {
        return {
          label: "Result not proved",
          tone: "halted",
          text:
            "OpenAdapt couldn't prove the saved result yet and didn't send the " +
            "earlier action again. Review the live record, then check again or hand it on.",
          terminal: false,
        };
      }
      return { ...copy };
    }
    // A state this build has no wording for is reported as exactly that. It is
    // NOT a refusal.
    return {
      label: "Unknown outcome",
      tone: "uncertain",
      text: `OpenAdapt returned an outcome this phone has no wording for (${state}). Check it on the computer running OpenAdapt.`,
      terminal: true,
    };
  }
  // No receipt at all: a refusal before admission (expired task, wrong
  // binding, action not allowed, another operator already answered).
  return {
    label: "Not accepted",
    tone: "halted",
    text: "OpenAdapt didn't accept that answer and did nothing. Review the live state and answer again.",
    terminal: false,
  };
}

// Translate one reply to a save answer. Flow's refusal codes are closed, and
// a bare 404 means the computer isn't answering saves, not that this one is gone.
function interpretSaveReply(status, body, action) {
  if (status === 0 || status >= 500) return interpretReply(status, body, action);
  const code = flowCode(body);
  if ((status === 404 || status === 409) && SAVE_GONE.has(code)) return { gone: true };
  if (status === 409 && code === "task_changed") return { changed: true };
  if (status === 409 && code === "request_expired") return { expired: true };
  if (status === 404) {
    return {
      label: "Not sent to a save",
      tone: "halted",
      text: "The computer isn't answering saves from this phone right now. Nothing was saved. Answer on the computer.",
      terminal: false,
    };
  }
  const state = body && body.state;
  const reason = body && body.reason_code;
  if (status === 200 && action === "verify_and_resume" && state === "accepted_pending_runner" && reason === "pending_runner") {
    return {
      label: "Approved",
      result: "Approved",
      tone: "success",
      text: "OpenAdapt reads the form again and saves only if nothing changed.",
      terminal: true,
      advance: true,
    };
  }
  if (status === 200 && action === "reject" && state === "rejected" && reason === "rejected_by_operator") {
    return {
      label: "Not saved",
      result: "Not saved",
      tone: "neutral",
      text: "This save is stopped. Nothing was saved.",
      terminal: true,
      advance: true,
    };
  }
  if (status === 200 && state && reason) return interpretReply(status, body, action);
  return {
    label: "Not accepted",
    tone: "halted",
    text: "OpenAdapt didn't accept that answer and saved nothing. Look at the save again and answer.",
    terminal: false,
  };
}

function receiptFor(card, action, reason, reply, body) {
  const what =
    card.kind === "save"
      ? `Save${card.step ? ` · ${card.step}` : ""}`
      : `Paused run${card.step ? ` · ${card.step}` : ""}`;
  // The time Flow recorded the answer, not this phone's clock.
  const decided = Date.parse((body && body.decided_at) || "");
  const digest =
    body && typeof body.decision_digest === "string" && /^sha256:[0-9a-f]{64}$/.test(body.decision_digest)
      ? body.decision_digest.slice(7, 19)
      : "";
  const meta = [
    "On this phone",
    Number.isFinite(decided) ? clockTime(decided) : "",
    digest ? `receipt ${digest}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  const answer =
    card.kind === "save"
      ? reason
        ? `${SAVE_ANSWER.reject.short}: ${DECLINE_REASON[reason].label}`
        : SAVE_ANSWER.verify_and_resume.short
      : ACTION_WIRE[action].label;
  return { what, result: reply.result || reply.label, tone: reply.tone || "neutral", meta, answer };
}

function applyReply(card, action, reason, reply, body) {
  deck.armed = false;
  if (reply.gone) return showGone(card.kind);
  if (reply.changed) {
    return resultCard({
      tone: "neutral",
      label: "Changed",
      title: "This save changed",
      text: "The form or the request changed after this phone showed it. Nothing was sent. Look at it again before you answer.",
      next: "Look again",
      onNext: () => openTop(),
    });
  }
  if (reply.expired) return expire(card);
  if (!reply.terminal) {
    card.notice = { tone: reply.tone, label: reply.label, text: reply.text };
    restoreCard(card);
    announce(`${reply.label}. ${reply.text}`);
    return;
  }
  const receipt = receiptFor(card, action, reason, reply, body);
  deck.receipts.push(receipt);
  releaseFrame(card);
  if (reply.advance) {
    showToast(reply.label, reply.text, 2600);
    announce(`${reply.label}. ${reply.text}`);
    return advance();
  }
  return resultCard({
    tone: reply.tone,
    label: reply.label,
    title: reply.label,
    text: reply.text,
    receipt,
    advanceOnNext: true,
  });
}

// A full-deck result the person acknowledges. `id="outcome"` carries the tone
// and `terminal`, so the outcome is checkable without reading the words.
function resultCard({ tone = "neutral", label, title, text, receipt = null, next, onNext, advanceOnNext = true }) {
  const more = deck.queue.length > 1;
  screen(
    `${ring(tone)}
     <span class="label">${esc(label)}</span>
     <h1>${esc(title || label)}</h1>
     <p>${esc(text)}</p>
     ${
       receipt
         ? `<div class="receipts"><div class="receipt"><span class="rv">${esc(receipt.answer)}</span>
            <span class="ro ${esc(receipt.tone)}">${esc(receipt.result)}</span><span class="rk">${esc(receipt.meta)}</span></div></div>`
         : ""
     }
     <button class="btn primary" id="next" type="button">${esc(next || (more ? "Next" : "Done"))}</button>`,
  );
  const panel = main.querySelector(".panel");
  panel.id = "outcome";
  panel.classList.add("result", tone, "terminal");
  $("next").addEventListener("click", () => (onNext ? onNext() : advanceOnNext ? advance() : openTop()));
}

function showGone(kind) {
  return resultCard({
    tone: "neutral",
    label: "No longer waiting",
    title: kind === "save" ? "This save is no longer waiting" : "This run is no longer waiting",
    text: "It was answered elsewhere, already used, or it expired. Nothing was sent from this phone.",
  });
}

function notAttendedCard(body) {
  return resultCard({
    tone: "uncertain",
    label: "Not attended",
    title: "OpenAdapt isn't answering this one",
    text:
      (body && body.message) ||
      "The computer running OpenAdapt isn't attending this decision right now. It may still be waiting there. Nothing was sent.",
    next: "Skip for now",
  });
}

// ------------------------------------------------------------- toast

function showToast(message, sub, ms, { onDone = null, onUndo = null } = {}) {
  const toast = $("toast");
  const undo = $("undo");
  const bar = $("toast-bar");
  clearTimeout(toastTimer);
  $("toast-msg").innerHTML = `${esc(message)}<small>${esc(sub)}</small>`;
  undo.hidden = !onUndo;
  undo.onclick = () => {
    clearTimeout(toastTimer);
    toast.hidden = true;
    if (onUndo) onUndo();
  };
  bar.classList.remove("run");
  void bar.offsetWidth;
  bar.style.setProperty("--toast-ms", `${ms}ms`);
  bar.hidden = !onUndo;
  if (onUndo) bar.classList.add("run");
  toast.hidden = false;
  if (onUndo) undo.focus({ preventScroll: true });
  toastTimer = setTimeout(() => {
    toast.hidden = true;
    if (onDone) onDone();
  }, ms);
}

function hideToast() {
  clearTimeout(toastTimer);
  $("toast").hidden = true;
}

// ------------------------------------------------------------- sheets

let sheetReturn = null;

function openSheet(html, onOpen) {
  const sheet = $("sheet");
  sheetReturn = document.activeElement;
  sheet.innerHTML = `<div class="grab" aria-hidden="true"></div>${html}`;
  $("scrim").hidden = false;
  sheet.hidden = false;
  if (onOpen) onOpen(sheet);
  const close = sheet.querySelector("[data-close]");
  if (close) close.addEventListener("click", () => closeSheet());
  const first = sheet.querySelector("button:not([disabled])");
  if (first) first.focus({ preventScroll: true });
}

function closeSheet(restoreFocus = true) {
  const sheet = $("sheet");
  if (!sheet || sheet.hidden) return;
  sheet.hidden = true;
  sheet.innerHTML = "";
  $("scrim").hidden = true;
  if (restoreFocus && sheetReturn && sheetReturn.isConnected) sheetReturn.focus({ preventScroll: true });
  sheetReturn = null;
}

$("scrim").addEventListener("click", () => closeSheet());
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeSheet();
});

function openReasons() {
  const card = deck.card;
  const reasons = declineReasons(card.presentation);
  if (reasons.length === 0) {
    card.notice = {
      tone: "halted",
      label: "Answer on the computer",
      text: "This phone has no reason it can send for Don't save. Answer it on the computer running OpenAdapt.",
    };
    refreshNotice();
    return;
  }
  openSheet(
    `<h2 id="sheet-title">Why shouldn't it save?</h2>
     <p class="sub">Your answer goes back to the run. Nothing is saved either way.</p>
     ${reasons
       .map(
         (reason) => `<button class="opt${reason === "wrong_record" ? " danger" : ""}" type="button" data-decline="${esc(reason)}">
           <b>${esc(DECLINE_REASON[reason].label)}</b><span>${esc(DECLINE_REASON[reason].then)}</span></button>`,
       )
       .join("")}
     <button class="quiet" type="button" data-close>Back to the card</button>`,
    (sheet) => {
      sheet.querySelectorAll("[data-decline]").forEach((button) => {
        button.disabled = isOffline();
        button.addEventListener("click", () => {
          if (isOffline() || deck.card !== card) return;
          holdForUndo(card, "reject", button.dataset.decline);
        });
      });
    },
  );
}

function detailRows(rows) {
  const body = rows
    .filter(Boolean)
    .map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${value}</dd></div>`)
    .join("");
  return body ? `<dl class="detail-list">${body}</dl>` : "";
}

function openMore() {
  const card = deck.card;
  if (!card) return;
  const others =
    card.kind === "run"
      ? card.actions.filter((action) => action !== card.left && action !== card.right)
      : [];
  const options = others
    .map(
      (action) => `<button class="opt" type="button" data-answer="${esc(action)}">
        <b>${esc(ACTION_WIRE[action].label)}</b><span>${esc(ACTION_WIRE[action].consequence)}</span></button>`,
    )
    .join("");
  const screenOption = card.frameOk
    ? `<button class="opt" type="button" data-screen><b>${
        card.kind === "save" ? "See the full screen" : "See the screen it kept"
      }</b><span>${
        card.kind === "save" ? "The form exactly as OpenAdapt will save it." : "Kept when it stopped. Not live."
      }</span></button>`
    : "";
  const closes =
    card.expiresAt != null ? [["Closes", `${esc(clockTime(card.expiresAt))}. After that, nothing happens.`]] : [];
  const digest = String(card.detail.task_digest || "");
  const details =
    card.kind === "save"
      ? `${others.length ? "<h3>Details</h3>" : ""}${detailRows([
          card.step ? ["Step", esc(card.step)] : null,
          ...closes,
          ["Request", `<code>${esc(digest.slice(7, 19))}</code> · signed`],
          ["Recorded as", "A person on this phone. A paired phone proves a device, not who held it."],
        ])}`
      : `<h3>Why it stopped</h3><p>${esc(card.why)}</p>
         ${recheckBlock(card.presentation.halt)}
         ${ladderBlock(card.presentation.halt)}
         ${evidenceBlock(card.task)}
         <h3>Details</h3>${detailRows([
           ...closes,
           ["Request", `<code>${esc(digest.slice(7, 19))}</code> · signed`],
         ])}
         <p class="sub">${esc(LIMIT_COPY)}</p>`;
  openSheet(
    `<h2 id="sheet-title">${others.length ? "Other answers" : "Details"}</h2>
     <p class="sub">${esc(card.title)}${card.step ? ` · ${esc(card.step)}` : ""}</p>
     ${options}${screenOption}${details}
     <button class="quiet" type="button" data-close>Back to the card</button>`,
    (sheet) => {
      sheet.querySelectorAll("[data-answer]").forEach((button) => {
        button.disabled = isOffline();
        button.addEventListener("click", () => choose(button.dataset.answer));
      });
      const shot = sheet.querySelector("[data-screen]");
      if (shot) shot.addEventListener("click", openScreen);
    },
  );
}

function openScreen() {
  const card = deck.card;
  if (!card || !card.frameOk) return;
  const save = card.kind === "save";
  openSheet(
    `<h2 id="sheet-title">${save ? "Exactly what will be saved" : "The screen it kept"}</h2>
     <p class="sub">${save ? "The form as OpenAdapt will save it." : "Kept when it stopped. Not live, so check the application."} Drag to pan. Tap the picture to fit it.</p>
     <div class="screen-wrap" id="screen-wrap"><img id="frame" src="${esc(card.frameUrl)}"
       alt="${save ? "The filled form, exactly as OpenAdapt will save it." : "The screen OpenAdapt kept when it stopped."}"></div>
     <button class="quiet" type="button" data-close>Back to the card</button>`,
    () => {
      const wrap = $("screen-wrap");
      wrap.addEventListener("click", () => wrap.classList.toggle("fit"));
    },
  );
}

// ------------------------------------------------------------------- routing

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
  // Unreachable is not unpaired: keep the session and say what is wrong.
  if (status === 0) return unreachable();
  if (status >= 500) return notAttended(body);
  if (status !== 200) return startPairing();
  deviceLabel.textContent = body && body.device_label ? body.device_label : "";
  $("status").hidden = false;
  await loadQueue();
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

syncConnection();
route();
