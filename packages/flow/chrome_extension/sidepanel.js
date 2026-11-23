// OpenAdapt Chrome Side-Panel Co-Pilot UI controller.
// Communicates strictly via chrome.runtime.sendMessage -> background.js ->
// Chrome Native Messaging (com.openadapt.extension). Zero remote calls.
'use strict';

(function () {
  const ACTION_META = {
    provide_value: { label: 'Submit Value & Continue', className: 'btn-primary' },
    accept_portal_prefill: { label: 'Accept Portal Prefill', className: 'btn-warning' },
    dismiss_portal_entity: { label: 'Remove Portal Entity', className: 'btn-primary' },
    confirm_modal_action: { label: 'Confirm Modal', className: 'btn-primary' },
    fixed_on_screen: { label: 'I Fixed It on Screen', className: '' },
    escalate_to_owner: { label: 'Escalate', className: '' },
    abort_run: { label: 'Abort', className: 'btn-danger' },
  };

  const HALT_KIND_LABELS = {
    ungrounded_required_field: 'Ungrounded Required Field',
    prefill_value_conflict: 'Prefill Value Conflict',
    prefill_entity_conflict: 'Prefill Entity Conflict',
    ambiguous_option_or_target: 'Ambiguous Option / Target',
    portal_validation_blocked: 'Portal Validation Blocked',
    popup_contract_failed: 'Popup Subroutine Blocked',
    modal_confirmation_required: 'Modal Confirmation Required',
    auth_wall_detected: 'Authentication Required',
    mutation_budget_exceeded: 'Mutation Budget Exceeded',
  };

  const statusPillEl = document.getElementById('status-pill');
  const recordBtnEl = document.getElementById('record-btn');
  const refreshBtnEl = document.getElementById('refresh-btn');
  const progressCounterEl = document.getElementById('progress-counter');
  const progressTrackEl = document.getElementById('progress-track');
  const progressFillEl = document.getElementById('progress-bar-fill');
  const statusLineEl = document.getElementById('status-line');
  const runMetaEl = document.getElementById('run-meta');
  const errorBannerEl = document.getElementById('error-banner');
  const authPauseContainerEl = document.getElementById('auth-pause-container');
  const popupBannerContainerEl = document.getElementById('popup-banner-container');
  const receiptContainerEl = document.getElementById('receipt-container');
  const haltsContainerEl = document.getElementById('halts-container');
  const emptyStateEl = document.getElementById('empty-state');

  let currentState = null;
  let currentAuthPause = null;
  let currentPopupState = null;
  let isRecording = false;
  let lastRenderedStateKey = '__uninitialized__';
  let syncInFlight = false;

  function sendRuntimeMessage(message) {
    return new Promise((resolve) => {
      if (!chrome || !chrome.runtime || typeof chrome.runtime.sendMessage !== 'function') {
        resolve({ ok: false, error: 'runtime_unavailable' });
        return;
      }
      try {
        chrome.runtime.sendMessage(message, (response) => {
          const lastErr =
            chrome.runtime && chrome.runtime.lastError
              ? chrome.runtime.lastError.message
              : null;
          if (lastErr && !response) {
            resolve({ ok: false, error: lastErr });
            return;
          }
          resolve(response || { ok: false, error: 'empty_response' });
        });
      } catch (err) {
        resolve({
          ok: false,
          error: String((err && err.message) || err || 'send_failed'),
        });
      }
    });
  }

  function deriveStatusBadge(state, authPause) {
    if (authPause && authPause.paused) {
      return { text: 'Sign-In Paused', status: 'auth_paused' };
    }
    if (!state) {
      if (isRecording) {
        return { text: 'Recording', status: 'running' };
      }
      return { text: 'Idle', status: 'idle' };
    }
    if (state.outcome === 'verified_done') {
      return { text: 'Verified Done', status: 'verified_done' };
    }
    const hasHalts = Array.isArray(state.halts) && state.halts.length > 0;
    if (
      hasHalts ||
      state.outcome === 'auth_required' ||
      state.outcome === 'validation_blocked'
    ) {
      return { text: 'Action Needed', status: 'action_needed' };
    }
    return { text: 'Running', status: 'running' };
  }

  function computeIdempotencyKey(runId, haltId, action, providedValue, rememberRule) {
    return [
      'copilot',
      String(runId || 'run'),
      String(haltId),
      String(action),
      'value-redacted',
      rememberRule ? '1' : '0',
    ].join(':');
  }

  function createStructuredValueNode(value, emptyPlaceholder) {
    if (value === null || value === undefined || value === '') {
      const span = document.createElement('span');
      span.textContent = emptyPlaceholder;
      return span;
    }
    if (Array.isArray(value)) {
      if (!value.length) {
        const span = document.createElement('span');
        span.textContent = emptyPlaceholder;
        return span;
      }
      const list = document.createElement('div');
      list.className = 'kv-list';
      for (const entry of value) {
        const row = document.createElement('div');
        row.className = 'kv-val';
        row.textContent =
          entry && typeof entry === 'object' ? JSON.stringify(entry) : String(entry);
        list.appendChild(row);
      }
      return list;
    }
    if (typeof value === 'object') {
      const entries = Object.entries(value);
      if (!entries.length) {
        const span = document.createElement('span');
        span.textContent = emptyPlaceholder;
        return span;
      }
      const dl = document.createElement('div');
      dl.className = 'kv-list';
      for (const [k, v] of entries) {
        const row = document.createElement('div');
        row.className = 'kv-row';
        const keyEl = document.createElement('span');
        keyEl.className = 'kv-key';
        keyEl.textContent = k;
        const valEl = document.createElement('span');
        valEl.className = 'kv-val';
        valEl.textContent =
          v !== null && typeof v === 'object' ? JSON.stringify(v) : String(v ?? '');
        row.appendChild(keyEl);
        row.appendChild(valEl);
        dl.appendChild(row);
      }
      return dl;
    }
    const span = document.createElement('span');
    span.textContent = String(value);
    return span;
  }

  function renderConflictBox(halt) {
    const grid = document.createElement('div');
    grid.className = 'conflict-grid';
    grid.setAttribute('data-role', 'conflict-comparison');

    const portalBox = document.createElement('div');
    portalBox.className = 'conflict-box portal-box';
    portalBox.setAttribute('data-role', 'portal-prefilled');
    const portalTitle = document.createElement('div');
    portalTitle.className = 'conflict-box-title';
    portalTitle.textContent = 'Portal Prefilled';
    const portalBody = document.createElement('div');
    portalBody.className = 'conflict-box-body';
    portalBody.appendChild(createStructuredValueNode(halt.portal_value, '(empty)'));
    portalBox.appendChild(portalTitle);
    portalBox.appendChild(portalBody);

    const dossierBox = document.createElement('div');
    dossierBox.className = 'conflict-box dossier-box';
    dossierBox.setAttribute('data-role', 'grounded-dossier');
    const dossierTitle = document.createElement('div');
    dossierTitle.className = 'conflict-box-title';
    dossierTitle.textContent = 'Grounded Dossier';
    const dossierBody = document.createElement('div');
    dossierBody.className = 'conflict-box-body';
    dossierBody.appendChild(
      createStructuredValueNode(halt.dossier_value, '(not in dossier)')
    );
    dossierBox.appendChild(dossierTitle);
    dossierBox.appendChild(dossierBody);

    grid.appendChild(portalBox);
    grid.appendChild(dossierBox);
    return grid;
  }

  function renderOperatorInputBlock(halt) {
    const block = document.createElement('div');
    block.className = 'operator-input-block';
    block.setAttribute('data-role', 'ungrounded-field-controls');

    const options = Array.isArray(halt.available_options) ? halt.available_options : [];
    const inputId = `operator-input-${halt.halt_id}`;
    const labelEl = document.createElement('label');
    labelEl.className = 'input-label';
    labelEl.setAttribute('for', inputId);

    if (options.length > 0) {
      labelEl.textContent = 'Select grounded option';
      const selectEl = document.createElement('select');
      selectEl.id = inputId;
      selectEl.className = 'operator-select';
      selectEl.setAttribute('data-role', 'operator-input');

      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = '-- Select an option --';
      selectEl.appendChild(placeholder);

      for (const opt of options) {
        if (!opt || typeof opt !== 'object') continue;
        const optEl = document.createElement('option');
        optEl.value = String(opt.value ?? '');
        optEl.textContent = String(opt.label ?? opt.value ?? '');
        selectEl.appendChild(optEl);
      }
      block.appendChild(labelEl);
      block.appendChild(selectEl);
    } else {
      labelEl.textContent = 'Operator value';
      const inputEl = document.createElement('input');
      inputEl.type = 'text';
      inputEl.id = inputId;
      inputEl.className = 'operator-text-input';
      inputEl.placeholder = 'Enter grounded value...';
      inputEl.setAttribute('data-role', 'operator-input');
      block.appendChild(labelEl);
      block.appendChild(inputEl);
    }

    const ruleRow = document.createElement('label');
    ruleRow.className = 'candidate-rule-row';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'candidate-rule-checkbox';
    checkbox.setAttribute('data-role', 'remember-as-candidate-rule');
    const ruleText = document.createElement('span');
    ruleText.textContent = 'Save pattern as candidate rule';
    ruleRow.appendChild(checkbox);
    ruleRow.appendChild(ruleText);
    block.appendChild(ruleRow);

    return block;
  }

  function showCardFeedback(cardEl, message, isError) {
    let feedbackEl = cardEl.querySelector('[data-role="card-feedback"]');
    if (!feedbackEl) {
      feedbackEl = document.createElement('div');
      feedbackEl.setAttribute('data-role', 'card-feedback');
      cardEl.appendChild(feedbackEl);
    }
    feedbackEl.className = `card-feedback ${isError ? 'feedback-error' : 'feedback-ok'}`;
    feedbackEl.textContent = message;
  }

  async function submitDecisionForHalt(cardEl, halt, action) {
    if (!currentState || !currentState.binding) {
      showCardFeedback(cardEl, 'No active operator session. Refresh the panel.', true);
      return;
    }
    const binding = currentState.binding;
    let providedValue = null;
    if (action === 'provide_value') {
      const inputEl = cardEl.querySelector('[data-role="operator-input"]');
      const rawVal = inputEl ? String(inputEl.value || '') : '';
      if (!rawVal.trim()) {
        showCardFeedback(
          cardEl,
          'Select or enter a value before submitting.',
          true
        );
        return;
      }
      providedValue = rawVal;
    }

    const ruleCheckbox = cardEl.querySelector(
      '[data-role="remember-as-candidate-rule"]'
    );
    const rememberRule = Boolean(ruleCheckbox && ruleCheckbox.checked);
    const runId = currentState ? currentState.run_id : 'run';

    const decision = {
      halt_id: halt.halt_id,
      action,
      actor_id: binding.operator_id,
      actor_role: 'operator',
      provided_value: providedValue,
      mapped_dossier_key: halt.dossier_key || null,
      remember_as_candidate_rule: rememberRule,
      note: '',
      idempotency_key: computeIdempotencyKey(
        binding.session_nonce + ':' + binding.generation + ':' + runId,
        halt.halt_id,
        action,
        providedValue,
        rememberRule
      ),
    };

    const buttons = Array.from(cardEl.querySelectorAll('button.action-btn'));
    for (const btn of buttons) {
      btn.disabled = true;
    }

    const resp = await sendRuntimeMessage({
      type: 'COPILOT_DECISION',
      decision,
      binding,
    });

    for (const btn of buttons) {
      btn.disabled = false;
    }

    if (!resp || !resp.ok) {
      showCardFeedback(
        cardEl,
        `Submission failed: ${(resp && resp.error) || 'unknown error'}`,
        true
      );
      return;
    }

    cardEl.dataset.decisionAck = 'true';
    cardEl.dataset.acceptedNew = String(Boolean(resp.accepted_new));
    const actionLabel = (ACTION_META[action] && ACTION_META[action].label) || action;
    if (resp.accepted_new) {
      showCardFeedback(cardEl, `Decision recorded: ${actionLabel}.`, false);
    } else {
      showCardFeedback(
        cardEl,
        `Already recorded (${actionLabel}); duplicate ignored.`,
        false
      );
    }
  }

  function renderHaltCard(halt) {
    const card = document.createElement('article');
    card.className = 'halt-card';
    card.setAttribute('data-halt-id', String(halt.halt_id || ''));
    card.setAttribute('data-halt-kind', String(halt.kind || ''));

    const header = document.createElement('div');
    header.className = 'halt-card-header';

    const badgeRow = document.createElement('div');
    badgeRow.className = 'halt-badge-row';

    const kindBadge = document.createElement('span');
    kindBadge.className = 'halt-kind-badge';
    kindBadge.textContent =
      HALT_KIND_LABELS[halt.kind] || String(halt.kind || 'Halt');
    badgeRow.appendChild(kindBadge);

    const contextParts = [halt.screen_id, halt.section_text].filter(Boolean);
    if (contextParts.length > 0) {
      const ctxEl = document.createElement('span');
      ctxEl.className = 'halt-context';
      ctxEl.textContent = contextParts.join(' · ');
      badgeRow.appendChild(ctxEl);
    }

    const titleEl = document.createElement('h2');
    titleEl.className = 'halt-field-label';
    titleEl.textContent =
      halt.field_label || halt.dossier_key || halt.control_id || halt.halt_id;

    header.appendChild(badgeRow);
    header.appendChild(titleEl);
    card.appendChild(header);

    const msgEl = document.createElement('p');
    msgEl.className = 'halt-message';
    msgEl.textContent = String(halt.message || '');
    card.appendChild(msgEl);

    const isConflict =
      halt.kind === 'prefill_entity_conflict' ||
      halt.kind === 'prefill_value_conflict';
    const allowedActions = Array.isArray(halt.allowed_actions)
      ? halt.allowed_actions
      : [];

    if (isConflict) {
      card.appendChild(renderConflictBox(halt));
    }

    // A value conflict can be resolved by entering the correct value, so it
    // needs the input too. Only an entity-row conflict has no single value.
    if (
      halt.kind === 'ungrounded_required_field' ||
      (halt.kind !== 'prefill_entity_conflict' &&
        allowedActions.includes('provide_value'))
    ) {
      card.appendChild(renderOperatorInputBlock(halt));
    }

    const actionsRow = document.createElement('div');
    actionsRow.className = 'halt-actions';

    for (const action of allowedActions) {
      const meta = ACTION_META[action];
      if (!meta) continue;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `action-btn ${meta.className}`.trim();
      btn.setAttribute('data-action', action);
      btn.setAttribute('data-halt-id', String(halt.halt_id || ''));
      btn.textContent = meta.label;
      btn.addEventListener('click', () => {
        void submitDecisionForHalt(card, halt, action);
      });
      actionsRow.appendChild(btn);
    }

    card.appendChild(actionsRow);
    return card;
  }

  function renderReceiptCard(state) {
    receiptContainerEl.textContent = '';
    if (!state || state.outcome !== 'verified_done') {
      receiptContainerEl.hidden = true;
      return;
    }

    const card = document.createElement('div');
    card.id = 'verified-receipt-card';
    card.className = 'receipt-card';
    card.setAttribute('data-receipt-verified', String(Boolean(state.receipt_verified)));

    const header = document.createElement('div');
    header.className = 'receipt-header';
    const title = document.createElement('h2');
    title.className = 'receipt-title';
    title.textContent = 'Verified Receipt';
    const badge = document.createElement('span');
    badge.className = 'receipt-badge';
    badge.textContent = state.receipt_verified
      ? 'Ed25519 Verified'
      : 'Verified Done';
    header.appendChild(title);
    header.appendChild(badge);
    card.appendChild(header);

    const idRow = document.createElement('div');
    idRow.className = 'receipt-id-row';
    const idLabel = document.createElement('span');
    idLabel.className = 'receipt-id-label';
    idLabel.textContent = 'Receipt ID';
    const idCode = document.createElement('code');
    idCode.id = 'receipt-id-value';
    idCode.className = 'receipt-id-code';
    idCode.setAttribute('data-role', 'receipt-id');
    idCode.textContent = state.receipt_id || '—';
    idRow.appendChild(idLabel);
    idRow.appendChild(idCode);
    card.appendChild(idRow);

    const outputs =
      state.outputs && typeof state.outputs === 'object' ? state.outputs : {};
    const outputEntries = Object.entries(outputs);
    const outputsWrap = document.createElement('div');
    outputsWrap.className = 'receipt-outputs';
    outputsWrap.setAttribute('data-role', 'receipt-outputs');
    const outputsHeading = document.createElement('div');
    outputsHeading.className = 'receipt-outputs-heading';
    outputsHeading.textContent = 'Extracted Outputs';
    outputsWrap.appendChild(outputsHeading);

    if (outputEntries.length > 0) {
      outputsWrap.appendChild(createStructuredValueNode(outputs, '—'));
    } else {
      const noneEl = document.createElement('span');
      noneEl.textContent = 'No extracted output fields';
      outputsWrap.appendChild(noneEl);
    }
    card.appendChild(outputsWrap);

    receiptContainerEl.appendChild(card);
    receiptContainerEl.hidden = false;
  }

  function renderRunMeta(state) {
    runMetaEl.textContent = '';
    if (!state) {
      runMetaEl.hidden = true;
      return;
    }
    const runChip = document.createElement('span');
    runChip.className = 'meta-chip';
    runChip.setAttribute('data-role', 'run-id');
    runChip.textContent = `run: ${state.run_id}`;
    runMetaEl.appendChild(runChip);

    const subs = Array.isArray(state.subroutines_completed)
      ? state.subroutines_completed
      : [];
    for (const sub of subs) {
      const chip = document.createElement('span');
      chip.className = 'meta-chip';
      chip.textContent = `subroutine: ${sub}`;
      runMetaEl.appendChild(chip);
    }
    runMetaEl.hidden = false;
  }

  function renderAuthPauseCard(authPause) {
    if (!authPauseContainerEl) return;
    authPauseContainerEl.textContent = '';
    if (!authPause || !authPause.paused) {
      authPauseContainerEl.hidden = true;
      return;
    }

    const card = document.createElement('article');
    card.id = 'auth-pause-card';
    card.className = 'auth-pause-card';
    card.setAttribute('data-auth-kind', String(authPause.kind || 'sign_in'));

    const header = document.createElement('div');
    header.className = 'auth-pause-header';
    const badge = document.createElement('span');
    badge.className = 'auth-pause-badge';
    const hostAcked = Boolean(authPause.hostAcked);
    badge.textContent = hostAcked
      ? authPause.kind === 'mfa'
        ? 'MFA Capture Paused'
        : 'Sign-In Capture Paused'
      : 'Sign-In Notice (Local Only)';
    header.appendChild(badge);
    card.appendChild(header);

    const title = document.createElement('h2');
    title.className = 'auth-pause-title';
    title.textContent = 'Sign in directly in Chrome to continue';
    card.appendChild(title);

    const body = document.createElement('p');
    body.className = 'auth-pause-copy';
    body.textContent = hostAcked
      ? 'Browser evidence and network capture are paused on this tab, and the local OpenAdapt host recorded the sign-in pause. Complete sign-in in Chrome, then click Resume.'
      : 'Browser evidence and network capture are paused locally on this tab (the local OpenAdapt host is not attached). Sign in normally in Chrome, then dismiss or reconnect.';
    card.appendChild(body);

    const actions = document.createElement('div');
    actions.className = 'halt-actions';
    const resumeBtn = document.createElement('button');
    resumeBtn.type = 'button';
    resumeBtn.id = 'auth-resume-btn';
    resumeBtn.className = 'action-btn btn-primary';
    resumeBtn.setAttribute('data-action', 'auth_resume');
    resumeBtn.textContent = hostAcked ? 'I Signed In — Resume' : 'Dismiss Notice';
    resumeBtn.addEventListener('click', async () => {
      resumeBtn.disabled = true;
      const resp = await sendRuntimeMessage({
        type: 'COPILOT_AUTH_RESUME',
        tabId: Number.isInteger(authPause.tabId) ? authPause.tabId : undefined,
      });
      resumeBtn.disabled = false;
      if (!resp || resp.ok !== true) {
        showCardFeedback(
          card,
          `Could not resume: ${(resp && resp.error) || 'native host rejected resume'}`,
          true
        );
        return;
      }
      lastRenderedStateKey = '__force_rerender__';
      void syncState({ forceScroll: false });
    });
    actions.appendChild(resumeBtn);
    card.appendChild(actions);

    authPauseContainerEl.appendChild(card);
    authPauseContainerEl.hidden = false;
  }

  function renderPopupBanner(popupState) {
    if (!popupBannerContainerEl) return;
    popupBannerContainerEl.textContent = '';
    if (!popupState || !Number.isInteger(popupState.childTabId)) {
      popupBannerContainerEl.hidden = true;
      return;
    }

    const card = document.createElement('article');
    card.id = 'popup-banner-card';
    card.className = 'popup-banner-card';
    card.setAttribute('data-child-tab-id', String(popupState.childTabId));
    if (Number.isInteger(popupState.parentTabId)) {
      card.setAttribute('data-parent-tab-id', String(popupState.parentTabId));
    }

    const title = document.createElement('div');
    title.className = 'popup-banner-title';
    title.textContent = 'Estimator / Child Pop-Up Active';
    card.appendChild(title);

    const copy = document.createElement('p');
    copy.className = 'popup-banner-copy';
    const originText = popupState.childOrigin ? ` (${popupState.childOrigin})` : '';
    copy.textContent =
      `Tracking child window${originText}. Closing it returns focus to your parent quoting tab automatically.`;
    card.appendChild(copy);

    const actions = document.createElement('div');
    actions.className = 'halt-actions';
    const returnBtn = document.createElement('button');
    returnBtn.type = 'button';
    returnBtn.id = 'popup-return-btn';
    returnBtn.className = 'action-btn';
    returnBtn.setAttribute('data-action', 'return_to_parent');
    returnBtn.textContent = 'Return to Parent Tab';
    returnBtn.addEventListener('click', async () => {
      returnBtn.disabled = true;
      await sendRuntimeMessage({
        type: 'COPILOT_RETURN_TO_PARENT',
        childTabId: popupState.childTabId,
      });
      returnBtn.disabled = false;
      lastRenderedStateKey = '__force_rerender__';
      void syncState({ forceScroll: false });
    });
    actions.appendChild(returnBtn);
    card.appendChild(actions);

    popupBannerContainerEl.appendChild(card);
    popupBannerContainerEl.hidden = false;
  }

  function renderState(state, extras) {
    const authPause = extras && extras.authPause ? extras.authPause : null;
    const popupState = extras && extras.popupState ? extras.popupState : null;
    const serialized = JSON.stringify({
      state: state || null,
      authPause,
      popupState,
      isRecording,
    });
    if (serialized === lastRenderedStateKey) {
      return;
    }
    lastRenderedStateKey = serialized;
    currentState = state || null;
    currentAuthPause = authPause;
    currentPopupState = popupState;

    const badge = deriveStatusBadge(currentState, currentAuthPause);
    statusPillEl.textContent = badge.text;
    statusPillEl.setAttribute('data-status', badge.status);

    const filled = currentState ? Number(currentState.fields_filled_auto || 0) : 0;
    const total = currentState ? Number(currentState.total_bound_fields || 0) : 0;
    progressCounterEl.textContent = `${filled} / ${total}`;
    progressTrackEl.setAttribute('aria-valuemax', String(Math.max(total, 0)));
    progressTrackEl.setAttribute('aria-valuenow', String(Math.max(filled, 0)));
    const pct = total > 0 ? Math.min(100, Math.round((filled / total) * 100)) : 0;
    progressFillEl.style.width = `${pct}%`;

    statusLineEl.textContent =
      currentAuthPause && currentAuthPause.paused
        ? 'Capture paused on sign-in / MFA screen. Sign in in Chrome, then click Resume.'
        : currentState
          ? String(currentState.status_line || '')
          : 'Waiting for an active local form session.';

    renderRunMeta(currentState);
    renderAuthPauseCard(currentAuthPause);
    renderPopupBanner(currentPopupState);
    renderReceiptCard(currentState);

    haltsContainerEl.textContent = '';
    const halts =
      currentState && Array.isArray(currentState.halts) ? currentState.halts : [];
    for (const halt of halts) {
      haltsContainerEl.appendChild(renderHaltCard(halt));
    }

    const hasAuthPause = Boolean(currentAuthPause && currentAuthPause.paused);
    const hasPopup = Boolean(currentPopupState && Number.isInteger(currentPopupState.childTabId));
    const showEmpty =
      halts.length === 0 &&
      !hasAuthPause &&
      !hasPopup &&
      (!currentState || currentState.outcome !== 'verified_done');
    emptyStateEl.hidden = !showEmpty;
  }

  function setErrorBanner(errorMsg) {
    if (!errorMsg || errorMsg === 'no_recording_dir') {
      errorBannerEl.hidden = true;
      errorBannerEl.textContent = '';
      return;
    }
    errorBannerEl.textContent = `Native channel notice: ${errorMsg}`;
    errorBannerEl.hidden = false;
  }

  async function syncState(options) {
    if (syncInFlight) return;
    syncInFlight = true;
    const forceScroll = Boolean(options && options.forceScroll);
    try {
      const resp = await sendRuntimeMessage({
        type: 'COPILOT_SYNC_STATE',
        forceScroll,
      });
      const extras = {
        authPause: resp && resp.authPause ? resp.authPause : null,
        popupState: resp && resp.popupState ? resp.popupState : null,
      };
      if (!resp || !resp.ok) {
        if (resp && resp.error === 'no_recording_dir') {
          setErrorBanner('');
          renderState(null, extras);
        } else {
          setErrorBanner((resp && resp.error) || 'unavailable');
          renderState(currentState, extras);
        }
        return;
      }
      setErrorBanner('');
      renderState(resp.state || null, extras);
    } finally {
      syncInFlight = false;
    }
  }

  function updateRecordingUi(active) {
    isRecording = Boolean(active);
    if (recordBtnEl) {
      recordBtnEl.textContent = isRecording ? 'Stop Recording' : 'Start Recording';
      recordBtnEl.setAttribute('aria-pressed', isRecording ? 'true' : 'false');
    }
    if (!currentState && !(currentAuthPause && currentAuthPause.paused) && statusPillEl) {
      statusPillEl.textContent = isRecording ? 'Recording' : 'Idle';
      statusPillEl.dataset.status = isRecording ? 'running' : 'idle';
    }
  }

  async function syncRecordingState() {
    const resp = await sendRuntimeMessage({ type: 'GET_RECORDING' });
    if (resp && resp.ok) {
      updateRecordingUi(Boolean(resp.active));
    }
  }

  if (recordBtnEl) {
    recordBtnEl.addEventListener('click', async () => {
      const next = !isRecording;
      const resp = await sendRuntimeMessage({ type: 'SET_RECORDING', active: next });
      if (resp && resp.ok) {
        updateRecordingUi(Boolean(resp.active));
      } else {
        setErrorBanner((resp && resp.error) || 'recording_toggle_failed');
      }
    });
  }

  refreshBtnEl.addEventListener('click', () => {
    lastRenderedStateKey = '__force_rerender__';
    void syncRecordingState();
    void syncState({ forceScroll: true });
  });

  document.addEventListener('DOMContentLoaded', () => {
    void syncRecordingState();
    void syncState({ forceScroll: true });
    setInterval(() => {
      if (document.visibilityState === 'visible') {
        void syncRecordingState();
        void syncState({ forceScroll: false });
      }
    }, 1500);
  });
})();
