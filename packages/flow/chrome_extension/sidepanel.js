// OpenAdapt Chrome Side-Panel Co-Pilot UI controller.
// Communicates strictly via chrome.runtime.sendMessage -> background.js ->
// Chrome Native Messaging (com.openadapt.extension). Zero remote calls.
'use strict';

(function () {
  const ONBOARDING_DISMISSED_KEY = 'openadapt_copilot_onboarding_dismissed';

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
    ungrounded_required_field: 'Needs your pick',
    prefill_value_conflict: 'Value mismatch',
    prefill_entity_conflict: 'Extra portal row',
    ambiguous_option_or_target: 'Choose matching option',
    portal_validation_blocked: 'Portal validation error',
    popup_contract_failed: 'Pop-up step paused',
    modal_confirmation_required: 'Confirm dialog',
    auth_wall_detected: 'Sign-in required',
    mutation_budget_exceeded: 'Safety limit reached',
  };

  const statusPillEl = document.getElementById('status-pill');
  const recordBtnEl = document.getElementById('record-btn');
  const refreshBtnEl = document.getElementById('refresh-btn');
  const progressPanelEl = document.getElementById('progress-panel');
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

  const onboardingCardEl = document.getElementById('onboarding-card');
  const dismissOnboardingBtnEl = document.getElementById('dismiss-onboarding-btn');
  const stepConnectEl = document.getElementById('step-connect');
  const stepConnectBadgeEl = document.getElementById('step-connect-badge');
  const connectionStatusBadgeEl = document.getElementById('connection-status-badge');
  const tabDesktopEl = document.getElementById('tab-desktop');
  const tabCliEl = document.getElementById('tab-cli');
  const panelDesktopEl = document.getElementById('panel-desktop');
  const panelCliEl = document.getElementById('panel-cli');
  const cliInstallCmdEl = document.getElementById('cli-install-cmd');
  const copyCliBtnEl = document.getElementById('copy-cli-btn');
  const checkConnectionBtnEl = document.getElementById('check-connection-btn');
  const connectionFeedbackEl = document.getElementById('connection-feedback');
  const stepInspectEl = document.getElementById('step-inspect');
  const stepInspectBadgeEl = document.getElementById('step-inspect-badge');
  const stepInspectPillEl = document.getElementById('step-inspect-pill');

  const inspectCardEl = document.getElementById('inspect-card');
  const activeTabHostEl = document.getElementById('active-tab-host');
  const inspectPageBtnEl = document.getElementById('inspect-page-btn');
  const inspectResultEl = document.getElementById('inspect-result');

  let currentState = null;
  let currentAuthPause = null;
  let currentPopupState = null;
  let isRecording = false;
  let hostConnected = false;
  let sawDisconnectedSession = false;
  let onboardingDismissed = false;
  let hasInspectedPage = false;
  let lastRenderedStateKey = '__uninitialized__';
  let syncInFlight = false;

  function isHostDisconnectedError(errorMsg) {
    if (!errorMsg) return false;
    const text = String(errorMsg).toLowerCase();
    return (
      text.includes('native messaging host not found') ||
      text.includes('native_host_unavailable') ||
      text.includes('native_host_disconnected') ||
      text.includes('native_host_timeout') ||
      text.includes('native_post_failed') ||
      text.includes('copilot_sync_failed') ||
      text === 'unavailable' ||
      text === 'runtime_unavailable'
    );
  }

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

  function loadOnboardingPreference() {
    return new Promise((resolve) => {
      try {
        if (
          chrome &&
          chrome.storage &&
          chrome.storage.local &&
          typeof chrome.storage.local.get === 'function'
        ) {
          chrome.storage.local.get([ONBOARDING_DISMISSED_KEY], (data) => {
            onboardingDismissed = Boolean(data && data[ONBOARDING_DISMISSED_KEY]);
            resolve(onboardingDismissed);
          });
          return;
        }
      } catch (e) {}
      resolve(false);
    });
  }

  function saveOnboardingDismissed(dismissed) {
    onboardingDismissed = Boolean(dismissed);
    try {
      if (
        chrome &&
        chrome.storage &&
        chrome.storage.local &&
        typeof chrome.storage.local.set === 'function'
      ) {
        chrome.storage.local.set({ [ONBOARDING_DISMISSED_KEY]: onboardingDismissed });
      }
    } catch (e) {}
  }

  function deriveStatusBadge(state, authPause) {
    if (authPause && authPause.paused) {
      return { text: 'Sign-In Paused', status: 'auth_paused' };
    }
    if (!state) {
      if (isRecording) {
        return { text: 'Recording', status: 'running' };
      }
      if (!hostConnected) {
        return { text: 'Standalone', status: 'standalone' };
      }
      return { text: 'Idle', status: 'idle' };
    }
    if (state.outcome === 'verified_done') {
      return { text: 'Verified Done', status: 'verified_done' };
    }
    // Submitted but not verified: a person must look before anyone relies on it.
    if (state.outcome === 'readback_refuted') {
      return { text: 'Read-back Mismatch', status: 'action_needed' };
    }
    if (state.outcome === 'readback_incomplete') {
      return { text: 'Submitted, Not Fully Checked', status: 'action_needed' };
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
    portalTitle.textContent = 'On this page';
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
    dossierTitle.textContent = 'In your record';
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

  function renderOperatorInputBlock(halt, cardEl) {
    const block = document.createElement('div');
    block.className = 'operator-input-block';
    block.setAttribute('data-role', 'ungrounded-field-controls');

    const options = Array.isArray(halt.available_options) ? halt.available_options : [];
    const inputId = `operator-input-${halt.halt_id}`;
    const labelEl = document.createElement('label');
    labelEl.className = 'input-label';
    labelEl.setAttribute('for', inputId);

    if (options.length > 0) {
      labelEl.textContent = 'Pick an option';
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
      labelEl.textContent = 'Enter value';
      const inputEl = document.createElement('input');
      inputEl.type = 'text';
      inputEl.id = inputId;
      inputEl.className = 'operator-text-input';
      inputEl.placeholder = 'Type a value...';
      inputEl.setAttribute('data-role', 'operator-input');
      inputEl.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' && cardEl) {
          ev.preventDefault();
          void submitDecisionForHalt(cardEl, halt, 'provide_value');
        }
      });
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
    ruleText.textContent = 'Remember this choice for next time';
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
      showCardFeedback(cardEl, 'No active session. Click Refresh to reconnect.', true);
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
          'Select or enter a value before continuing.',
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
        `Could not save choice: ${(resp && resp.error) || 'unknown error'}`,
        true
      );
      return;
    }

    cardEl.dataset.decisionAck = 'true';
    cardEl.dataset.acceptedNew = String(Boolean(resp.accepted_new));
    const actionLabel = (ACTION_META[action] && ACTION_META[action].label) || action;
    if (resp.accepted_new) {
      showCardFeedback(cardEl, `Saved: ${actionLabel}.`, false);
    } else {
      showCardFeedback(
        cardEl,
        `Already saved (${actionLabel}); duplicate ignored.`,
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
      HALT_KIND_LABELS[halt.kind] || String(halt.kind || 'Needs review');
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
      card.appendChild(renderOperatorInputBlock(halt, card));
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
      ? 'Signed Receipt'
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
    outputsHeading.textContent = 'Captured Summary';
    outputsWrap.appendChild(outputsHeading);

    if (outputEntries.length > 0) {
      outputsWrap.appendChild(createStructuredValueNode(outputs, '—'));
    } else {
      const noneEl = document.createElement('span');
      noneEl.textContent = 'No extracted summary fields';
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
      chip.textContent = `step: ${sub}`;
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
        ? '2FA Capture Paused'
        : 'Sign-In Capture Paused'
      : 'Sign-In Protected (Local)';
    header.appendChild(badge);
    card.appendChild(header);

    const title = document.createElement('h2');
    title.className = 'auth-pause-title';
    title.textContent = 'Sign in directly in Chrome to continue';
    card.appendChild(title);

    const body = document.createElement('p');
    body.className = 'auth-pause-copy';
    body.textContent = hostAcked
      ? 'Capture is paused on this tab while you sign in. Passwords and one-time codes are never read. Click Resume once you are signed in.'
      : 'Capture is paused locally on this tab while you sign in. Passwords and one-time codes are never read.';
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
          `Could not resume: ${(resp && resp.error) || 'local engine rejected resume'}`,
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
    title.textContent = 'Pop-Up Window Active';
    card.appendChild(title);

    const copy = document.createElement('p');
    copy.className = 'popup-banner-copy';
    const originText = popupState.childOrigin ? ` (${popupState.childOrigin})` : '';
    copy.textContent =
      `Tracking child window${originText}. Closing it returns focus to your main tab automatically.`;
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

  function updateOnboardingSteps() {
    if (stepConnectEl) {
      stepConnectEl.setAttribute('data-done', hostConnected ? 'true' : 'false');
    }
    if (stepConnectBadgeEl) {
      stepConnectBadgeEl.textContent = hostConnected ? '\u2713' : '2';
    }
    if (connectionStatusBadgeEl) {
      connectionStatusBadgeEl.setAttribute(
        'data-connected',
        hostConnected ? 'true' : 'false'
      );
      connectionStatusBadgeEl.textContent = hostConnected
        ? '\u2713 Connected'
        : 'Not connected';
    }
    if (checkConnectionBtnEl) {
      checkConnectionBtnEl.setAttribute(
        'data-state',
        hostConnected ? 'connected' : 'idle'
      );
      checkConnectionBtnEl.textContent = hostConnected
        ? 'Connected \u2713'
        : 'Check connection';
    }
    if (dismissOnboardingBtnEl) {
      dismissOnboardingBtnEl.hidden = !hostConnected;
    }
    if (stepInspectEl) {
      stepInspectEl.setAttribute('data-done', hasInspectedPage ? 'true' : 'false');
    }
    if (stepInspectBadgeEl) {
      stepInspectBadgeEl.textContent = hasInspectedPage ? '\u2713' : '3';
    }
    if (stepInspectPillEl) {
      stepInspectPillEl.textContent = hasInspectedPage ? 'Done' : 'Works now';
      stepInspectPillEl.className = hasInspectedPage
        ? 'step-pill step-pill-done'
        : 'step-pill';
    }
  }

  function renderState(state, extras) {
    const authPause = extras && extras.authPause ? extras.authPause : null;
    const popupState = extras && extras.popupState ? extras.popupState : null;
    const serialized = JSON.stringify({
      state: state || null,
      authPause,
      popupState,
      isRecording,
      hostConnected,
      sawDisconnectedSession,
      onboardingDismissed,
      hasInspectedPage,
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
        ? 'Capture paused on sign-in screen. Sign in in Chrome, then click Resume.'
        : currentState
          ? String(currentState.status_line || '')
          : 'Ready for a local form session. Everything stays staged before Submit.';

    // Hide progress bar when in pure standalone mode (no local host yet)
    if (progressPanelEl) {
      progressPanelEl.hidden = !hostConnected && !currentState;
    }

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
    const hasPopup = Boolean(
      currentPopupState && Number.isInteger(currentPopupState.childTabId)
    );
    const hasActiveWorkflow =
      halts.length > 0 ||
      hasAuthPause ||
      hasPopup ||
      Boolean(currentState && currentState.outcome === 'verified_done');

    updateOnboardingSteps();

    if (onboardingCardEl) {
      const showOnboarding =
        !hasActiveWorkflow &&
        (!hostConnected || (sawDisconnectedSession && !onboardingDismissed));
      onboardingCardEl.hidden = !showOnboarding;
    }

    if (inspectCardEl) {
      inspectCardEl.hidden = hasActiveWorkflow;
    }

    const showEmpty = hostConnected && !hasActiveWorkflow;
    emptyStateEl.hidden = !showEmpty;
  }

  function setErrorBanner(errorMsg) {
    if (!errorMsg || errorMsg === 'no_recording_dir' || isHostDisconnectedError(errorMsg)) {
      errorBannerEl.hidden = true;
      errorBannerEl.textContent = '';
      return;
    }
    errorBannerEl.textContent = `Notice: ${errorMsg}`;
    errorBannerEl.hidden = false;
  }

  async function syncActiveTab() {
    if (!activeTabHostEl) return;
    const resp = await sendRuntimeMessage({ type: 'COPILOT_GET_ACTIVE_TAB' });
    if (resp && resp.ok && resp.hostname) {
      activeTabHostEl.textContent = resp.hostname;
    } else {
      activeTabHostEl.textContent = 'No web page open';
    }
  }

  function renderInspectionResult(inspection) {
    if (!inspectResultEl) return;
    inspectResultEl.textContent = '';
    inspectResultEl.hidden = false;

    const fieldCount = Number(inspection.fieldCount || 0);
    const submitCount = Number(inspection.submitCount || 0);
    const secretCount = Number(inspection.secretCount || 0);
    const labels = Array.isArray(inspection.sampleLabels)
      ? inspection.sampleLabels
      : [];

    const summary = document.createElement('div');
    summary.className = 'inspect-summary-line';
    if (fieldCount > 0) {
      inspectResultEl.setAttribute('data-tone', 'ready');
      const fieldWord = fieldCount === 1 ? 'form field' : 'form fields';
      const submitWord = submitCount === 1 ? 'Submit button' : 'Submit buttons';
      summary.textContent =
        `${fieldCount} ${fieldWord} \u00b7 ${submitCount} ${submitWord} detected \u00b7 Ready for Stage-Only`;
    } else {
      inspectResultEl.removeAttribute('data-tone');
      summary.textContent =
        'No form fields detected on this page. Open a web form or try the sample portal.';
    }
    inspectResultEl.appendChild(summary);

    if (secretCount > 0) {
      const note = document.createElement('div');
      note.className = 'inspect-subnote';
      const word = secretCount === 1 ? 'sign-in / secret field' : 'sign-in / secret fields';
      note.textContent = `${secretCount} ${word} automatically protected (never read).`;
      inspectResultEl.appendChild(note);
    }

    if (labels.length > 0) {
      const chips = document.createElement('div');
      chips.className = 'inspect-chips';
      for (const lbl of labels) {
        const chip = document.createElement('span');
        chip.className = 'inspect-chip';
        chip.textContent = String(lbl);
        chips.appendChild(chip);
      }
      inspectResultEl.appendChild(chips);
    }
  }

  function renderInspectionError(message) {
    if (!inspectResultEl) return;
    inspectResultEl.textContent = '';
    inspectResultEl.removeAttribute('data-tone');
    inspectResultEl.hidden = false;
    const line = document.createElement('div');
    line.className = 'inspect-summary-line';
    line.textContent = message;
    inspectResultEl.appendChild(line);
  }

  async function inspectCurrentPage() {
    if (!inspectPageBtnEl) return;
    inspectPageBtnEl.disabled = true;
    const originalText = inspectPageBtnEl.textContent;
    inspectPageBtnEl.textContent = 'Inspecting...';
    try {
      const resp = await sendRuntimeMessage({ type: 'COPILOT_INSPECT_PAGE' });
      if (resp && resp.ok && resp.inspection) {
        if (resp.inspection.hostname && activeTabHostEl) {
          activeTabHostEl.textContent = resp.inspection.hostname;
        }
        hasInspectedPage = true;
        renderInspectionResult(resp.inspection);
        updateOnboardingSteps();
      } else if (resp && resp.error === 'no_web_page') {
        renderInspectionError(
          'Open any http/https web form tab (or try the sample portal), then click Inspect this page.'
        );
      } else {
        renderInspectionError(
          'Reload this tab once so Co-Pilot can inspect its form structure locally.'
        );
      }
    } finally {
      inspectPageBtnEl.disabled = false;
      inspectPageBtnEl.textContent = originalText;
    }
  }

  async function syncState(options) {
    if (syncInFlight) return { connected: hostConnected };
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
          hostConnected = true;
          setErrorBanner('');
          renderState(null, extras);
          return { connected: true };
        }
        const errText = (resp && resp.error) || 'unavailable';
        if (isHostDisconnectedError(errText)) {
          hostConnected = false;
          sawDisconnectedSession = true;
          setErrorBanner('');
          renderState(null, extras);
          return { connected: false };
        }
        setErrorBanner(errText);
        renderState(currentState, extras);
        return { connected: hostConnected };
      }
      hostConnected = true;
      setErrorBanner('');
      renderState(resp.state || null, extras);
      return { connected: true };
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
      if (isRecording) {
        statusPillEl.textContent = 'Recording';
        statusPillEl.dataset.status = 'running';
      } else if (!hostConnected) {
        statusPillEl.textContent = 'Standalone';
        statusPillEl.dataset.status = 'standalone';
      } else {
        statusPillEl.textContent = 'Idle';
        statusPillEl.dataset.status = 'idle';
      }
    }
  }

  async function syncRecordingState() {
    const resp = await sendRuntimeMessage({ type: 'GET_RECORDING' });
    if (resp && resp.ok) {
      updateRecordingUi(Boolean(resp.active));
    }
  }

  function selectSetupTab(which) {
    const isDesktop = which === 'desktop';
    if (tabDesktopEl) {
      tabDesktopEl.setAttribute('aria-selected', isDesktop ? 'true' : 'false');
    }
    if (tabCliEl) {
      tabCliEl.setAttribute('aria-selected', isDesktop ? 'false' : 'true');
    }
    if (panelDesktopEl) {
      panelDesktopEl.hidden = !isDesktop;
    }
    if (panelCliEl) {
      panelCliEl.hidden = isDesktop;
    }
  }

  async function copyCliCommand() {
    if (!cliInstallCmdEl || !copyCliBtnEl) return;
    const text = cliInstallCmdEl.textContent.trim();
    let copied = false;
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        await navigator.clipboard.writeText(text);
        copied = true;
      }
    } catch (e) {
      copied = false;
    }
    if (!copied) {
      try {
        const area = document.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', 'true');
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        copied = document.execCommand('copy');
        document.body.removeChild(area);
      } catch (e) {
        copied = false;
      }
    }
    const prev = copyCliBtnEl.textContent;
    copyCliBtnEl.textContent = copied ? 'Copied \u2713' : 'Copy failed';
    setTimeout(() => {
      copyCliBtnEl.textContent = prev || 'Copy';
    }, 1800);
  }

  if (tabDesktopEl && tabCliEl) {
    tabDesktopEl.addEventListener('click', () => selectSetupTab('desktop'));
    tabCliEl.addEventListener('click', () => selectSetupTab('cli'));
    const onTabKeydown = (ev) => {
      if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
        ev.preventDefault();
        const next = ev.currentTarget === tabDesktopEl ? 'cli' : 'desktop';
        selectSetupTab(next);
        (next === 'desktop' ? tabDesktopEl : tabCliEl).focus();
      }
    };
    tabDesktopEl.addEventListener('keydown', onTabKeydown);
    tabCliEl.addEventListener('keydown', onTabKeydown);
  }

  if (copyCliBtnEl) {
    copyCliBtnEl.addEventListener('click', () => {
      void copyCliCommand();
    });
  }

  if (checkConnectionBtnEl) {
    checkConnectionBtnEl.addEventListener('click', async () => {
      checkConnectionBtnEl.disabled = true;
      checkConnectionBtnEl.textContent = 'Checking...';
      if (connectionFeedbackEl) {
        connectionFeedbackEl.textContent = '';
        connectionFeedbackEl.removeAttribute('data-tone');
      }
      lastRenderedStateKey = '__force_rerender__';
      const result = await syncState({ forceScroll: false });
      checkConnectionBtnEl.disabled = false;
      if (result && result.connected) {
        if (connectionFeedbackEl) {
          connectionFeedbackEl.textContent = 'Local engine connected.';
          connectionFeedbackEl.setAttribute('data-tone', 'ok');
        }
      } else {
        updateOnboardingSteps();
        if (connectionFeedbackEl) {
          connectionFeedbackEl.textContent =
            'Not detected yet. Install Desktop or run the CLI command, then check again.';
        }
      }
    });
  }

  if (dismissOnboardingBtnEl) {
    dismissOnboardingBtnEl.addEventListener('click', () => {
      saveOnboardingDismissed(true);
      lastRenderedStateKey = '__force_rerender__';
      renderState(currentState, {
        authPause: currentAuthPause,
        popupState: currentPopupState,
      });
    });
  }

  if (inspectPageBtnEl) {
    inspectPageBtnEl.addEventListener('click', () => {
      void inspectCurrentPage();
    });
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
    void syncActiveTab();
    void syncState({ forceScroll: true });
  });

  document.addEventListener('DOMContentLoaded', async () => {
    await loadOnboardingPreference();
    void syncRecordingState();
    void syncActiveTab();
    void syncState({ forceScroll: true });
    setInterval(() => {
      if (document.visibilityState === 'visible') {
        void syncRecordingState();
        void syncActiveTab();
        void syncState({ forceScroll: false });
      }
    }, 1500);
  });
})();
