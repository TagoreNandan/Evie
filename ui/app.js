/**
 * Evie Assistant Main Web Application Logic (Phase D - Dashboard-First).
 * 
 * Connects frontend UI to Evie FastAPI backend endpoints:
 * - GET /dashboard/state: Full structured snapshot (Overall status, GitHub, Gmail, Security, Activity)
 * - GET /score & GET /findings: Security metrics & remediation list
 * - POST /integrations/sync: Telemetry sync
 * - GET /config/tone & POST /config/tone: Conversational tone preference
 * - GET /briefing/wake & GET /briefing/on-demand: Briefing generation
 * - POST /chat: Secondary conversational assistant endpoint
 * - POST /calendar/propose & POST /calendar/confirm: Two-step proposal-confirmation calendar flow
 * - GET /journal/search & POST /journal/entry: RAG memory recall
 */

let activeProposalToken = null;

document.addEventListener('DOMContentLoaded', () => {
  fetchDashboardState();
  fetchTonePreference();
  fetchFindingsList();
  refreshEnrollmentStatus();
});

// --- Dashboard State Loader (Phase D Primary Surface) ---
async function fetchDashboardState() {
  try {
    const res = await fetch('/dashboard/state');
    if (!res.ok) {
      renderDashboardErrorState('Dashboard state unavailable');
      return;
    }
    const data = await res.json();
    renderDashboardUI(data);
  } catch (e) {
    console.error('Failed to fetch dashboard state:', e);
    renderDashboardErrorState('Network error fetching dashboard state');
  }
}

// Backwards-compatible alias for legacy test assertions
function fetchSecurityScore() {
  return fetchDashboardState();
}

function renderDashboardUI(data) {
  if (!data) return;

  // 1. Overall Status Card
  const statusBadge = document.getElementById('overallStatusBadge');
  const statusText = data.overall_status || 'Good';
  statusBadge.innerText = statusText;
  statusBadge.className = 'status-badge';
  if (statusText === 'Good') {
    statusBadge.classList.add('badge-good');
  } else if (statusText === 'Needs Attention') {
    statusBadge.classList.add('badge-attention');
  } else {
    statusBadge.classList.add('badge-critical');
  }

  // Security Score (Explicit null/undefined handling - ZERO fallback to 100 on 0!)
  const secScore = data.security_score || {};
  // Score and grade come from the canonical backend value; never derived client-side.
  const scoreVal = (secScore.score !== null && secScore.score !== undefined) ? secScore.score : null;
  document.getElementById('scoreValue').innerText = scoreVal !== null ? scoreVal : '--';

  const grade = (secScore.grade !== null && secScore.grade !== undefined) ? secScore.grade : '--';
  document.getElementById('scoreGrade').innerText = grade;

  const findingsSummary = data.security_findings_summary || {};
  document.getElementById('scoreTrend').innerText = `Active Open Findings: ${findingsSummary.total_open_findings || 0}`;

  // 2. Security Section Summary
  document.getElementById('secOpenSecrets').innerText = findingsSummary.open_secrets_count || 0;
  document.getElementById('secBreaches').innerText = findingsSummary.unacknowledged_breaches_count || 0;
  document.getElementById('secExposures').innerText = findingsSummary.open_exposures_count || 0;
  document.getElementById('secTwoFA').innerText = findingsSummary.missing_twofa_count || 0;
  document.getElementById('secDepAlerts').innerText = findingsSummary.high_dep_alerts_count || 0;

  // 3. GitHub Section
  const gh = data.github_summary || {};
  document.getElementById('githubRepoCount').innerText = `${gh.total_repositories || 0} Repos`;
  document.getElementById('ghOpenPRs').innerText = gh.open_prs_count || 0;
  document.getElementById('ghUnreviewedPRs').innerText = gh.unreviewed_prs_count || 0;
  document.getElementById('ghSecretFindings').innerText = gh.secret_findings_count || 0;
  document.getElementById('ghTotalCommits').innerText = gh.commits_total || 0;

  const repoListEl = document.getElementById('githubRepoList');
  if (gh.repositories && gh.repositories.length > 0) {
    let repoHtml = '';
    gh.repositories.slice(0, 5).forEach(r => {
      repoHtml += `
        <div class="repo-pill">
          <span>📦 <strong>${escapeHtml(r.name || r.full_name)}</strong></span>
          <span class="badge-neutral">${r.is_private ? 'Private' : 'Public'}</span>
        </div>
      `;
    });
    repoListEl.innerHTML = repoHtml;
  } else {
    repoListEl.innerHTML = '<p class="empty-state">No repositories synced yet.</p>';
  }

  // 4. Gmail Section & Phishing / Promotional Separation
  const gm = data.gmail_summary || {};
  document.getElementById('gmailTotalScanned').innerText = `${gm.total_emails_scanned || 0} Scanned`;
  document.getElementById('gmailImportantCount').innerText = gm.important_count || 0;
  document.getElementById('gmailPromoCount').innerText = gm.promotional_count || 0;
  document.getElementById('gmailPhishCount').innerText = gm.phishing_alert_count || 0;

  const gmailBoxEl = document.getElementById('gmailDetailsBox');
  let gmailHtml = '';

  // Render phishing alerts first with heuristic disclaimer
  if (data.phishing_alerts && data.phishing_alerts.length > 0) {
    data.phishing_alerts.forEach(p => {
      gmailHtml += `
        <div class="phish-alert-item">
          <strong>🚨 [PHISHING ALERT] ${escapeHtml(p.subject || 'Suspicious Email')}</strong>
          <div>From: <code>${escapeHtml(p.sender || p.sender_domain)}</code> | Risk: ${p.risk_score}</div>
          <span class="phish-item-disclaimer">Heuristic risk assessment — not definitive proof.</span>
        </div>
      `;
    });
  }

  // Render top promotional senders cleanly distinct from phishing
  const topPromos = gm.top_promotional_senders || [];
  if (topPromos.length > 0) {
    gmailHtml += `<div class="promo-senders-summary"><strong>📢 Top Promotional Senders:</strong></div>`;
    topPromos.slice(0, 3).forEach(s => {
      gmailHtml += `<div>• ${escapeHtml(s.domain)} (${s.count} emails)</div>`;
    });
  }

  if (!gmailHtml) {
    gmailHtml = '<p class="empty-state">No notable email alerts or promotional senders.</p>';
  }
  gmailBoxEl.innerHTML = gmailHtml;

  // 5. Activity Timeline
  const timelineEl = document.getElementById('activityTimeline');
  const activityList = data.recent_activity || [];
  document.getElementById('timelineCount').innerText = `${activityList.length} Events`;

  if (activityList.length > 0) {
    let timelineHtml = '';
    activityList.forEach(act => {
      const type = act.activity_type || 'event';
      const icon = type === 'commit' ? '📝' : (type === 'pr' ? '🔀' : '⚡');
      const timeStr = act.timestamp ? new Date(act.timestamp).toLocaleTimeString() : '';
      timelineHtml += `
        <div class="timeline-item gh">
          <div class="timeline-content">
            <div>${icon} <strong>${escapeHtml(act.repo_name)}:</strong> ${escapeHtml(act.summary)}</div>
            <div class="timeline-time">By ${escapeHtml(act.author || 'system')} ${timeStr ? 'at ' + timeStr : ''}</div>
          </div>
        </div>
      `;
    });
    timelineEl.innerHTML = timelineHtml;
  } else {
    timelineEl.innerHTML = '<p class="empty-state">No recent system activity recorded.</p>';
  }
}

function renderDashboardErrorState(msg) {
  const statusBadge = document.getElementById('overallStatusBadge');
  if (statusBadge) {
    statusBadge.innerText = 'Offline';
    statusBadge.className = 'status-badge badge-attention';
  }
  document.getElementById('scoreTrend').innerText = msg || 'Failed to load live state';
}

function refreshSecurityScore() {
  fetchDashboardState();
  fetchFindingsList();
}

// --- Tone Preference ---
async function fetchTonePreference() {
  try {
    const res = await fetch('/config/tone');
    if (!res.ok) return;
    const data = await res.json();
    const toneSelect = document.getElementById('toneSelect');
    if (toneSelect) toneSelect.value = data.tone_preference || 'neutral';
  } catch (e) {
    console.error('Failed to fetch tone preference:', e);
  }
}

async function updateTonePreference(newTone) {
  try {
    await fetch('/config/tone', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tone_preference: newTone }),
    });
  } catch (e) {
    console.error('Failed to update tone:', e);
  }
}

// --- Integrations Sync ---
async function triggerSync() {
  appendAssistantMessage('Syncing external security integrations (GitHub, HIBP, Google Drive, Calendar, Gmail)...');
  try {
    const res = await fetch('/integrations/sync', { method: 'POST' });
    const data = await res.json();
    let msg = `Integration Sync Processed! (Status: ${data.status})`;
    const details = [];
    if (data.github) details.push(`GitHub: ${data.github.status || 'unknown'}`);
    if (data.gmail) details.push(`Gmail: ${data.gmail.status || 'unknown'}`);
    if (data.hibp) details.push(`HIBP: ${data.hibp.status || 'unknown'}`);
    if (details.length > 0) {
      msg += `\nIntegrations status: ${details.join(', ')}.`;
    }
    appendAssistantMessage(msg);
    fetchDashboardState();
    fetchFindingsList();
  } catch (e) {
    appendAssistantMessage('Integration sync failed or offline.');
  }
}

// --- Briefings ---
async function triggerBriefing(type) {
  const endpoint = type === 'wake' ? '/briefing/wake' : '/briefing/on-demand';
  appendAssistantMessage(`Generating ${type} briefing report...`);
  try {
    const res = await fetch(endpoint);
    const data = await res.json();
    renderStructuredChatMessage(data);
    if (window.voiceAdapter && window.voiceAdapter.isTTSSupported()) {
      window.voiceAdapter.speak(`Evie ${type} briefing ready.`);
    }
  } catch (e) {
    appendAssistantMessage('Failed to generate briefing report.');
  }
}

// --- Chat & Messaging with Structured UI Rendering ---
function sendPrompt(text) {
  const input = document.getElementById('userInput');
  if (input) {
    input.value = text;
    sendUserMessage();
  }
}

async function sendUserMessage() {
  const input = document.getElementById('userInput');
  if (!input) return;
  const text = input.value.trim();
  if (!text) return;

  appendUserMessage(text);
  input.value = '';

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text }),
    });
    handleAssistantResponse(await res.json());
  } catch (e) {
    appendAssistantMessage('Failed to query Evie backend.');
  }
}

// Voice transcripts go to the shared tool-calling router through /voice/command.
async function sendVoiceCommand(transcript, audioWavBase64) {
  const text = (transcript || '').trim();
  if (!text) return;
  appendUserMessage(`🎙️ ${text}`);

  const body = { transcript: text };
  if (audioWavBase64) body.audio_wav_base64 = audioWavBase64; // used only for local speaker verification
  try {
    const res = await fetch('/voice/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    handleAssistantResponse(await res.json());
  } catch (e) {
    appendAssistantMessage('Failed to send voice command to Evie backend.');
  }
}

function handleAssistantResponse(data) {
  renderStructuredChatMessage(data);

  if (data && data.proposal) {
    activeProposalToken = data.proposal.proposal_token;
    showCalendarModal(data.proposal);
  }

  const replyText = typeof data === 'string' ? data : ((data && (data.reply || data.detail)) || '');
  if (replyText && window.voiceAdapter && window.voiceAdapter.isTTSSupported()) {
    window.voiceAdapter.speak(replyText);
  }
}

function renderStructuredChatMessage(data) {
  if (typeof data === 'string') {
    appendAssistantMessage(data);
    return;
  }

  // Handle standard JSON objects with reply, plus the structured ui payload
  if (data.reply) {
    appendAssistantMessage(data.reply);
    if (data.ui) renderToolUI(data.ui);
    return;
  }

  // Handle structured briefing objects
  if (data.trigger || data.sections) {
    let cardHtml = `<strong>📋 Briefing Report (${escapeHtml(data.trigger || 'on-demand')})</strong>`;
    if (data.timestamp) {
      cardHtml += `<div class="timeline-time">${escapeHtml(data.timestamp)}</div>`;
    }
    if (data.sections) {
      cardHtml += `<table class="chat-table">`;
      if (data.sections.github_activity) {
        cardHtml += `<tr><td>GitHub Events</td><td>${data.sections.github_activity.count || 0}</td></tr>`;
      }
      if (data.sections.security_findings) {
        cardHtml += `<tr><td>Open Secrets</td><td>${data.sections.security_findings.open_secrets || 0}</td></tr>`;
        cardHtml += `<tr><td>Public Exposures</td><td>${data.sections.security_findings.public_exposures || 0}</td></tr>`;
      }
      cardHtml += `</table>`;
    }
    appendAssistantHTML(cardHtml);
    return;
  }

  appendAssistantMessage(JSON.stringify(data));
}

// --- Structured Tool UI Payloads (03_UIUX_Design.md Section 3) ---
function uiText(v) {
  return (v === null || v === undefined) ? '' : escapeHtml(String(v));
}

function severityBadge(sev) {
  const s = String(sev || '').toLowerCase();
  if (s === 'critical' || s === 'high') return '<span class="badge badge-critical">🔴 ' + uiText(sev) + '</span>';
  if (s === 'medium' || s === 'moderate') return '<span class="badge badge-attention">🟡 ' + uiText(sev) + '</span>';
  return '<span class="badge badge-good">🟢 ' + uiText(sev || 'low') + '</span>';
}

function renderUITable(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return '<p class="empty-state">Nothing to show.</p>';
  const cols = Object.keys(rows[0]).filter(k => rows.some(r => r[k] !== null && typeof r[k] !== 'object')).slice(0, 5);
  let html = '<table class="chat-table"><tr>' + cols.map(c => `<th>${uiText(c.replace(/_/g, ' '))}</th>`).join('') + '</tr>';
  rows.slice(0, 20).forEach(r => {
    html += '<tr>' + cols.map(c => `<td>${uiText(typeof r[c] === 'object' ? '' : r[c])}</td>`).join('') + '</tr>';
  });
  return html + '</table>';
}

function renderUISummary(obj) {
  const rows = Object.entries(obj || {}).filter(([, v]) => v !== null && typeof v !== 'object');
  if (rows.length === 0) return '';
  return '<table class="chat-table">' + rows.map(([k, v]) => `<tr><td>${uiText(k.replace(/_/g, ' '))}</td><td>${uiText(v)}</td></tr>`).join('') + '</table>';
}

function renderToolUI(ui) {
  if (!ui || !ui.type) return;
  const data = ui.data;
  let html = '';

  switch (ui.type) {
    case 'finding_cards':
      if (!Array.isArray(data) || data.length === 0) { html = '<p class="empty-state">No open findings.</p>'; break; }
      data.slice(0, 10).forEach(c => {
        const key = `${uiText(c.finding_type)}-${uiText(c.id)}`;
        html += `
          <div class="chat-card" id="finding-card-${key}">
            <div>${severityBadge(c.severity)} <strong>${uiText(c.title)}</strong></div>
            <div class="timeline-time">${uiText(c.detail)}</div>
            <div class="banner-actions">
              <button class="btn btn-xs btn-outline" onclick="resolveFindingFromCard(this, '${uiText(c.finding_type)}', ${Number(c.id)}, 'resolved')">Resolve</button>
              <button class="btn btn-xs btn-outline" onclick="resolveFindingFromCard(this, '${uiText(c.finding_type)}', ${Number(c.id)}, 'false_positive')">False positive</button>
            </div>
          </div>`;
      });
      break;
    case 'tool_proposal':
      html = `
        <div class="chat-card">
          <strong>⚠️ Confirmation required</strong>
          <p>${uiText(data.prompt)}</p>
          <div class="banner-actions">
            <button class="btn btn-xs btn-primary" onclick="confirmToolProposal(this, '${uiText(data.proposal_token)}', true)">Confirm</button>
            <button class="btn btn-xs btn-outline" onclick="confirmToolProposal(this, '${uiText(data.proposal_token)}', false)">Cancel</button>
          </div>
        </div>`;
      break;
    case 'gmail_proposals':
      (Array.isArray(data) ? data : []).forEach(p => {
        html += `
          <div class="chat-card">
            <strong>Unsubscribe from ${uiText(p.sender_domain)}?</strong>
            <div class="banner-actions">
              <button class="btn btn-xs btn-primary" onclick="confirmGmailProposal(this, '${uiText(p.proposal_token)}', true)">Confirm</button>
              <button class="btn btn-xs btn-outline" onclick="confirmGmailProposal(this, '${uiText(p.proposal_token)}', false)">Reject</button>
            </div>
          </div>`;
      });
      break;
    case 'alerts':
      if (!Array.isArray(data) || data.length === 0) { html = '<p class="empty-state">No alerts.</p>'; break; }
      data.slice(0, 10).forEach(a => {
        html += `
          <div class="chat-card">
            ${severityBadge(a.risk_score)} <strong>${uiText(a.subject)}</strong>
            <div class="timeline-time">From ${uiText(a.sender)} · ${uiText(String(a.received_at || '').slice(0, 10))}</div>
            <div class="phish-item-disclaimer">Heuristic risk indicator, not proof of phishing.</div>
          </div>`;
      });
      break;
    case 'timeline':
      if (!Array.isArray(data) || data.length === 0) { html = '<p class="empty-state">No events in this window.</p>'; break; }
      html = '<div class="timeline-list">' + data.slice(0, 20).map(e => `
        <div class="timeline-item gh">
          <div class="timeline-content"><strong>${uiText(e.repo_name)}</strong> ${uiText(e.summary)}</div>
          <div class="timeline-time">${uiText(e.author)} · ${uiText(String(e.timestamp || '').slice(0, 16))}</div>
        </div>`).join('') + '</div>';
      break;
    case 'table':
      html = renderUITable(data);
      break;
    case 'score':
      if (data && data.score !== null && data.score !== undefined) {
        html = `<div class="chat-card"><strong>${uiText(data.score)}/100</strong> <span class="badge badge-neutral">Grade ${uiText(data.grade)}</span></div>`;
      }
      break;
    case 'summary':
    case 'dashboard':
    case 'briefing':
      html = renderUISummary(ui.type === 'dashboard' && data ? data.security_findings_summary : data);
      break;
    case 'steps':
      if (data && Array.isArray(data.steps)) {
        html = '<ol>' + data.steps.map(st => `<li>${uiText(String(st).replace(/^\d+\.\s*/, ''))}</li>`).join('') + '</ol>';
      }
      break;
    case 'notice':
      html = `<span class="badge badge-neutral">${uiText(data && data.reason)}</span>`;
      break;
    default:
      return; // 'text' and 'proposal' (calendar modal) need no extra rendering
  }

  if (html) appendAssistantHTML(html);
}

async function confirmToolProposal(btn, token, confirmed) {
  if (btn && btn.parentElement) btn.parentElement.querySelectorAll('button').forEach(b => { b.disabled = true; });
  try {
    const res = await fetch('/tools/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proposal_token: token, confirmed: confirmed }),
    });
    const data = await res.json();
    appendAssistantMessage(data.reply || `Request ${data.status}.`);
    if (data.ui && data.ui.type !== 'notice') renderToolUI(data.ui);
    if (data.status === 'executed') fetchDashboardState();
  } catch (e) {
    appendAssistantMessage('Confirmation failed.');
  }
}

async function confirmGmailProposal(btn, token, confirmed) {
  if (btn && btn.parentElement) btn.parentElement.querySelectorAll('button').forEach(b => { b.disabled = true; });
  try {
    const res = await fetch('/gmail/confirm_cleanup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proposal_token: token, confirmed: confirmed }),
    });
    const data = await res.json();
    appendAssistantMessage(data.message || data.detail || 'Cleanup proposal updated.');
  } catch (e) {
    appendAssistantMessage('Cleanup confirmation failed.');
  }
}

// Finding cards: explicit two-step click (Propose -> Confirm) before resolving.
async function resolveFindingFromCard(btn, findingType, findingId, resolution) {
  if (!btn.dataset.armed) {
    btn.dataset.armed = '1';
    btn.innerText = resolution === 'false_positive' ? 'Confirm false positive?' : 'Confirm resolve?';
    return;
  }
  btn.parentElement.querySelectorAll('button').forEach(b => { b.disabled = true; });
  try {
    const res = await fetch(`/findings/${findingId}/resolve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ finding_type: findingType, status: resolution }),
    });
    const data = await res.json();
    if (res.ok) {
      appendAssistantMessage(`Marked ${findingType} finding #${findingId} as ${resolution === 'false_positive' ? 'a false positive' : 'resolved'}. Security score: ${data.score.score}/100.`);
      fetchDashboardState();
    } else {
      appendAssistantMessage(`Could not update finding: ${data.detail || res.status}`);
    }
  } catch (e) {
    appendAssistantMessage('Finding update failed.');
  }
}

function appendUserMessage(text) {
  const chatMessages = document.getElementById('chatMessages');
  if (!chatMessages) return;
  const msg = document.createElement('div');
  msg.className = 'message user-message';
  msg.innerHTML = `
    <div class="message-avatar">U</div>
    <div class="message-body"><p>${escapeHtml(text)}</p></div>
  `;
  chatMessages.appendChild(msg);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

function appendAssistantMessage(text) {
  appendAssistantHTML(`<p>${escapeHtml(text)}</p>`);
}

function appendAssistantHTML(innerHtml) {
  const chatMessages = document.getElementById('chatMessages');
  if (!chatMessages) return;
  const msg = document.createElement('div');
  msg.className = 'message assistant-message';
  msg.innerHTML = `
    <div class="message-avatar">E</div>
    <div class="message-body">${innerHtml}</div>
  `;
  chatMessages.appendChild(msg);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

// --- Calendar Action Safety Protocol ---
function showCalendarModal(data) {
  document.getElementById('modalPromptText').innerText = data.prompt || 'Confirm calendar action?';
  const details = data.proposal_details || {};
  document.getElementById('modalProposalDetails').innerHTML = `
    <p><strong>Action:</strong> ${escapeHtml(details.action_type || '')}</p>
    <p><strong>Summary:</strong> ${escapeHtml(details.summary || '')}</p>
    <p><strong>Start:</strong> ${escapeHtml(details.start_time || '')}</p>
    <p><strong>End:</strong> ${escapeHtml(details.end_time || '')}</p>
    <p><strong>Token:</strong> <code>${escapeHtml(data.proposal_token || '')}</code></p>
  `;
  document.getElementById('calendarModal').classList.add('open');
}

function closeCalendarModal() {
  document.getElementById('calendarModal').classList.remove('open');
}

async function confirmCalendarProposal(confirmed) {
  if (!activeProposalToken) return;

  try {
    const res = await fetch('/calendar/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        proposal_token: activeProposalToken,
        confirmed: confirmed,
      }),
    });

    const data = await res.json();
    closeCalendarModal();

    if (confirmed && data.status === 'executed') {
      appendAssistantMessage(`✅ Calendar event '${data.summary}' successfully created! (ID: ${data.event_id})`);
    } else {
      appendAssistantMessage(`🚫 Calendar action cancelled or expired: ${data.message || data.status}`);
    }
  } catch (e) {
    appendAssistantMessage('Calendar confirmation failed.');
    closeCalendarModal();
  }
  activeProposalToken = null;
}

// --- Security Voice Control (Conversational Feature) ---
function toggleVoiceListening() {
  const micBtn = document.getElementById('micBtn');
  const statusText = document.getElementById('voiceStatusText');

  if (!window.voiceAdapter || !window.voiceAdapter.isSTTSupported()) {
    alert('Browser SpeechRecognition is not supported on this browser.');
    return;
  }

  const recorder = window.voiceSampleRecorder;
  const recorderReady = recorder && recorder.isSupported();

  if (window.voiceAdapter.isListening) {
    window.voiceAdapter.stopListening();
    if (recorderReady) recorder.cancel();
    if (micBtn) micBtn.classList.remove('listening');
    if (statusText) statusText.innerText = 'Voice Ready';
  } else {
    if (micBtn) micBtn.classList.add('listening');
    if (statusText) statusText.innerText = 'Listening...';
    if (recorderReady) recorder.start().catch(() => recorder.cancel());
    window.voiceAdapter.startListening(
      async (transcript) => {
        if (micBtn) micBtn.classList.remove('listening');
        if (statusText) statusText.innerText = 'Voice Captured';
        let audio = null;
        if (recorderReady) {
          try { audio = await recorder.stop(); } catch (e) { audio = null; }
        }
        sendVoiceCommand(transcript, audio);
      },
      (err) => {
        if (recorderReady) recorder.cancel();
        if (micBtn) micBtn.classList.remove('listening');
        if (statusText) statusText.innerText = `Voice Error: ${err}`;
      }
    );
  }
}

// --- Dedicated Voice Automation (Computer Control Feature) ---
let currentAutomationSessionId = null;
let lastExecutedResultKey = null;

async function toggleVoiceAutomation() {
  const btn = document.getElementById('automationBtn');
  const statusText = document.getElementById('automationStatusText');

  if (!window.voiceAdapter || !window.voiceAdapter.isSTTSupported()) {
    const msg = 'Browser SpeechRecognition is not supported in this browser.';
    if (statusText) statusText.innerText = msg;
    alert(msg);
    return;
  }

  if (currentAutomationSessionId !== null) {
    await stopVoiceAutomation();
    return;
  }

  const newSessionId = 'auto_sess_' + Date.now() + '_' + Math.random().toString(36).substring(2, 7);
  if (statusText) statusText.innerText = 'Starting Voice Automation service...';
  if (btn) btn.disabled = true;

  try {
    const res = await fetch('/voice/automation/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'start', session_id: newSessionId }),
    });
    const data = await res.json();

    if (data.status !== 'OK') {
      const errMsg = data.reply || 'Voice Automation service failed to start.';
      if (statusText) statusText.innerText = `Error: ${errMsg}`;
      if (btn) {
        btn.disabled = false;
        btn.innerText = '🎙 Start Voice Automation';
        btn.classList.remove('listening');
      }
      if (window.voiceAdapter && window.voiceAdapter.isTTSSupported()) {
        window.voiceAdapter.speak(errMsg);
      }
      return;
    }
  } catch (e) {
    const errMsg = 'Failed to connect to Voice Automation service.';
    if (statusText) statusText.innerText = `Error: ${errMsg}`;
    if (btn) {
      btn.disabled = false;
      btn.innerText = '🎙 Start Voice Automation';
      btn.classList.remove('listening');
    }
    return;
  } finally {
    if (btn) btn.disabled = false;
  }

  // Active session set ONLY after backend START returns success
  currentAutomationSessionId = newSessionId;
  lastExecutedResultKey = null;

  window.voiceAdapter.continuous = true;
  window.voiceAdapter.interimResults = true;

  window.voiceAdapter.startListening(
    async (transcript, isFinal, resultIndex) => {
      const activeSessionId = currentAutomationSessionId;
      if (!activeSessionId) return;

      // Rule 8: Interim results must NEVER execute
      if (!isFinal) return;

      // Rule 9: Deduplicate final recognition results
      const resultKey = `${activeSessionId}_${resultIndex}_${transcript}`;
      if (lastExecutedResultKey === resultKey) return;
      lastExecutedResultKey = resultKey;

      await executeVoiceAutomationCommand(transcript, activeSessionId);
    },
    (err) => {
      if (currentAutomationSessionId) {
        stopVoiceAutomation();
        if (statusText) statusText.innerText = `Voice Error: ${err}`;
      }
    }
  );

  if (btn) {
    btn.innerText = '🛑 Stop Voice Automation';
    btn.classList.add('listening');
  }
  if (statusText) statusText.innerText = 'Voice Automation Active — Listening';
}

async function stopVoiceAutomation() {
  const sessionToStop = currentAutomationSessionId;

  // STOP RACE SAFETY: Immediately invalidate local session state
  currentAutomationSessionId = null;
  lastExecutedResultKey = null;

  if (window.voiceAdapter) {
    window.voiceAdapter.stopListening();
  }

  const btn = document.getElementById('automationBtn');
  const statusText = document.getElementById('automationStatusText');

  if (btn) {
    btn.innerText = '🎙 Start Voice Automation';
    btn.classList.remove('listening');
    btn.disabled = false;
  }
  if (statusText) statusText.innerText = 'Voice Automation Inactive';

  if (sessionToStop) {
    try {
      const res = await fetch('/voice/automation/stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'stop', session_id: sessionToStop }),
      });
      const data = await res.json();
      if (data.status !== 'OK' && statusText) {
        statusText.innerText = 'Voice Automation Stopped (Service Warning)';
      }
    } catch (e) {
      if (statusText) {
        statusText.innerText = 'Voice Automation Stopped (Service Offline)';
      }
    }
  }
}

async function executeVoiceAutomationCommand(transcript, sessionId) {
  const text = (transcript || '').trim();
  if (!text) return;

  if (!currentAutomationSessionId || currentAutomationSessionId !== sessionId) return;

  const statusText = document.getElementById('automationStatusText');
  if (statusText) statusText.innerText = `Executing: "${text}"...`;

  try {
    const res = await fetch('/voice/automation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ transcript: text, session_id: sessionId }),
    });

    if (!currentAutomationSessionId || currentAutomationSessionId !== sessionId) return;

    const data = await res.json();

    if (!currentAutomationSessionId || currentAutomationSessionId !== sessionId) return;

    // Completely decoupled from chat UI:
    // Successful execution: SILENT (no speech, no chat message)
    // Clarification/Error: speak ONLY the necessary clarification text
    if (data.status === 'ASKING') {
      const speechText = data.reply || 'Clarification required.';
      if (speechText && window.voiceAdapter && window.voiceAdapter.isTTSSupported()) {
        window.voiceAdapter.speak(speechText);
      }
      if (statusText) statusText.innerText = `Clarification: ${speechText}`;
    } else if (data.status === 'SESSION_ENDED') {
      // The backend session itself is gone (not a command failure): reflect it, never keep "listening" silently.
      const speechText = data.reply || 'Voice automation stopped.';
      if (window.voiceAdapter && window.voiceAdapter.isTTSSupported()) {
        window.voiceAdapter.speak(speechText);
      }
      await stopVoiceAutomation();
      const st = document.getElementById('automationStatusText');
      if (st) st.innerText = `Error: ${speechText}`;
    } else if (data.status === 'ERROR') {
      const speechText = data.reply || 'Execution error.';
      if (speechText && window.voiceAdapter && window.voiceAdapter.isTTSSupported()) {
        window.voiceAdapter.speak(speechText);
      }
      if (statusText) statusText.innerText = `Error: ${speechText}`;
    } else {
      if (statusText) statusText.innerText = 'Voice Automation Active — Listening';
    }
  } catch (e) {
    if (currentAutomationSessionId && currentAutomationSessionId === sessionId && statusText) {
      statusText.innerText = 'Voice Automation Error';
    }
  }
}

window.toggleVoiceAutomation = toggleVoiceAutomation;
window.stopVoiceAutomation = stopVoiceAutomation;

function initVoiceAutomationUI() {
  const btn = document.getElementById('automationBtn');
  if (btn && !btn.dataset.automationBound) {
    btn.dataset.automationBound = 'true';
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      toggleVoiceAutomation();
    });
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initVoiceAutomationUI);
} else {
  initVoiceAutomationUI();
}

// --- Speaker Verification Enrollment (local only) ---
let voiceEnrollment = { enrolled: false, verification_available: false };
let enrollmentReplace = false;

async function refreshEnrollmentStatus() {
  try {
    const res = await fetch('/voice/enrollment');
    voiceEnrollment = await res.json();
  } catch (e) {
    voiceEnrollment = { enrolled: false, verification_available: false };
  }
  const btn = document.getElementById('enrollVoiceBtn');
  if (btn) btn.innerText = voiceEnrollment.enrolled ? '🔐 Re-enroll Voice' : '🔐 Enroll Voice';
}

function startVoiceEnrollment() {
  if (!window.voiceSampleRecorder || !window.voiceSampleRecorder.isSupported()) {
    appendAssistantMessage('This browser cannot record a local voice sample, so voice enrollment is unavailable.');
    return;
  }
  if (voiceEnrollment.enrolled) {
    // Re-enrollment replaces your voice print, so it needs an explicit click confirmation first.
    appendAssistantHTML(`
      <div class="chat-card">
        <strong>⚠️ Replace your enrolled voice?</strong>
        <p>Sensitive voice actions will then only accept the new recording's voice.</p>
        <div class="banner-actions">
          <button class="btn btn-xs btn-primary" onclick="this.parentElement.querySelectorAll('button').forEach(b => b.disabled = true); beginEnrollmentRecording(true)">Confirm</button>
          <button class="btn btn-xs btn-outline" onclick="this.parentElement.querySelectorAll('button').forEach(b => b.disabled = true); appendAssistantMessage('Re-enrollment cancelled - your existing voice print is unchanged.')">Cancel</button>
        </div>
      </div>`);
    return;
  }
  beginEnrollmentRecording(false);
}

async function beginEnrollmentRecording(replaceExisting) {
  enrollmentReplace = replaceExisting;
  try {
    await window.voiceSampleRecorder.start();
  } catch (e) {
    appendAssistantMessage('Microphone access was not available, so enrollment did not start.');
    return;
  }
  appendAssistantHTML(`
    <div class="chat-card">
      <strong>🎙️ Recording your voice sample…</strong>
      <p>Speak naturally for a few seconds, then click Stop (it stops automatically at 15 seconds).</p>
      <div class="banner-actions">
        <button class="btn btn-xs btn-primary" onclick="this.disabled = true; finishEnrollmentRecording()">Stop</button>
      </div>
    </div>`);
}

async function finishEnrollmentRecording() {
  let audio = null;
  try { audio = await window.voiceSampleRecorder.stop(); } catch (e) { audio = null; }
  if (!audio) {
    appendAssistantMessage('That recording was too short (at least 1 second is needed). Nothing was saved.');
    return;
  }
  try {
    const res = await fetch('/voice/enroll', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ audio_wav_base64: audio, replace_existing: enrollmentReplace }),
    });
    const data = await res.json();
    const messages = {
      REENROLLMENT_CONFIRMATION_REQUIRED: 'A voice is already enrolled. Use Re-enroll Voice and confirm to replace it.',
      INVALID_AUDIO: 'That recording could not be used (it may be silent or too long). Nothing was saved.',
      NO_VOICE_SAMPLE: 'No recording was received. Nothing was saved.',
      VERIFICATION_UNAVAILABLE: 'The local voice verification model is not installed on this computer yet, so enrollment could not run.',
    };
    if (res.ok) {
      appendAssistantMessage(data.replaced
        ? 'Your voice print was replaced. The recording itself was discarded; only the voice print is kept on this computer.'
        : 'Voice enrolled. The recording itself was discarded; only the voice print is kept on this computer.');
    } else {
      appendAssistantMessage(messages[data.detail] || 'Voice enrollment failed. Nothing was saved.');
    }
  } catch (e) {
    appendAssistantMessage('Voice enrollment failed. Nothing was saved.');
  }
  enrollmentReplace = false;
  refreshEnrollmentStatus();
}

// --- Drawers & Findings ---
function toggleDrawer(id) {
  const d = document.getElementById(id);
  if (d) d.classList.toggle('open');
}

async function fetchFindingsList() {
  try {
    const res = await fetch('/findings');
    if (!res.ok) return;
    const findings = await res.json();
    
    let totalCount = 0;
    let html = '';

    for (const [cat, list] of Object.entries(findings)) {
      totalCount += list.length;
      if (list.length > 0) {
        html += `<h4>${escapeHtml(cat.toUpperCase())} (${list.length})</h4><ul>`;
        list.forEach(item => {
          html += `<li>${escapeHtml(item.summary || item.pattern_type || item.email_checked || item.source)}</li>`;
        });
        html += `</ul>`;
      }
    }

    const countEl = document.getElementById('findingsCount');
    if (countEl) countEl.innerText = totalCount;
    const listEl = document.getElementById('findingsList');
    if (listEl) listEl.innerHTML = totalCount > 0 ? html : '<p>No open security findings!</p>';
  } catch (e) {
    console.error('Failed to fetch findings:', e);
  }
}

// --- Memory Search ---
async function searchMemory() {
  const input = document.getElementById('memoryQuery');
  if (!input) return;
  const query = input.value.trim();
  if (!query) return;

  try {
    const res = await fetch(`/journal/search?q=${encodeURIComponent(query)}`);
    const data = await res.json();
    let html = '<h4>RAG Memory Recall Results:</h4>';
    if (data.results && data.results.length > 0) {
      data.results.forEach(r => {
        html += `<div class="memory-card"><p><strong>${escapeHtml(r.entry_date)}:</strong> ${escapeHtml(r.content)}</p></div>`;
      });
    } else {
      html += '<p>No matching memory recall entries found.</p>';
    }
    document.getElementById('memoryResults').innerHTML = html;
  } catch (e) {
    document.getElementById('memoryResults').innerHTML = '<p>Memory recall query failed.</p>';
  }
}

async function addJournal() {
  const date = document.getElementById('journalDate').value;
  const content = document.getElementById('journalContent').value.trim();
  if (!date || !content) return;

  try {
    await fetch('/journal/entry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entry_date: date, content: content }),
    });
    alert('Journal entry added and indexed successfully!');
    document.getElementById('journalContent').value = '';
  } catch (e) {
    alert('Failed to save journal entry.');
  }
}

function escapeHtml(text) {
  if (!text) return '';
  return String(text).replace(/[&<>"']/g, m => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
  })[m]);
}
