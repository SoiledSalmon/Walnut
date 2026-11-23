'use strict';
// All values are rendered with textContent (never innerHTML): evidence text is untrusted.

const state = { meta: null, userId: null, requestId: null, chart: null };
const $ = (sel) => document.querySelector(sel);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) node.append(c instanceof Node ? c : document.createTextNode(c ?? ''));
  return node;
}

async function api(path, body) {
  const opts = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                           body: body === null ? undefined : JSON.stringify(body) };
  const res = await fetch('/api' + path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => `${x.loc.at(-1)}: ${x.msg}`).join('; ') : d || res.statusText);
  }
  return data;
}

let toastTimer;
function toast(message, isError = false) {
  const t = $('#toast');
  t.textContent = message;
  t.className = 'toast' + (isError ? ' error' : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 7000 : 3500);
}

async function guarded(button, fn) {
  if (button) button.disabled = true;
  try { await fn(); } catch (e) { toast(e.message, true); } finally { if (button) button.disabled = false; }
}

const inr = (v, cur = 'INR') => v == null ? '—' : (Number(v) < 0 ? '−' : '') +
  (cur === 'INR' ? '₹' : cur + ' ') + Math.abs(Number(v)).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtDate = (d) => d ? new Date(d.length === 10 ? d + 'T00:00:00' : d).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' }) : '—';
const fmtTime = (d) => d ? new Date(d).toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—';
const fmtStamp = (d) => d ? new Date(d).toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';
const showSection = (id) => document.getElementById(id).scrollIntoView({ behavior: 'smooth', block: 'start' });
const STATUS_LABEL = { SAFE_NOW: 'Safe Now', SPLIT_PLAN: 'Split Plan', WAIT: 'Wait' };
const STATUS_TONE = { SAFE_NOW: 'ok', SPLIT_PLAN: 'warn', WAIT: 'bad', PASSED: 'ok', HELD: 'warn', REJECTED: 'bad',
                      PROMOTED: 'ok', RECEIVED: 'info', EXTRACTED: 'warn', EXTRACTING: 'info' };
const badge = (text, tone) => el('span', { class: 'badge ' + (tone || STATUS_TONE[text] || '') }, STATUS_LABEL[text] || text);
const formData = (form) => Object.fromEntries(new FormData(form).entries());

// ---------- meta and users ----------

async function loadMeta() {
  state.meta = await api('/meta');
  const m = state.meta;
  document.querySelectorAll('[data-currencies]').forEach((s) =>
    s.replaceChildren(...m.currencies.map((c) => el('option', { value: c.currency_code }, c.currency_code))));
  document.querySelectorAll('[data-categories]').forEach((s) => s.replaceChildren(
    ...(s.hasAttribute('data-allow-none') ? [el('option', { value: '' }, '(none)')] : []),
    ...m.categories.map((c) => el('option', { value: s.hasAttribute('data-ids') ? c.category_id : c.category_name },
                                  c.category_name + (c.is_discretionary ? ' (discretionary)' : '')))));
  document.querySelector('#event-form [name=event_date]').value = m.as_of;
}

async function loadUsers(selectId) {
  const users = await api('/users');
  const sel = $('#user-select');
  sel.replaceChildren(...users.map((u) => el('option', { value: u.user_id }, `${u.user_id} · ${u.name}`)));
  if (!users.length) { state.userId = null; return; }
  state.userId = Number(selectId ?? (users.some((u) => u.user_id === state.userId) ? state.userId : users[0].user_id));
  sel.value = state.userId;
  await loadUser();
}

async function loadUser() {
  const p = await api(`/users/${state.userId}/profile`);
  $('#profile').textContent = `${p.email} · base ${p.currency} · minimum balance ${inr(p.min_balance_threshold, p.currency)} · target savings ${inr(p.target_monthly_savings, p.currency)}/month`;
  state.requestId = null;
  $('#decision-body').hidden = true;
  $('#decision-empty').hidden = false;
  $('#pipeline').hidden = true;
  await Promise.all([loadEvidence(), loadEvents(), loadRequests(), loadAudit()]);
}

// ---------- evidence inbox ----------

async function loadEvidence() {
  const docs = await api(`/users/${state.userId}/evidence`);
  const rows = docs.filter((d) => d.kind !== 'FORM').map((d) => {
    const c = d.candidate;
    const result = c?.validation?.result;
    const actions = el('td');
    if (d.status === 'RECEIVED') actions.append(el('button', { type: 'button', onclick: (e) => processEvidence(d._id, e.target) }, 'Process'));
    else if (c) actions.append(el('button', { type: 'button', class: 'secondary', onclick: () => showPipeline(d, c) }, 'Details'));
    if (result === 'HELD' && !c.promoted_event_id)
      actions.append(' ', el('button', { type: 'button', class: 'secondary', onclick: (e) => confirmHeld(c._id, e.target) }, 'Confirm'));
    return el('tr', {},
      el('td', {}, fmtTime(d.received_at)), el('td', {}, d.kind),
      el('td', { class: 'text' }, d.raw_text, d.ocr ? el('div', { class: 'mono muted' }, 'OCR: ' + d.ocr.text.replaceAll('\n', ' · ')) : ''),
      el('td', {}, badge(d.status)), el('td', {}, result ? badge(result) : '—'), actions);
  });
  $('#evidence-table tbody').replaceChildren(...(rows.length ? rows : [el('tr', {}, el('td', { colspan: 6, class: 'muted' }, 'No messages for this user.'))]));
}

async function processEvidence(id, button) {
  await guarded(button, async () => {
    const r = await api(`/evidence/${id}/process`, null);
    showPipeline(r.evidence, r.candidate, r.event_id);
    const v = r.candidate?.validation;
    if (!v) { toast('Still being processed (another tab?). Refresh in a moment.'); return; }
    toast(v.result === 'PASSED' ? `Promoted to PostgreSQL as event ${r.event_id}` :
          v.result === 'HELD' ? 'Held for confirmation: ' + v.errors.join('; ') : 'Rejected: ' + v.errors.join('; '), v.result === 'REJECTED');
    await Promise.all([loadEvidence(), loadEvents()]);
  });
}

async function confirmHeld(candidateId, button) {
  await guarded(button, async () => {
    const r = await api(`/candidates/${candidateId}/confirm`, null);
    toast(`Confirmed by user and promoted as event ${r.event_id}`);
    await Promise.all([loadEvidence(), loadEvents()]);
  });
}

async function showPipeline(evidence, candidate, eventId) {
  eventId = eventId ?? candidate?.promoted_event_id;
  const pretty = (o) => el('pre', { class: 'code' }, JSON.stringify(o, null, 2));
  const { candidate: _, ...rawDoc } = evidence;
  let pgRow = el('p', { class: 'muted' }, 'Nothing written to PostgreSQL.');
  if (eventId) {
    const events = await api(`/users/${state.userId}/events`);
    const row = events.find((e) => e.event_id === eventId);
    if (row) { const { evidence: __, ...rest } = row; pgRow = pretty(rest); }
  }
  const v = candidate?.validation || {};
  $('#pipeline').replaceChildren(
    el('article', {}, el('h3', {}, '1 · MongoDB raw_evidence', badge(evidence.status)), pretty(rawDoc)),
    el('article', {}, el('h3', {}, '2 · candidate_events', v.result ? badge(v.result) : ''),
       el('p', {}, `Confidence ${candidate?.confidence ?? '—'} · action ${candidate?.action ?? '—'}`),
       v.errors?.length ? el('ul', {}, ...v.errors.map((e) => el('li', {}, e))) : '',
       pretty({ extracted: candidate?.extracted, target_hint: candidate?.target_hint, resolved: candidate?.resolved })),
    el('article', {}, el('h3', {}, '3 · PostgreSQL FINANCIAL_EVENTS', eventId ? badge('event ' + eventId, 'ok') : ''), pgRow));
  $('#pipeline').hidden = false;
}

// ---------- events ----------

async function loadEvents() {
  const events = await api(`/users/${state.userId}/events`);
  let lastRoot = null;
  const rows = events.map((e) => {
    const first = e.root_id !== lastRoot; lastRoot = e.root_id;
    const reason = e.is_effective ? badge('counted', 'ok') :
      badge({ EXCLUDE: 'excluded: ' + e.status.toLowerCase(), OUTFLOW_ONLY: 'excluded: pending credit' }[e.forecast_rule]
            || (e.event_type === 'INVESTMENT' ? 'excluded: unrealised' : 'lost precedence'), 'bad');
    return el('tr', { class: (e.is_effective ? '' : 'excluded ') + (first ? 'family-start' : '') },
      el('td', {}, first ? '#' + e.root_id : '↳'), el('td', {}, String(e.event_id)), el('td', {}, e.event_type),
      el('td', {}, e.category_name || '—'), el('td', { class: 'num' }, inr(e.amount, e.currency)),
      el('td', {}, fmtDate(e.event_date) + (e.recurrence_interval ? ' · ' + e.recurrence_interval.toLowerCase() : '')),
      el('td', {}, e.status), el('td', {}, `${e.source_type} (rank ${e.status_rank}, ${e.source_rank})`), el('td', {}, reason),
      el('td', { class: 'text' }, el('span', { class: 'muted' }, (e.evidence.kind || '?') + ': '), e.evidence.raw_text || ''));
  });
  $('#events-table tbody').replaceChildren(...(rows.length ? rows : [el('tr', {}, el('td', { colspan: 10, class: 'muted' }, 'No events yet. Process evidence or add one manually.'))]));
}

// ---------- requests ----------

async function loadRequests() {
  const reqs = await api(`/users/${state.userId}/requests`);
  const optionLists = await Promise.all(reqs.map((r) => api(`/requests/${r.request_id}/options`)));
  const rows = reqs.map((r, i) => el('tr', {},
    el('td', {}, `#${r.request_id}`), el('td', {}, r.category_name), el('td', { class: 'num' }, inr(r.requested_amount)),
    el('td', {}, fmtDate(r.target_deadline)),
    el('td', {}, r.affordability_status ? badge(r.affordability_status) : r.status),
    el('td', { class: 'mono' }, optionLists[i].map((o) => o.plan_type === 'FULL' ? `#${o.option_id} FULL` :
      `#${o.option_id} ${o.plan_type} ${o.tenure_months}m ${o.interest_rate_bps / 100}% fee ${o.processing_fee}`).join(' · ')),
    el('td', {},
      el('button', { type: 'button', onclick: (e) => decide(r.request_id, e.target) }, r.current_decision_id ? 'Re-decide' : 'Decide'),
      r.current_decision_id ? el('button', { type: 'button', class: 'secondary', onclick: () => showDecision(r.current_decision_id, r.request_id) }, 'View') : '')));
  $('#requests-table tbody').replaceChildren(...(rows.length ? rows : [el('tr', {}, el('td', { colspan: 7, class: 'muted' }, 'No requests yet.'))]));
  $('#option-request').replaceChildren(...reqs.map((r) => el('option', { value: r.request_id }, `#${r.request_id} ${inr(r.requested_amount)}`)));
}

async function decide(requestId, button) {
  await guarded(button, async () => {
    const d = await api(`/requests/${requestId}/decide`, null);
    renderDecision(d, requestId);
    toast(`Decided in ${d.elapsed_ms} ms inside PostgreSQL`);
    await Promise.all([loadRequests(), loadAudit()]);
    showSection('decision');
  });
}

async function showDecision(decisionId, requestId) {
  await guarded(null, async () => { renderDecision(await api(`/decisions/${decisionId}`), requestId); showSection('decision'); });
}

async function renderDecision(d, requestId) {
  const dec = d.decision;
  state.requestId = requestId;
  $('#decision-empty').hidden = true;
  $('#decision-body').hidden = false;
  $('#d-status').replaceWith(Object.assign(badge(dec.affordability_status), { id: 'd-status' }));
  const opt = d.option;
  const facts = [
    ['Request', `#${dec.request_id} · ${inr(dec.requested_amount)}`],
    ['Safe to pay today', inr(dec.amount_safe_to_pay)],
    ['Recommended', opt ? (opt.plan_type === 'FULL' ? 'Full payment' : `${opt.plan_type} ${opt.tenure_months} months (option #${opt.option_id})`) : 'Wait'],
    ['Earliest full payment', dec.earliest_safe_date ? fmtDate(dec.earliest_safe_date) : 'Beyond 90 days'],
    ['Minimum balance used', inr(dec.min_threshold_applied)],
    ['Decision', `#${dec.decision_id}` + (d.elapsed_ms != null ? ` · ${d.elapsed_ms} ms` : '')]];
  $('#d-facts').replaceChildren(...facts.map(([k, v]) => el('div', {}, el('dt', {}, k), el('dd', {}, v))));
  $('#d-summary').textContent = dec.decision_summary;
  $('#plan-table tbody').replaceChildren(...(d.plan_items.length ? d.plan_items.map((p) =>
    el('tr', {}, el('td', {}, String(p.installment_number)), el('td', {}, fmtDate(p.due_date)), el('td', { class: 'num' }, inr(p.installment_amount))))
    : [el('tr', {}, el('td', { colspan: 3, class: 'muted' }, 'No payment now.'))]));
  $('#cuts-table tbody').replaceChildren(...(d.spending_changes.length ? d.spending_changes.map((c) =>
    el('tr', {}, el('td', {}, c.category_name), el('td', { class: 'num' }, inr(c.recommended_reduction)), el('td', {}, c.period_days + ' days')))
    : [el('tr', {}, el('td', { colspan: 3, class: 'muted' }, 'None needed.'))]));
  const versions = await api(`/requests/${requestId}/decisions`);
  $('#versions-table tbody').replaceChildren(...versions.map((v) => el('tr', {},
    el('td', {}, `#${v.decision_id}` + (v.is_current ? ' (current)' : '')), el('td', {}, badge(v.affordability_status)),
    el('td', { class: 'mono' }, fmtStamp(v.valid_time_start)), el('td', { class: 'mono' }, v.valid_time_end ? fmtStamp(v.valid_time_end) : 'open'))));
  drawChart(d.forecast, Number(dec.min_threshold_applied));
}

function drawChart(forecast, minimum) {
  if (!window.Chart) { $('.chart-box').replaceChildren(el('p', { class: 'muted' }, 'Chart library unavailable (offline). Forecast rows: ' + forecast.length)); return; }
  const css = getComputedStyle(document.documentElement);
  const color = (n) => css.getPropertyValue(n).trim();
  state.chart?.destroy();
  state.chart = new Chart($('#chart'), {
    type: 'line',
    data: {
      labels: forecast.map((f) => fmtDate(f.forecast_date)),
      datasets: [
        { label: 'Projected balance', data: forecast.map((f) => Number(f.projected_balance)), borderColor: color('--accent'), pointRadius: 0, borderWidth: 2, stepped: true },
        { label: 'Cumulative savings', data: forecast.map((f) => Number(f.projected_savings)), borderColor: color('--info'), pointRadius: 0, borderWidth: 1.5, stepped: true },
        { label: 'Minimum balance', data: forecast.map(() => minimum), borderColor: color('--bad'), borderDash: [6, 4], pointRadius: 0, borderWidth: 1.5 }],
    },
    options: { maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
               scales: { y: { ticks: { callback: (v) => inr(v).replace('.00', '') } }, x: { ticks: { maxTicksLimit: 12 } } },
               plugins: { tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${inr(c.parsed.y)}` } } } },
  });
}

// ---------- audit and explain ----------

async function loadAudit() {
  const chain = await api('/audit');
  $('#audit-table tbody').replaceChildren(...(chain.length ? chain.map((a) => el('tr', {},
    el('td', {}, String(a.audit_id)), el('td', {}, '#' + a.decision_id),
    el('td', { class: 'mono' }, a.previous_hash ? a.previous_hash.slice(0, 12) + '…' : 'GENESIS'),
    el('td', { class: 'mono' }, a.current_hash.slice(0, 12) + '…'),
    el('td', {}, a.tamper_flag ? badge('TAMPERED', 'bad') : badge('intact', 'ok')),
    el('td', {}, el('button', { type: 'button', class: 'danger', onclick: (e) => tamper(a.decision_id, e.target) }, 'Tamper (demo)'))))
    : [el('tr', {}, el('td', { colspan: 6, class: 'muted' }, 'No decisions sealed yet.'))]));
}

async function tamper(decisionId, button) {
  await guarded(button, async () => {
    await api(`/decisions/${decisionId}/tamper`, null);
    toast(`Decision #${decisionId} edited directly. Now press Verify chain.`);
  });
}

// ---------- wiring ----------

function wire() {
  $('#user-select').addEventListener('change', (e) => { state.userId = Number(e.target.value); guarded(null, loadUser); });
  $('#register-form').addEventListener('submit', (e) => { e.preventDefault(); guarded(e.submitter, async () => {
    const r = await api('/users', formData(e.target)); e.target.reset(); toast(`Registered user ${r.user_id} with profile`); await loadUsers(r.user_id); }); });
  $('#evidence-form').addEventListener('submit', (e) => { e.preventDefault(); guarded(e.submitter, async () => {
    await api(`/users/${state.userId}/evidence`, formData(e.target)); e.target.reset(); toast('Stored in MongoDB raw_evidence'); await loadEvidence(); }); });
  $('#event-form').addEventListener('submit', (e) => { e.preventDefault(); guarded(e.submitter, async () => {
    const f = formData(e.target);
    const r = await api(`/users/${state.userId}/events`, { ...f, category: f.category || null, recurrence_interval: f.recurrence_interval || null });
    const v = r.candidate?.validation || { result: 'REJECTED', errors: ['not processed'] };
    toast(v.result === 'PASSED' ? `Event ${r.event_id} added through the gate` : `${v.result}: ${v.errors.join('; ')}`, v.result === 'REJECTED');
    await Promise.all([loadEvents(), loadEvidence()]); }); });
  $('#request-form').addEventListener('submit', (e) => { e.preventDefault(); guarded(e.submitter, async () => {
    const f = formData(e.target);
    const r = await api(`/users/${state.userId}/requests`, { ...f, category_id: Number(f.category_id), target_deadline: f.target_deadline || null });
    e.target.reset(); toast(`Request ${r.request_id} created with a FULL payment option`); await loadRequests(); }); });
  $('#option-form').addEventListener('submit', (e) => { e.preventDefault(); guarded(e.submitter, async () => {
    const f = formData(e.target);
    const r = await api(`/requests/${f.request_id}/options`, { plan_type: f.plan_type, tenure_months: Number(f.tenure_months),
      interest_rate_bps: Number(f.interest_rate_bps), processing_fee: f.processing_fee });
    toast(`Option ${r.option_id} added`); await loadRequests(); }); });
  $('#verify-btn').addEventListener('click', (e) => guarded(e.target, async () => {
    const r = await api('/audit/verify', null);
    $('#verify-result').textContent = r.tampered ? `${r.tampered} record(s) failed verification: tamper flag set.` : 'Chain intact: every hash recomputes.';
    await loadAudit(); }));
  $('#explain-btn').addEventListener('click', (e) => guarded(e.target, async () => {
    const r = await api(`/users/${state.userId}/explain`);
    const out = $('#explain-out');
    out.textContent = `-- user ${state.userId}, ${r.start} to ${r.end}\n` + r.plan.join('\n');
    out.hidden = false; }));
}

wire();
guarded(null, async () => { await loadMeta(); await loadUsers(); });
