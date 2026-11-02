'use strict';
/* FinFilings AI audit workbench. Native DOM only: every server, model and filing string is inserted with
   textContent / text nodes (never innerHTML, never Markdown). Nothing is persisted in the browser. */
(() => {
  const MAX_PROMPT_BYTES = 8192;
  const REQUEST_TIMEOUT_MS = 15000;
  const TERMINAL = ['completed', 'failed', 'cancelled'];
  const STATUS_LABEL = { pending: 'Starting', running: 'Running', completed: 'Completed', failed: 'Failed', cancelled: 'Cancelled' };
  const SECTION_LABEL = {
    'Item 1A': 'Risk factors (Item 1A)', 'Item 7': 'Management discussion (Item 7)', 'Item 8': 'Financial statements (Item 8)',
    'Full filing (item not classified)': 'Full filing (section not classified)',
  };
  const $ = (id) => document.getElementById(id);
  const encoder = new TextEncoder();
  const state = { catalog: null, jobId: null, poll: null, pollFailures: 0, tick: null, base: 0, baseAt: 0, result: null, job: null, evidence: new Map(), samples: [], viewRequests: new Set(), seq: 0, historySeq: 0, submitting: false };

  /* ---------- DOM helpers (text only) ---------- */
  function el(tag, options = {}, ...children) {
    const node = document.createElement(tag);
    if (options.cls) node.className = options.cls;
    if (options.text !== undefined) node.textContent = String(options.text);
    for (const [name, value] of Object.entries(options.attrs || {})) node.setAttribute(name, String(value));
    for (const [name, handler] of Object.entries(options.on || {})) node.addEventListener(name, handler);
    for (const child of children.flat()) if (child !== null && child !== undefined && child !== false) node.append(child);
    return node;
  }
  const clear = (node) => node.replaceChildren();
  const code = (text) => el('code', { text });

  function safeSecUrl(value) {
    try {
      const url = new URL(value);
      if (url.protocol === 'https:' && url.hostname === 'www.sec.gov' && url.port === '' && !url.username && !url.password &&
          url.pathname.startsWith('/Archives/') && !url.search && !url.hash && url.href === value) return url.href;
    } catch (_) { /* fall through */ }
    return null;
  }
  function secLink(url, label, extra = {}) {
    const safe = safeSecUrl(url);
    if (!safe) return el('span', { text: `${label} (link withheld: failed the sec.gov safety check)` });
    return el('a', { text: label, cls: extra.cls, attrs: { href: safe, target: '_blank', rel: 'noopener noreferrer', ...(extra.attrs || {}) } });
  }
  const sectionLabel = (section) => SECTION_LABEL[section] || section || 'Unknown section';
  const seconds = (value) => `${Number(value).toFixed(1)} s`;
  const shortId = (id) => String(id).slice(0, 8);

  /* ---------- API ---------- */
  async function api(method, path, body, view = false) {
    const controller = new AbortController();
    if (view) state.viewRequests.add(controller);
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    const init = { method, cache: 'no-store', credentials: 'same-origin', referrerPolicy: 'no-referrer', headers: { 'X-Requested-With': 'finfilings-ui' }, signal: controller.signal };
    if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    try {
      let response;
      try { response = await fetch(path, init); } catch (_) { return { network: true, timeout: controller.signal.aborted, status: 0, ok: false, data: null }; }
      let data = null;
      try { data = await response.json(); } catch (_) { if (controller.signal.aborted) return { network: true, timeout: true, status: 0, ok: false, data: null }; }
      return { network: false, timeout: false, status: response.status, ok: response.ok, data };
    } finally { clearTimeout(timer); state.viewRequests.delete(controller); }
  }
  const errorText = (r, fallback) => (r.timeout ? `The local server did not answer within ${REQUEST_TIMEOUT_MS / 1000} seconds.`
    : r.network ? 'The local server could not be reached. Is it still running?' : (r.data && r.data.error && r.data.error.message) || fallback);

  /* ---------- corpus catalog ---------- */
  function renderCatalog() {
    const catalog = state.catalog;
    const list = $('corpus-list');
    clear(list);
    if (!catalog) return;
    const filter = $('corpus-filter').value.trim().toUpperCase();
    const byTicker = new Map();
    for (const filing of catalog.filings) {
      if (filter && !filing.ticker.includes(filter)) continue;
      if (!byTicker.has(filing.ticker)) byTicker.set(filing.ticker, []);
      byTicker.get(filing.ticker).push(filing);
    }
    for (const [ticker, filings] of byTicker) {
      const item = el('li', {}, el('span', { cls: 'ticker', text: ticker }));
      for (const filing of filings) {
        const sections = filing.sections.map(sectionLabel).join(', ');
        const title = `${filing.form} filed ${filing.filing_year}; accession ${filing.accession}; fiscal period end ${filing.fiscal_period_end || 'not recorded'}; sections: ${sections}`;
        item.append(secLink(filing.url, String(filing.filing_year), { cls: 'year-chip', attrs: { title, 'aria-label': `${ticker} filing year ${filing.filing_year}, ${filing.form}. Opens on sec.gov` } }));
      }
      list.append(item);
    }
    if (!byTicker.size) list.append(el('li', { cls: 'hint', text: 'No filings match that ticker.' }));
  }
  async function loadCatalog() {
    const r = await api('GET', '/api/catalog');
    if (!r.ok) { $('corpus-summary').textContent = errorText(r, 'The filing catalog could not be loaded.'); return; }
    state.catalog = r.data;
    $('corpus-summary').textContent = `${r.data.filing_count} filings from ${r.data.company_count} companies. Filing year is the year the filing was submitted, which can differ from the fiscal year it reports.`;
    $('corpus-filter').hidden = false;
    renderCatalog();
    state.samples = r.data.samples || [];
    const box = $('samples');
    clear(box);
    for (const sample of state.samples) {
      box.append(el('button', { cls: 'chip', text: sample.label, attrs: { type: 'button' }, on: { click: () => {
        $('question').value = sample.prompt; updateCount(); $('question').focus(); $('sample-note').textContent = sample.note; hideFormError();
      } } }));
    }
  }

  /* ---------- form ---------- */
  function promptBytes() { return encoder.encode($('question').value).length; }
  function updateCount() {
    const n = promptBytes();
    $('question-count').textContent = `${n} / ${MAX_PROMPT_BYTES} bytes`;
  }
  function showFormError(message) { const box = $('form-error'); box.textContent = message; box.hidden = false; }
  function hideFormError() { $('form-error').hidden = true; $('form-error').textContent = ''; }
  function setRunning(running) {
    $('submit-button').disabled = running;
    $('question').readOnly = running;
    $('cancel-button').hidden = !running;
  }

  async function submit(event) {
    event.preventDefault();
    if (state.submitting) return;
    hideFormError();
    const text = $('question').value;
    if (!text.trim()) { showFormError('Enter an audit question first.'); $('question').focus(); return; }
    if (promptBytes() > MAX_PROMPT_BYTES) { showFormError(`The question is too long (${promptBytes()} bytes; the limit is ${MAX_PROMPT_BYTES}).`); return; }
    state.submitting = true;
    $('submit-button').disabled = true;
    const r = await api('POST', '/api/audits', { prompt: text });  // never retried automatically: a retry could start a duplicate audit
    state.submitting = false;
    if (r.status === 202 && r.data && r.data.id) { startPolling(r.data.id); return; }
    $('submit-button').disabled = false;
    if (r.status === 429) {
      const active = r.data && r.data.error && r.data.error.active_id;
      showFormError('Another audit is already running on this server. Only one audit runs at a time. Wait for it to finish or cancel it, then try again.');
      if (active) startPolling(active);
      return;
    }
    if (r.network || (r.status >= 500 && !(r.data && r.data.error))) {
      showFormError(`${errorText(r, 'The server gave an unexpected reply.')} We could not confirm whether the audit was started. Check "Recent audits" before submitting again so the same audit is not run twice.`);
      refreshHistory();
      return;
    }
    showFormError(errorText(r, 'The audit could not be started.'));
  }
  async function cancel() {
    if (!state.jobId) return;
    $('cancel-button').disabled = true;
    const r = await api('DELETE', `/api/audits/${encodeURIComponent(state.jobId)}`);
    if (!r.ok && r.status !== 202) { $('cancel-button').disabled = false; showFormError(errorText(r, 'The audit could not be cancelled.')); }
  }

  /* ---------- status & polling ---------- */
  function stopTimers() {
    clearTimeout(state.poll); clearInterval(state.tick); state.poll = state.tick = null; state.seq += 1;
    for (const controller of state.viewRequests) controller.abort();  // status requests for a job we are leaving are dropped, not awaited
  }
  /* Switch the view to another job: clear anything belonging to the previous one at once, and return the token that
     invalidates every request still in flight for it. */
  function beginView(id) {
    stopTimers();
    if (state.jobId !== id) {
      clearResults();
      state.job = null;
      $('status-card').hidden = true;
      clear($('status-message')); $('status-prompt').hidden = true;
    }
    state.jobId = id;
    return state.seq;
  }
  function startPolling(id) {
    const seq = beginView(id);
    state.pollFailures = 0;
    setRunning(true);
    $('cancel-button').disabled = false;
    pollOnce(id, seq);
  }
  async function pollOnce(id, seq) {
    const r = await api('GET', `/api/audits/${encodeURIComponent(id)}`, undefined, true);
    if (state.seq !== seq) return;  // another job was selected (or polling restarted) while this request was in flight
    if (r.status === 404) { stopTimers(); setRunning(false); state.jobId = null; showNotice('This audit is no longer stored on the server (finished audits are kept for 30 minutes).', 'warn'); refreshHistory(); return; }
    if (!r.ok) {
      state.pollFailures += 1;
      showNotice(errorText(r, 'Lost contact with the server; retrying…'), 'warn');
      if (!state.job || !state.job.cancel_requested) $('cancel-button').disabled = false;  // cancelling stays possible while the connection is flaky
      state.poll = setTimeout(() => pollOnce(id, seq), Math.min(1000 * 2 ** state.pollFailures, 8000));
      return;
    }
    state.pollFailures = 0;
    renderJob(r.data);
    if (TERMINAL.includes(r.data.status)) { stopTimers(); setRunning(false); refreshHistory(); }
    else state.poll = setTimeout(() => pollOnce(id, seq), 1000);  // next request only after this one finished: no overlap
  }
  function showNotice(text, kind) {
    const card = $('status-card'); card.hidden = false;
    const message = $('status-message'); clear(message);
    message.append(el('div', { cls: `notice ${kind || ''}`, text }));
  }
  function updateElapsed() {
    const live = state.job && !TERMINAL.includes(state.job.status);
    const value = live ? state.base + (performance.now() - state.baseAt) / 1000 : state.base;
    $('status-elapsed').textContent = `Elapsed: ${seconds(value)}`;
  }
  function renderJob(job) {
    state.job = job;
    $('status-card').hidden = false;
    const pill = $('status-pill');
    pill.textContent = job.status === 'running' && job.cancel_requested ? 'Cancelling' : STATUS_LABEL[job.status] || job.status;
    pill.dataset.status = job.status;
    $('status-id').textContent = `Audit ${shortId(job.id)}`;
    $('status-prompt-text').textContent = job.prompt || '';
    $('status-prompt').hidden = job.status === 'completed' && !!job.result;  // a completed result shows it inside the result itself
    state.base = job.elapsed_seconds; state.baseAt = performance.now();
    clearInterval(state.tick);
    updateElapsed();
    if (!TERMINAL.includes(job.status)) state.tick = setInterval(updateElapsed, 250);
    const message = $('status-message'); clear(message);
    const actions = $('status-actions'); clear(actions);
    if (job.status === 'pending') message.append(el('p', { text: 'The audit has been accepted and is starting.' }));
    else if (job.status === 'running') message.append(el('p', { text: job.cancel_requested
      ? 'Cancelling. Waiting for the running steps to stop; a new audit can start once they have.'
      : 'The audit is running. It finishes when the evidence checks are done, or stops at the time limit.' }));
    else if (job.status === 'completed') message.append(el('p', { text: `Finished in ${seconds(job.elapsed_seconds)}.` }));
    else if (job.status === 'cancelled') message.append(el('div', { cls: 'notice warn', text: 'This audit was cancelled. No result is shown.' }));
    else if (job.status === 'failed') {
      const e = job.error || {};
      message.append(el('div', { cls: 'notice bad' }, el('p', { text: e.message || 'The audit failed.' }), e.hint ? el('p', { text: e.hint }) : null, e.code ? el('p', { cls: 'hint' }, 'Error code: ', code(e.code)) : null));
    }
    $('cancel-button').disabled = job.cancel_requested === true;
    if (job.status === 'completed' && job.result) renderResult(job, job.result);
    else if (TERMINAL.includes(job.status)) clearResults(job.status === 'completed' ? 'The server returned no result for this audit.' : null);
  }

  /* ---------- history ---------- */
  async function refreshHistory() {
    const ticket = ++state.historySeq;
    const r = await api('GET', '/api/audits');
    if (ticket !== state.historySeq) return r;  // a newer refresh supersedes this one
    const list = $('history-list'); clear(list);
    if (!r.ok) { $('history-empty').textContent = errorText(r, 'History is unavailable.'); $('history-empty').hidden = false; return r; }
    $('history-empty').hidden = r.data.audits.length > 0;
    for (const audit of r.data.audits) {
      const current = audit.id === state.jobId;
      const button = el('button', { cls: 'history-item', attrs: { type: 'button', 'aria-current': current ? 'true' : 'false' }, on: { click: () => openAudit(audit.id) } },
        el('span', { cls: 'pill', text: audit.status === 'running' && audit.cancel_requested ? 'Cancelling' : STATUS_LABEL[audit.status] || audit.status, attrs: { 'data-status': audit.status } }),
        el('span', { cls: 'history-prompt', text: audit.prompt_preview }),
        el('span', { cls: 'hint', text: `${seconds(audit.elapsed_seconds)} · ${new Date(audit.created_at).toLocaleTimeString()}` }));
      list.append(el('li', {}, button));
    }
    return r;
  }
  async function openAudit(id) {
    const seq = beginView(id);
    showNotice('Loading audit…');
    const r = await api('GET', `/api/audits/${encodeURIComponent(id)}`, undefined, true);
    if (state.seq !== seq) return;  // user selected something else meanwhile: never repaint the older choice
    if (!r.ok) { state.jobId = null; setRunning(false); showNotice(errorText(r, 'That audit is no longer available.'), 'warn'); refreshHistory(); return; }
    renderJob(r.data);
    if (!TERMINAL.includes(r.data.status)) { setRunning(true); $('cancel-button').disabled = r.data.cancel_requested === true; state.poll = setTimeout(() => pollOnce(id, seq), 1000); }
    else setRunning(false);
    refreshHistory();
  }

  /* ---------- results ---------- */
  function clearResults(message) {
    state.result = null; state.evidence = new Map();
    const body = $('results-body'); clear(body);
    if (message) body.append(el('div', { cls: 'empty-state' }, el('p', { text: message })));
    else if (message === undefined) body.append(el('div', { cls: 'empty-state' }, el('p', { text: 'Results will appear here when the audit completes.' })));
    else body.append(el('div', { cls: 'empty-state' }, el('p', { text: 'No result is shown for this audit.' })));
  }

  function citeButton(id, label) {
    const row = state.evidence.get(id);
    return el('button', { cls: 'cite-tag', text: `[${label || (row ? row.tag : id)}]`, attrs: { type: 'button', 'aria-haspopup': 'dialog', title: 'Open the full source passage' }, on: { click: () => openEvidence(id) } });
  }

  function renderResult(job, result) {
    if (state.result === result) return;
    state.result = result;
    state.evidence = new Map(result.evidence.map((row) => [row.id, row]));
    const body = $('results-body'); clear(body);
    body.append(el('section', { cls: 'panel audited', attrs: { 'aria-labelledby': 'audited-h' } }, el('h3', { text: 'Audited question', attrs: { id: 'audited-h' } }),
      el('p', { cls: 'audited-text', text: job.prompt || result.prompt }),
      el('p', { cls: 'hint', text: 'This result answers the question above, not whatever is currently typed in the question box.' }),
      el('button', { cls: 'btn', text: 'Copy into question box', attrs: { type: 'button' }, on: { click: () => {
        if ($('question').readOnly) return;
        $('question').value = job.prompt || result.prompt; updateCount(); $('question').focus();
      } } })));
    body.append(el('p', { cls: 'summary-line', text: `${result.excerpts.length} verified ${result.excerpts.length === 1 ? 'excerpt' : 'excerpts'} · ${result.gaps.length} ${result.gaps.length === 1 ? 'note or gap' : 'notes or gaps'}` }));

    const excerpts = el('section', { cls: 'panel', attrs: { 'aria-labelledby': 'excerpts-h' } }, el('h3', { text: 'Verified excerpts', attrs: { id: 'excerpts-h' } }),
      el('p', { cls: 'hint', text: 'Each excerpt is an exact quotation checked against the stored SEC filing text. The tag opens the full passage.' }));
    if (!result.excerpts.length) excerpts.append(el('p', { text: 'No verified excerpts were produced for this question. Check the evidence gaps below.' }));
    for (const ex of result.excerpts) {
      excerpts.append(el('article', { cls: 'excerpt' },
        el('div', { cls: 'excerpt-scope' }, el('span', { text: ex.company }), el('span', { text: `Filing year ${ex.filing_year}` }), el('span', { cls: 'scope-sub', text: `${ex.form} · ${sectionLabel(ex.section)}` })),
        el('blockquote', {}, `“${ex.quote}”`, citeButton(ex.chunk_id, ex.tag)),
        el('div', { cls: 'source-meta' }, el('span', {}, 'Passage ID: ', code(ex.chunk_id)), el('span', {}, 'Accession: ', code(ex.accession)), secLink(ex.url, 'Open filing on sec.gov'))));
    }
    body.append(excerpts);

    const gaps = el('section', { cls: 'panel gaps', attrs: { 'aria-labelledby': 'gaps-h' } }, el('h3', { text: 'Evidence gaps and notes', attrs: { id: 'gaps-h' } }));
    if (!result.gaps.length) gaps.append(el('p', { text: 'No evidence gaps were reported for this audit.' }));
    else {
      const list = el('ul');
      for (const gap of result.gaps) {
        const topic = gap.subquery !== null && result.plan[gap.subquery] ? result.plan[gap.subquery] : null;
        list.append(el('li', {}, topic ? el('span', { cls: 'gap-topic', text: `Topic ${topic.index + 1} (${topic.filters.ticker} ${topic.filters.year}): ` }) : null, gap.text));
      }
      gaps.append(list);
    }
    body.append(gaps);

    const how = el('details', { attrs: { open: '' } }, el('summary', { text: 'How the audit searched' }));
    const howBody = el('div', { cls: 'details-body' });
    howBody.append(el('p', { cls: 'hint', text: 'The question was split into topics. Each topic searches one fixed filing scope; the search wording may be rewritten between attempts, the filing scope never is.' }));
    for (const topic of result.plan) howBody.append(renderTopic(topic, result));
    how.append(howBody); body.append(how);

    body.append(renderDetails(job, result));
  }

  function filterText(f) {
    return `${f.ticker || 'any company'}, filing year ${f.year || 'any'}, ${f.section ? sectionLabel(f.section) : 'any section'}`;
  }
  function renderTopic(topic, result) {
    const node = el('div', { cls: 'topic' }, el('h4', { text: `Topic ${topic.index + 1}` }),
      el('dl', {}, el('dt', { text: 'What was asked' }), el('dd', { text: topic.goal || '(not recorded)' }),
        el('dt', { text: 'Fixed filing scope' }), el('dd', { text: filterText(topic.filters) + (topic.filing_resolved ? '' : ' — no matching filing found in this corpus') }),
        el('dt', { text: 'Filing accession' }), el('dd', {}, topic.filters.accession ? code(topic.filters.accession) : '(none resolved)')));
    for (const hop of result.hops.filter((h) => h.subquery === topic.index)) {
      const verdict = hop.accepted ? el('span', { cls: 'tag-ok', text: 'Accepted' }) : el('span', { cls: 'tag-retry', text: hop.terminal_reason === 'stalled_rewrite' ? 'Stopped: no evidence and no new wording' : 'Not accepted' });
      const attempt = el('div', { cls: 'attempt' },
        el('div', { cls: 'attempt-head' }, el('span', { text: hop.attempt === 0 ? 'First search' : `Retry ${hop.attempt}` }), el('span', { cls: 'score', text: `Relevance ${hop.score}/5` }), verdict),
        el('p', {}, el('strong', { text: 'Reviewer rationale: ' }), hop.rationale),
        el('p', { cls: 'hint' }, 'Search wording: ', hop.query));
      if (!hop.accepted && hop.proposed_rewrite) attempt.append(el('p', { cls: 'hint' }, 'Proposed next wording: ', hop.proposed_rewrite));
      const carried = hop.context.filter((c) => c.origin === 'carried');
      const fresh = hop.context.filter((c) => c.origin === 'fresh');
      attempt.append(evidenceGroup('Carried over from the previous attempt', carried), evidenceGroup('Newly retrieved and verified', fresh));
      node.append(attempt);
    }
    return node;
  }
  function evidenceGroup(label, items) {
    const box = el('div', { cls: 'evidence-list' }, el('span', { cls: 'origin', text: `${label}:` }));
    if (!items.length) box.append(el('span', { cls: 'origin', text: 'none' }));
    for (const item of items) box.append(citeButton(item.id));
    return box;
  }

  function renderDetails(job, result) {
    const d = result.diagnostics;
    const details = el('details', {}, el('summary', { text: 'Run details (technical)' }));
    const bodyNode = el('div', { cls: 'details-body' });
    bodyNode.append(el('dl', { cls: 'meta-grid' },
      el('dt', { text: 'Audit ID' }), el('dd', {}, code(job.id)), el('dt', { text: 'Total time' }), el('dd', { text: seconds(result.elapsed_seconds) }),
      el('dt', { text: 'Controller states' }), el('dd', { text: d.states.join(' → ') }), el('dt', { text: 'Retrieval calls' }), el('dd', { text: d.retrieval_calls }),
      el('dt', { text: 'Retries' }), el('dd', { text: d.retries })));
    const model = el('table', {}, el('thead', {}, el('tr', {}, ['Step', 'Model', 'Seconds', 'Turns'].map((h) => el('th', { text: h })))),
      el('tbody', {}, d.model_calls.map((c) => el('tr', {}, el('td', { text: c.schema }), el('td', { text: (c.models || []).join(', ') || '—' }), el('td', { text: c.seconds }), el('td', { text: c.num_turns ?? '—' })))));
    bodyNode.append(el('h4', { text: 'Model calls' }), el('div', { cls: 'table-wrap' }, model));
    const hops = el('table', {}, el('thead', {}, el('tr', {}, ['Topic', 'Attempt', 'Score', 'Accepted', 'Retrieved', 'Context', 'Seconds', 'Stop reason'].map((h) => el('th', { text: h })))),
      el('tbody', {}, result.hops.map((h) => el('tr', {}, el('td', { text: h.subquery + 1 }), el('td', { text: h.attempt }), el('td', { text: h.score }), el('td', { text: h.accepted ? 'yes' : 'no' }),
        el('td', { text: h.retrieved_count }), el('td', { text: h.context.length }), el('td', { text: h.elapsed_seconds }), el('td', { text: h.terminal_reason || '—' })))));
    bodyNode.append(el('h4', { text: 'Search attempts' }), el('div', { cls: 'table-wrap' }, hops));
    bodyNode.append(el('div', { cls: 'actions' }, el('button', { cls: 'btn', text: 'Download result as JSON', attrs: { type: 'button' }, on: { click: () => downloadResult(job, result) } })));
    details.append(bodyNode);
    return details;
  }
  function downloadResult(job, result) {
    const blob = new Blob([JSON.stringify({ audit_id: job.id, prompt: job.prompt, result }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = el('a', { attrs: { href: url, download: `finfilings-audit-${shortId(job.id)}.json` } });
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  /* ---------- evidence dialog ---------- */
  function highlightSegments(text, spans) {
    const valid = spans.filter(([s, e]) => Number.isInteger(s) && Number.isInteger(e) && 0 <= s && s < e && e <= text.length).sort((a, b) => a[0] - b[0]);
    const nodes = []; let cursor = 0;
    for (const [s, e] of valid) {
      if (s < cursor) continue;
      if (s > cursor) nodes.push(document.createTextNode(text.slice(cursor, s)));
      nodes.push(el('mark', { text: text.slice(s, e) }));
      cursor = e;
    }
    if (cursor < text.length) nodes.push(document.createTextNode(text.slice(cursor)));
    return nodes;
  }
  function openEvidence(id) {
    const row = state.evidence.get(id);
    if (!row || !state.result) return;
    const quotes = state.result.excerpts.filter((ex) => ex.chunk_id === id);
    $('evidence-title').textContent = row.tag;
    const body = $('evidence-body'); clear(body);
    body.append(el('dl', { cls: 'meta-grid' },
      el('dt', { text: 'Company' }), el('dd', { text: row.company }), el('dt', { text: 'Filing year' }), el('dd', { text: row.filing_year }),
      el('dt', { text: 'Form' }), el('dd', { text: row.form }), el('dt', { text: 'Section' }), el('dd', { text: sectionLabel(row.section) }),
      el('dt', { text: 'Accession' }), el('dd', {}, code(row.accession)), el('dt', { text: 'Passage ID' }), el('dd', {}, code(row.id)),
      el('dt', { text: 'Source' }), el('dd', {}, secLink(row.url, 'Open filing on sec.gov'))));
    if (quotes.length) {
      body.append(el('div', {}, el('h3', { text: 'Quoted in the result' }), el('ul', {}, quotes.map((q) => el('li', { text: q.quote })))));
    } else body.append(el('p', { cls: 'hint', text: 'This passage was retrieved and verified but no quotation from it is published in the result.' }));
    const used = [];
    for (const hop of state.result.hops) for (const c of hop.context) if (c.id === id) used.push(`Topic ${hop.subquery + 1}, ${hop.attempt === 0 ? 'first search' : 'retry ' + hop.attempt} (${c.origin === 'carried' ? 'carried over' : 'newly retrieved'}${hop.accepted ? ', accepted' : ''})`);
    if (used.length) body.append(el('div', {}, el('h3', { text: 'Where it was used' }), el('ul', {}, used.map((u) => el('li', { text: u })))));
    body.append(el('div', {}, el('h3', { text: 'Full retrieved passage' }), el('p', { cls: 'hint', text: quotes.length ? 'Highlighted text is quoted in the result.' : 'No highlighted quotation.' }),
      el('pre', { cls: 'passage', attrs: { tabindex: '0', 'aria-label': 'Full retrieved passage' } }, ...highlightSegments(row.text, quotes.map((q) => q.quote_span)))));
    const dialog = $('evidence-dialog');
    if (!dialog.open) dialog.showModal();
    dialog.scrollTop = 0;
  }

  /* ---------- startup ---------- */
  async function init() {
    $('ask-form').addEventListener('submit', submit);
    $('cancel-button').addEventListener('click', cancel);
    $('question').addEventListener('input', () => { updateCount(); hideFormError(); });
    $('corpus-filter').addEventListener('input', renderCatalog);
    $('evidence-close').addEventListener('click', () => $('evidence-dialog').close());
    $('evidence-dialog').addEventListener('click', (event) => { if (event.target === $('evidence-dialog')) $('evidence-dialog').close(); });
    updateCount();
    clearResults();
    loadCatalog();
    const health = await api('GET', '/api/health');
    if (!health.ok) showNotice(health.network ? errorText(health, '') : 'The retrieval service is not ready, so new audits are refused until it is. Check the launcher output.', 'bad');
    const history = await refreshHistory();
    if (history.ok && history.data.active_id) openAudit(history.data.active_id);
  }
  init();
})();
