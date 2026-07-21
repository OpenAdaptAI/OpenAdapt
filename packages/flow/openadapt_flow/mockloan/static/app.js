'use strict';

/*
 * MockLoan - hash-routed static SPA for a loan-origination console.
 * All data is fake. No external resources are referenced.
 *
 * The consequential write here is authorizing a DISBURSEMENT of funds to a
 * borrower's loan - an irreversible money-movement write, the lending analog
 * of MockMed's clinical "Save Encounter". Drift and fault modes are read from
 * the query string EXACTLY ONCE at load time; navigation only ever changes
 * location.hash, so ?drift=... / ?fault=... persist across every screen.
 */

var DRIFT = new Set(
  (new URLSearchParams(location.search).get('drift') || '')
    .split(',')
    .map(function (s) { return s.trim(); })
    .filter(Boolean)
);

if (DRIFT.has('theme')) { document.body.classList.add('drift-theme'); }
if (DRIFT.has('move')) { document.body.classList.add('drift-move'); }
if (DRIFT.has('font')) { document.body.classList.add('drift-font'); }
if (DRIFT.has('zoom')) { document.body.classList.add('drift-zoom'); }

var LABEL_AUTHORIZE = DRIFT.has('rename')
  ? 'Release Funds' : 'Authorize Disbursement';
var LABEL_OPEN = DRIFT.has('rename') ? 'View' : 'Open';

// Transactional fault-injection hook (flag-gated, exactly like ?drift=).
// When the page is loaded with ?fault=<mode> the Authorize handler routes the
// write through a REAL backend API (openadapt_flow/mockloan/fault_server.py)
// so the fault-model study can judge a replay against DB ground truth rather
// than the on-screen banner. With no ?fault query this is inert and Authorize
// behaves byte-for-byte as before (the normal benchmark is unaffected).
var FAULT = (new URLSearchParams(location.search).get('fault') || '').trim();
// A stable idempotency key for this page load; only sent in ?fault=idempotent.
var FAULT_KEY = 'disb-' + Math.random().toString(36).slice(2);

var LOANS = [
  { id: 'L1001', applicant: 'Jordan Avery', product: 'Personal',
    amount: '18500', purpose: 'Debt consolidation', tier: 'Prime' },
  { id: 'L1002', applicant: 'Casey Monroe', product: 'Auto',
    amount: '32000', purpose: 'Vehicle purchase', tier: 'Near-prime' },
  { id: 'L1003', applicant: 'Riley Chen', product: 'Personal',
    amount: '9500', purpose: 'Home improvement', tier: 'Prime' }
];

var state = {
  currentLoanId: null,
  product: null,
  banner: null,          // { loanId, text }
  disbursements: {}      // loanId -> [{ product, amount, memo }]
};

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

var app = document.getElementById('app');

function loanById(id) {
  for (var i = 0; i < LOANS.length; i++) {
    if (LOANS[i].id === id) { return LOANS[i]; }
  }
  return LOANS[0];
}

function renderLogin() {
  app.innerHTML =
    '<h1>Sign in to MockLoan</h1>' +
    '<div id="login-card">' +
    '<label for="username">Officer ID</label>' +
    '<input id="username" type="text" autocomplete="off">' +
    '<label for="password">Password</label>' +
    '<input id="password" type="password" autocomplete="off">' +
    '<div class="actions"><button id="signin">Sign In</button></div>' +
    '</div>';
  document.getElementById('signin').addEventListener('click', function () {
    location.hash = '#pipeline';
  });
}

function renderPipeline() {
  state.banner = null;
  var rows = LOANS.map(function (l) {
    return '<tr>' +
      '<td>' + esc(l.id) + '</td>' +
      '<td>' + esc(l.applicant) + '</td>' +
      '<td>' + esc(l.product) + '</td>' +
      '<td>$' + esc(l.amount) + '</td>' +
      '<td><button class="open-btn" id="open-' + l.id + '" data-id="' +
      l.id + '">' + LABEL_OPEN + '</button></td>' +
      '</tr>';
  }).join('');
  app.innerHTML =
    '<h1>Funding Pipeline</h1>' +
    '<table id="pipeline-table"><thead><tr>' +
    '<th>Loan</th><th>Applicant</th><th>Product</th><th>Amount</th>' +
    '<th>Action</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table>';
  app.querySelectorAll('.open-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      location.hash = '#loan/' + btn.getAttribute('data-id');
    });
  });
}

function renderLoan(id) {
  var l = loanById(id);
  state.currentLoanId = l.id;

  var bannerHtml = '';
  if (state.banner && state.banner.loanId === l.id) {
    bannerHtml = '<div id="authorized-banner">' +
      esc(state.banner.text) + '</div>';
  }

  var disbs = state.disbursements[l.id] || [];
  var disbHtml;
  if (disbs.length) {
    disbHtml = '<ul id="disbursement-list">' + disbs.map(function (d) {
      return '<li class="disb-item">' + esc(d.product) + ' - $' +
        esc(d.amount) + ' - ' + esc(String(d.memo).slice(0, 60)) + '</li>';
    }).join('') + '</ul>';
  } else {
    disbHtml = '<p id="no-disbursements">No disbursements yet.</p>';
  }

  app.innerHTML =
    '<div id="loan-banner">' + esc(l.applicant) + ' - Loan ' +
    esc(l.id) + ' - ' + esc(l.product) + ' - $' + esc(l.amount) +
    ' - ' + esc(l.tier) + '</div>' +
    bannerHtml +
    '<div class="actions movable">' +
    '<button id="new-disbursement">New Disbursement</button></div>' +
    '<h2>Disbursements</h2>' + disbHtml;

  document.getElementById('new-disbursement')
    .addEventListener('click', function () {
      state.banner = null;
      location.hash = '#disburse';
    });
}

// -- Transactional fault path (only reached when ?fault=<mode> is set) -----

// Apply the LOCAL, optimistic UI update - identical to the non-fault path -
// so the recorded postconditions (authorized banner + loan screen) hold
// unchanged. The backend DB, not this in-page state, is the study's truth.
function commitLocalAndShow(loanId, amount, memo) {
  if (!state.disbursements[loanId]) { state.disbursements[loanId] = []; }
  state.disbursements[loanId].push({
    product: state.product || 'Personal', amount: amount, memo: memo
  });
  state.banner = {
    loanId: loanId,
    text: 'Disbursement authorized - $' + amount + ' - ' +
      String(memo).slice(0, 40)
  };
  location.hash = '#loan/' + loanId;
}

function showSaveError(msg) {
  var el = document.getElementById('save-error');
  if (el) { el.textContent = msg; }
}

// Route the Authorize write through the fault backend and reflect the outcome
// in the UI the way a real app would under each fault. What actually persists
// is decided server-side by ?fault=<mode>; this only shapes what the SCREEN
// says.
function authorizeViaBackend(loanId, amount, memo) {
  var payload = { loan_id: loanId, product: state.product || 'Personal',
    amount: amount, memo: memo };
  if (FAULT === 'idempotent') { payload.key = FAULT_KEY; }
  var url = '/api/disbursement?fault=' + encodeURIComponent(FAULT);

  function post(withTimeout) {
    var opts = { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload) };
    if (withTimeout && typeof AbortController !== 'undefined') {
      var ctrl = new AbortController();
      opts.signal = ctrl.signal;
      setTimeout(function () { ctrl.abort(); }, 1200);
    }
    return fetch(url, opts);
  }

  if (FAULT === 'optimistic') {
    // Optimistic UI: paint success NOW, fire the write, ignore its result.
    commitLocalAndShow(loanId, amount, memo);
    post(false).catch(function () {});
    return;
  }
  if (FAULT === 'timeout') {
    // Commit-then-hang: the client aborts and surfaces an error; no banner.
    post(true)
      .then(function () { commitLocalAndShow(loanId, amount, memo); })
      .catch(function () {
        showSaveError('Authorization timed out - the disbursement may or '
          + 'may not have been booked.');
      });
    return;
  }
  if (FAULT === 'duplicate' || FAULT === 'double' || FAULT === 'idempotent') {
    // Double-submit / double-delivered click: the write is sent twice. Under
    // ?fault=idempotent the payload carries a key the server de-duplicates
    // on; otherwise TWO disbursements land. The banner is shown exactly once.
    post(false).catch(function () {});
    post(false)
      .then(function () { commitLocalAndShow(loanId, amount, memo); })
      .catch(function () {});
    return;
  }
  // ok / partial / session / stale: a single write; banner only on success.
  post(false).then(function (res) {
    if (res.status === 401) { location.hash = '#login'; return; }
    if (res.ok) { commitLocalAndShow(loanId, amount, memo); }
    else { showSaveError('Authorization was rejected by the core.'); }
  }).catch(function () {
    showSaveError('Authorization failed to reach the core.');
  });
}

function renderDisburse() {
  state.banner = null;
  if (!state.currentLoanId) { state.currentLoanId = LOANS[0].id; }
  state.product = null;
  var l = loanById(state.currentLoanId);

  var personalBtn = '<button id="product-personal" class="seg-btn">' +
    'Personal</button>';
  var autoBtn = '<button id="product-auto" class="seg-btn">Auto</button>';

  app.innerHTML =
    '<h1>New Disbursement</h1>' +
    '<label id="loan-label">Loan ' + esc(l.id) + ' - ' +
    esc(l.applicant) + '</label>' +
    '<label id="product-label">Product</label>' +
    '<div class="segmented" id="product-seg">' + personalBtn + autoBtn +
    '</div>' +
    '<label id="amount-label">Amount to disburse</label>' +
    '<div id="amount-field">$' + esc(l.amount) + '</div>' +
    '<label for="memo" id="memo-label">Funding memo</label>' +
    '<textarea id="memo" rows="6"></textarea>' +
    '<div id="save-error"></div>' +
    '<div class="actions movable">' +
    '<button id="authorize">' + LABEL_AUTHORIZE + '</button></div>';

  ['personal', 'auto'].forEach(function (p) {
    document.getElementById('product-' + p)
      .addEventListener('click', function () {
        state.product = p === 'personal' ? 'Personal' : 'Auto';
        document.querySelectorAll('#product-seg .seg-btn')
          .forEach(function (b) { b.classList.remove('selected'); });
        this.classList.add('selected');
      });
  });

  document.getElementById('authorize')
    .addEventListener('click', function () {
      var memo = document.getElementById('memo').value;
      var amount = l.amount;
      // Flag-gated fault path: route the write through the backend so the
      // study can verify the DB effect. Inert unless ?fault=<mode> is set.
      if (FAULT) {
        authorizeViaBackend(state.currentLoanId, amount, memo);
        return;
      }
      var loanId = state.currentLoanId;
      if (!state.disbursements[loanId]) { state.disbursements[loanId] = []; }
      state.disbursements[loanId].push({
        product: state.product || 'Personal', amount: amount, memo: memo
      });
      state.banner = {
        loanId: loanId,
        text: 'Disbursement authorized - $' + amount + ' - ' +
          memo.slice(0, 40)
      };
      location.hash = '#loan/' + loanId;
    });
}

function route() {
  var hash = location.hash;
  if (!hash || hash === '#' || hash === '#login') {
    renderLogin();
  } else if (hash === '#pipeline') {
    renderPipeline();
  } else if (hash.indexOf('#loan/') === 0) {
    renderLoan(hash.slice('#loan/'.length));
  } else if (hash === '#disburse') {
    renderDisburse();
  } else {
    renderLogin();
  }
}

window.addEventListener('hashchange', route);
route();
