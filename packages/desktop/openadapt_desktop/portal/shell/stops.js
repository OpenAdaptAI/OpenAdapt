// The stop-review queue on a paired phone: one stop at a time, answered with
// one tap, or a batch of similar stops answered with one choice.
//
// This page shows what Desktop sends and adds nothing. The question, the
// item's values next to what the app shows, the marked differences and the
// choices with their consequences all come from the card. Desktop rebuilds
// every card before it acts: a stop that changed, expired or is gone, or a
// choice the stop doesn't offer, changes nothing, and this page shows the
// refusal as it came.
//
// Answers are signed on the computer with the decision portal's approver
// key. Two kinds of stop are view-only here: a waiting save is answered on
// the Decisions page (the portal's own save approval), and turning "only ask
// about problems" back on is answered on the computer.
//
// Like the decision shell, this page keeps its pairing in the tab only
// (sessionStorage), sends every request with `cache: "no-store"`, and builds
// its markup from text nodes, never from the card's strings as HTML.

const POLL_MS = 10000;
const MAX_BATCH = 25;
const PHONE_KINDS = new Set(["same_record", "confirm_save", "halted"]);

const store = window.sessionStorage;
const state = {
  cards: [],
  index: 0,
  batch: [],
  armed: null, // a batch choice waiting for its confirming tap
  busy: false,
  keyReady: false,
  shownAt: new Map(),
  shot: null, // { cardId, url }
};

function $(id) {
  return document.getElementById(id);
}

function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = String(options.text);
  if (options.attrs) {
    for (const [name, value] of Object.entries(options.attrs)) node.setAttribute(name, value);
  }
  for (const child of children) if (child) node.appendChild(child);
  return node;
}

function session() {
  return { token: store.getItem("portal_token"), csrf: store.getItem("portal_csrf") };
}

async function api(path, options = {}) {
  const { token, csrf } = session();
  const headers = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.method === "POST") {
    headers["Content-Type"] = "application/json";
    if (csrf) headers["X-OpenAdapt-Portal-CSRF"] = csrf;
  }
  let response;
  try {
    response = await fetch(path, {
      method: options.method || "GET",
      body: options.body,
      headers,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
  } catch (error) {
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

function cardHex(cardId) {
  return String(cardId).replace(/^sha256:/, "");
}

function label(field) {
  const text = String(field).replace(/_/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function say(id, text) {
  const node = $(id);
  node.textContent = text || "";
  node.hidden = !text;
}

function current() {
  return state.cards[Math.min(state.index, Math.max(0, state.cards.length - 1))] || null;
}

function similar(card) {
  if (!card || !card.batch_key || !PHONE_KINDS.has(card.kind)) return [];
  return state.cards.filter((c) => c.batch_key === card.batch_key);
}

// -------------------------------------------------------------- loading

function needsPairing(text) {
  const main = $("main");
  main.replaceChildren(
    el("section", { className: "stops-card" }, [
      el("h1", { className: "stops-q", text: "Pair this phone first" }),
      el("p", { className: "stops-sub", text: text }),
      el("a", { className: "stops-button", text: "Open Decisions", attrs: { href: "/" } }),
    ]),
  );
}

async function load({ quiet = false } = {}) {
  if (!session().token) {
    needsPairing("Scan the pairing code on the computer, then come back to Review stops.");
    return;
  }
  const { status, body } = await api("/api/portal/stops");
  if (status === 401 || status === 202) {
    needsPairing(
      status === 202
        ? "Confirm the code on the computer, then come back."
        : "This phone isn't paired, or its pairing ended.",
    );
    return;
  }
  if (status !== 200 || !body || !body.ok) {
    say("error", (body && (body.message || body.error)) || "The computer didn't answer. Nothing changed.");
    return;
  }
  const keep = current() && current().card_id;
  const cards = Array.isArray(body.cards) ? body.cards : [];
  const same =
    cards.length === state.cards.length &&
    cards.every((c, i) => c.card_id === state.cards[i].card_id) &&
    Boolean(body.portal_key && body.portal_key.ready) === state.keyReady;
  // A poll that found the same stops leaves the page alone, so the person's
  // place on it doesn't jump.
  if (quiet && same) return;
  state.cards = cards;
  state.keyReady = Boolean(body.portal_key && body.portal_key.ready);
  const ids = new Set(state.cards.map((c) => c.card_id));
  state.batch = state.batch.filter((id) => ids.has(id));
  if (state.armed && !state.batch.length) state.armed = null;
  const found = state.cards.findIndex((c) => c.card_id === keep);
  state.index = found >= 0 ? found : Math.min(state.index, Math.max(0, state.cards.length - 1));
  render();
}

// ------------------------------------------------------------ evidence

async function loadShot(card) {
  if (!card.evidence || !card.evidence.screenshot) return;
  if (state.shot && state.shot.cardId === card.card_id) {
    showShot(card);
    return;
  }
  if (state.shot) URL.revokeObjectURL(state.shot.url);
  state.shot = null;
  const { token } = session();
  let response;
  try {
    response = await fetch(`/api/portal/stops/${cardHex(card.card_id)}/screenshot`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
  } catch (error) {
    return;
  }
  if (!response.ok) return;
  const blob = await response.blob();
  state.shot = { cardId: card.card_id, url: URL.createObjectURL(blob) };
  if (current() && current().card_id === card.card_id) showShot(card);
}

function showShot(card) {
  const slot = $("shot");
  if (!slot || !state.shot || state.shot.cardId !== card.card_id) return;
  const caption = card.kind === "approve_save" ? "The form, before saving" : "Where the run stopped";
  slot.replaceChildren(
    el("figure", { className: "stops-shot" }, [
      el("img", { attrs: { src: state.shot.url, alt: caption } }),
      el("figcaption", { text: caption }),
    ]),
  );
}

// One possible match the app listed: field by field, each value marked Same
// or Different from the item. A row Flow read only as text shows its text.
function candidate(row, index, count) {
  const fields = (row && row.fields) || [];
  if (!fields.length) return el("li", { text: (row && row.text) || String(row) });
  const title = count > 1 ? `Possible match ${index + 1}` : "Possible match in the app";
  return el("li", {}, [
    el("h3", { text: title }),
    el(
      "dl",
      {},
      fields.map((f) =>
        el("div", { className: f.same ? "stops-row stops-same" : "stops-row stops-differs" }, [
          el("dt", { text: label(f.field) }),
          el("dd", {}, [
            f.value === "" ? el("em", { text: "Empty" }) : document.createTextNode(f.value),
            el("span", { className: "stops-flag", text: f.same ? "Same" : "Different" }),
          ]),
        ]),
      ),
    ),
  ]);
}

function evidence(card) {
  const ev = card.evidence || {};
  const diffs = new Map((ev.differences || []).map((d) => [d.field, d]));
  const rows = (ev.source || []).map((row) => {
    const diff = diffs.get(row.field);
    const differs = diff ? !diff.same : false;
    const value = el("dd", {}, [
      row.value === "" ? el("em", { text: "Left empty" }) : document.createTextNode(row.value),
    ]);
    if (diff) {
      value.appendChild(
        el("span", { className: "stops-record" }, [
          document.createTextNode(differs ? "App shows: " : "App matches: "),
          el("strong", { text: diff.record === "" ? "empty" : diff.record }),
        ]),
      );
    }
    return el("div", { className: differs ? "stops-row stops-differs" : "stops-row" }, [
      el("dt", {}, [
        document.createTextNode(label(row.field)),
        differs ? el("span", { className: "stops-flag", text: "Differs" }) : null,
      ]),
      value,
    ]);
  });
  const item = el("section", { className: "stops-side", attrs: { "aria-label": "The item" } }, [
    el("h2", { text: "The item" }),
    rows.length ? el("dl", {}, rows) : el("p", { className: "stops-sub", text: "No values to compare." }),
  ]);
  const app = el("section", { className: "stops-side", attrs: { "aria-label": "What the app shows" } }, [
    el("h2", { text: card.kind === "approve_save" ? "The form" : "What the app shows" }),
  ]);
  if ((ev.candidates || []).length) {
    app.appendChild(
      el(
        "ul",
        { className: "stops-candidates", attrs: { "aria-label": "Possible matches the app listed" } },
        ev.candidates.map((row, i) => candidate(row, i, ev.candidates.length)),
      ),
    );
  }
  if (ev.screenshot) {
    app.appendChild(el("div", { attrs: { id: "shot" } }, [el("p", { className: "stops-sub", text: "Loading the screen…" })]));
  }
  if (ev.reason) app.appendChild(el("p", { className: "stops-reason", text: ev.reason }));
  return el("div", { className: "stops-evidence" }, [item, app]);
}

// --------------------------------------------------------------- answers

function shownMs(ids) {
  const now = Date.now();
  const out = {};
  for (const id of ids) out[id] = now - (state.shownAt.get(id) || now);
  return out;
}

async function answer(choiceId, ids) {
  if (state.busy || !ids.length) return;
  state.busy = true;
  say("error", "");
  say("notice", "");
  render();
  const single = ids.length === 1;
  const path = single
    ? `/api/portal/stops/${cardHex(ids[0])}/answer`
    : "/api/portal/stops/answer";
  const times = shownMs(ids);
  const payload = single
    ? { choice: choiceId, shown_ms: times[ids[0]] }
    : { card_ids: ids, choice: choiceId, shown_ms: times };
  const { status, body } = await api(path, { method: "POST", body: JSON.stringify(payload) });
  state.busy = false;
  state.armed = null;
  if (status === 0) {
    say("error", "The answer may not have reached the computer. Check the queue before answering again.");
  } else if (!body || !body.ok) {
    say("error", (body && (body.error || body.message)) || "Nothing changed.");
  } else if (choiceId === "leave") {
    state.index += 1;
  } else {
    say("notice", single ? "Answered." : `Answered ${body.changed ?? ids.length} stops.`);
    state.batch = [];
  }
  await load();
}

function choices(card) {
  if (card.kind === "approve_save") {
    return el("div", { className: "stops-viewonly" }, [
      el("p", { text: "Answer this save on the Decisions page. It shows the exact form and signs your answer." }),
      el("a", { className: "stops-button", text: "Open Decisions", attrs: { href: "/" } }),
    ]);
  }
  if (!PHONE_KINDS.has(card.kind)) {
    return el("div", { className: "stops-viewonly" }, [
      el("p", { text: "Answer this on the computer." }),
    ]);
  }
  return el(
    "div",
    { className: "stops-choices" },
    card.choices.map((choice, i) => {
      const button = el("button", { className: i === 0 ? "stops-choice primary" : "stops-choice", attrs: { type: "button" } }, [
        el("span", { className: "stops-choice-label", text: choice.label }),
        el("span", { className: "stops-choice-then", text: choice.consequence }),
      ]);
      button.disabled = state.busy || !state.keyReady;
      button.addEventListener("click", () => answer(choice.id, [card.card_id]));
      return button;
    }),
  );
}

function batchBlock(card) {
  const group = similar(card);
  if (group.length < 2) return null;
  const section = el("section", { className: "stops-batch", attrs: { "aria-label": "Similar stops" } }, [
    el("h2", { text: `${group.length} similar stops${state.batch.length ? ` · ${state.batch.length} picked` : ""}` }),
    el("p", {
      className: "stops-sub",
      text: "Check each one before you pick it. A batch gets one answer, and each stop is still checked on its own.",
    }),
  ]);
  const list = el("ul", {});
  for (const other of group.slice(0, MAX_BATCH)) {
    const box = el("input", { attrs: { type: "checkbox" } });
    box.checked = state.batch.includes(other.card_id);
    box.addEventListener("change", () => {
      state.armed = null;
      state.batch = box.checked
        ? [...state.batch, other.card_id]
        : state.batch.filter((id) => id !== other.card_id);
      if (!state.shownAt.has(other.card_id)) state.shownAt.set(other.card_id, Date.now());
      render();
    });
    const values = (other.evidence.source || []).slice(0, 3).map((row) => row.value).join(", ");
    list.appendChild(
      el("li", {}, [
        el("label", { className: "stops-pick" }, [
          box,
          el("span", { text: `${other.label || other.source.id}: ${values}` }),
        ]),
      ]),
    );
  }
  section.appendChild(list);
  if (state.batch.length > 1) {
    const actions = el("div", { className: "stops-choices" });
    for (const choice of card.choices.filter((c) => c.id !== "leave")) {
      const armed = state.armed === choice.id;
      const button = el("button", { className: armed ? "stops-choice primary" : "stops-choice", attrs: { type: "button" } }, [
        el("span", {
          className: "stops-choice-label",
          text: armed ? `Tap again: ${choice.label} for ${state.batch.length} stops` : `${choice.label} (${state.batch.length})`,
        }),
        el("span", { className: "stops-choice-then", text: choice.consequence }),
      ]);
      button.disabled = state.busy || !state.keyReady;
      button.addEventListener("click", () => {
        if (state.armed === choice.id) {
          answer(choice.id, [...state.batch]);
        } else {
          state.armed = choice.id;
          render();
        }
      });
      actions.appendChild(button);
    }
    section.appendChild(actions);
  }
  return section;
}

// ---------------------------------------------------------------- render

function render() {
  const main = $("main");
  const card = current();
  if (!card) {
    main.replaceChildren(
      el("section", { className: "stops-card" }, [
        el("h1", { className: "stops-q", text: "Nothing is waiting for you" }),
        el("p", { className: "stops-sub", text: "New stops show up here on their own." }),
      ]),
    );
    return;
  }
  if (!state.shownAt.has(card.card_id)) state.shownAt.set(card.card_id, Date.now());
  const where = [`${state.index + 1} of ${state.cards.length}`, card.task_name, card.label]
    .filter(Boolean)
    .join(" · ");
  const nav = el("nav", { className: "stops-nav" });
  const prev = el("button", { className: "stops-nav-btn", text: "Previous", attrs: { type: "button" } });
  prev.disabled = state.index <= 0;
  prev.addEventListener("click", () => {
    state.index = Math.max(0, state.index - 1);
    render();
  });
  const next = el("button", { className: "stops-nav-btn", text: "Next", attrs: { type: "button" } });
  next.disabled = state.index >= state.cards.length - 1;
  next.addEventListener("click", () => {
    state.index = Math.min(state.cards.length - 1, state.index + 1);
    render();
  });
  nav.append(prev, next);
  main.replaceChildren(
    el("article", { className: "stops-card", attrs: { "aria-labelledby": "stops-q" } }, [
      el("p", { className: "stops-where", text: where }),
      el("h1", { className: "stops-q", text: card.question, attrs: { id: "stops-q" } }),
      el("p", { className: "stops-sub", text: card.summary }),
      state.keyReady || !PHONE_KINDS.has(card.kind)
        ? null
        : el("p", {
            className: "stops-warn",
            text: "Answers from this phone need the decision portal's approver key. Set it up on the computer. Until then, nothing here changes.",
          }),
      evidence(card),
      choices(card),
      batchBlock(card),
    ]),
    nav,
  );
  loadShot(card);
}

load();
setInterval(() => {
  if (!state.busy && !state.armed) load({ quiet: true });
}, POLL_MS);
