'use strict';

/*
 * MockMed Widgets lab — one interaction primitive per panel.
 *
 * ?panel=<name> renders a single panel (select, checks, date, modal,
 * typeahead, table, kbd, newtab, upload); omitting it renders all of them.
 * ?presort=desc reverses the table's initial row order (reorder drift).
 *
 * Every panel reports its outcome by writing a line into #status, so a
 * compiled workflow gets an OCR-checkable postcondition for each action.
 */

var QS = new URLSearchParams(location.search);
var PANEL = QS.get('panel') || 'all';
var PRESORT_DESC = QS.get('presort') === 'desc';

var app = document.getElementById('app');
var modalRoot = document.getElementById('modal-root');

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function setStatus(text) {
  document.getElementById('status').textContent = text;
}

function want(name) { return PANEL === 'all' || PANEL === name; }

var html = '<h1>Widgets</h1><div id="status">Ready and waiting.</div>';

if (want('select')) {
  html +=
    '<div class="panel" id="panel-select">' +
    '<label for="pet">Species</label>' +
    '<select id="pet">' +
    '<option value="">Choose a species</option>' +
    '<option>Cat</option><option>Dog</option><option>Ferret</option>' +
    '</select></div>';
}

if (want('checks')) {
  html +=
    '<div class="panel" id="panel-checks">' +
    '<label>Intake flags</label>' +
    '<div class="check-row"><input type="checkbox" id="consent">' +
    ' <label for="consent" style="display:inline">Consent recorded</label></div>' +
    '<div class="check-row"><input type="radio" name="prio" id="prio-routine" value="Routine">' +
    ' <label for="prio-routine" style="display:inline">Routine</label>' +
    ' <input type="radio" name="prio" id="prio-urgent" value="Urgent">' +
    ' <label for="prio-urgent" style="display:inline">Urgent</label></div>' +
    '</div>';
}

if (want('date')) {
  html +=
    '<div class="panel" id="panel-date">' +
    '<label for="when">Follow-up date</label>' +
    '<input type="date" id="when"></div>';
}

if (want('modal')) {
  html +=
    '<div class="panel" id="panel-modal">' +
    '<label>Discharge survey</label>' +
    '<div class="actions"><button id="open-survey">Start survey</button></div>' +
    '</div>';
}

if (want('typeahead')) {
  html +=
    '<div class="panel" id="panel-typeahead">' +
    '<label for="q">Contact lookup</label>' +
    '<input type="text" id="q" autocomplete="off" placeholder="Type a name">' +
    '<div id="suggestions"></div></div>';
}

if (want('table')) {
  html +=
    '<div class="panel" id="panel-table">' +
    '<label>Order queue</label>' +
    '<div class="actions" style="margin-top:6px">' +
    '<button id="sort-name">Sort by name</button></div>' +
    '<table id="widget-table"><thead><tr>' +
    '<th>Order</th><th>Ward</th><th>Action</th>' +
    '</tr></thead><tbody id="table-body"></tbody></table>' +
    '<div class="pager">' +
    '<button id="prev-page">Previous</button>' +
    '<button id="next-page">Next</button>' +
    '<span id="page-label"></span></div></div>';
}

if (want('kbd')) {
  html +=
    '<div class="panel" id="panel-kbd">' +
    '<label for="kb-name">Requester name</label>' +
    '<input type="text" id="kb-name" autocomplete="off">' +
    '<label for="kb-ward">Ward (press Enter to submit)</label>' +
    '<input type="text" id="kb-ward" autocomplete="off"></div>';
}

if (want('newtab')) {
  html +=
    '<div class="panel" id="panel-newtab">' +
    '<label>Reports</label>' +
    '<p><a id="report-link" href="index.html" target="_blank">' +
    'Open quarterly report in a new tab</a></p></div>';
}

if (want('upload')) {
  html +=
    '<div class="panel" id="panel-upload">' +
    '<label for="attach">Attach document</label>' +
    '<input type="file" id="attach"></div>';
}

app.innerHTML = html;

// -- select -----------------------------------------------------------------
if (want('select')) {
  document.getElementById('pet').addEventListener('change', function () {
    setStatus('Species set to ' + this.value + '.');
  });
}

// -- checkbox / radio ---------------------------------------------------------
if (want('checks')) {
  function checksStatus() {
    var consent = document.getElementById('consent').checked ? 'yes' : 'no';
    var prio = document.querySelector('input[name="prio"]:checked');
    setStatus('Consent ' + consent + ', priority ' +
      (prio ? prio.value : 'unset') + '.');
  }
  ['consent', 'prio-routine', 'prio-urgent'].forEach(function (id) {
    document.getElementById(id).addEventListener('change', checksStatus);
  });
}

// -- date ---------------------------------------------------------------------
if (want('date')) {
  document.getElementById('when').addEventListener('change', function () {
    if (this.value) { setStatus('Follow-up scheduled for ' + this.value + '.'); }
  });
}

// -- modal ----------------------------------------------------------------------
if (want('modal')) {
  document.getElementById('open-survey').addEventListener('click', function () {
    modalRoot.innerHTML =
      '<div id="modal-overlay"><div id="survey-modal">' +
      '<h2>Discharge survey</h2>' +
      '<p>Was the visit summary explained to the patient?</p>' +
      '<div class="actions"><button id="confirm-survey">Yes, confirmed</button>' +
      '</div></div></div>';
    document.getElementById('confirm-survey')
      .addEventListener('click', function () {
        modalRoot.innerHTML = '';
        setStatus('Survey response recorded.');
      });
  });
}

// -- typeahead -------------------------------------------------------------------
if (want('typeahead')) {
  var CONTACTS = ['Alice Anders', 'Alan Turingtest', 'Bob Baker',
                  'Bella Bracket', 'Carol Chen'];
  var q = document.getElementById('q');
  var box = document.getElementById('suggestions');
  q.addEventListener('input', function () {
    var v = q.value.trim().toLowerCase();
    box.innerHTML = '';
    if (!v) { return; }
    CONTACTS.filter(function (c) {
      return c.toLowerCase().indexOf(v) === 0;
    }).slice(0, 3).forEach(function (c) {
      var b = document.createElement('button');
      b.textContent = c;
      b.className = 'suggestion';
      b.addEventListener('click', function () {
        box.innerHTML = '';
        q.value = c;
        setStatus('Contact chosen: ' + c + '.');
      });
      box.appendChild(b);
    });
  });
}

// -- table: pagination + sort ------------------------------------------------------
if (want('table')) {
  var ORDERS = [
    { name: 'Amoxicillin refill', ward: 'North' },
    { name: 'Basic metabolic panel', ward: 'South' },
    { name: 'Chest radiograph', ward: 'East' },
    { name: 'Dermatology consult', ward: 'West' },
    { name: 'Echocardiogram', ward: 'North' },
    { name: 'Ferritin level', ward: 'South' }
  ];
  var PAGE_SIZE = 3;
  var page = 0;
  var ascending = !PRESORT_DESC;

  function renderTable() {
    var rows = ORDERS.slice().sort(function (a, b) {
      return ascending
        ? a.name.localeCompare(b.name)
        : b.name.localeCompare(a.name);
    });
    var start = page * PAGE_SIZE;
    var body = rows.slice(start, start + PAGE_SIZE).map(function (o) {
      return '<tr><td>' + esc(o.name) + '</td><td>' + esc(o.ward) +
        '</td><td><button class="pick-btn" data-name="' + esc(o.name) +
        '">Pick</button></td></tr>';
    }).join('');
    document.getElementById('table-body').innerHTML = body;
    document.getElementById('page-label').textContent =
      ' Page ' + (page + 1) + ' of ' + Math.ceil(rows.length / PAGE_SIZE);
    Array.prototype.forEach.call(
      document.querySelectorAll('.pick-btn'),
      function (btn) {
        btn.addEventListener('click', function () {
          setStatus('Order picked: ' + btn.getAttribute('data-name') + '.');
        });
      }
    );
  }

  document.getElementById('next-page').addEventListener('click', function () {
    if ((page + 1) * PAGE_SIZE < ORDERS.length) { page += 1; renderTable(); }
  });
  document.getElementById('prev-page').addEventListener('click', function () {
    if (page > 0) { page -= 1; renderTable(); }
  });
  document.getElementById('sort-name').addEventListener('click', function () {
    ascending = !ascending;
    page = 0;
    renderTable();
  });
  renderTable();
}

// -- keyboard-only ------------------------------------------------------------------
if (want('kbd')) {
  document.getElementById('kb-ward').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      setStatus('Request submitted for ' +
        document.getElementById('kb-name').value + ' on ward ' +
        this.value + '.');
    }
  });
}

// -- upload (no handler needed: the native chooser is the experiment) ---------------
