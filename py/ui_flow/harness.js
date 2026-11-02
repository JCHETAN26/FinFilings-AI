'use strict';
/* Frontend flow tests for ui/app.js against a minimal fake DOM and a scripted fetch (TEST DOUBLES). They check result/job
   association, stale-response races, request timeouts and cancel availability. They do not test layout, focus or rendering
   in a real browser. Run: node py/ui_flow/harness.js */
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'ui/index.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'ui/app.js'), 'utf8');

class TextNode { constructor(text) { this.data = String(text); } get textContent() { return this.data; } }
class Element {
  constructor(tag) { Object.assign(this, { tag, children: [], attrs: {}, listeners: {}, dataset: {}, hidden: false, disabled: false, readOnly: false, value: '', className: '', open: false, scrollTop: 0 }); }
  append(...nodes) { for (const n of nodes) this.children.push(typeof n === 'string' ? new TextNode(n) : n); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  set textContent(v) { this.children = [new TextNode(v)]; }
  get textContent() { return this.children.map((c) => c.textContent).join(''); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  async dispatch(type) { for (const fn of this.listeners[type] || []) await fn({ preventDefault() {}, target: this }); }
  click() { return this.dispatch('click'); }
  focus() {} remove() {} showModal() { this.open = true; } close() { this.open = false; }
  all(pred, out = []) { for (const c of this.children) { if (c instanceof Element) { if (pred(c)) out.push(c); c.all(pred, out); } } return out; }
}

function deferred() { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; }
const flush = async () => { for (let i = 0; i < 12; i += 1) await new Promise((r) => setImmediate(r)); };
const reply = (status, data) => ({ status, ok: status >= 200 && status < 300, json: async () => data });
function hang(init) {  // a request that only ends when the app aborts it
  return new Promise((_, reject) => init.signal.addEventListener('abort', () => reject(new Error('aborted'))));
}

function job(id, status, prompt, result) {
  return { id, status, prompt, created_at: '2026-11-01T00:00:00Z', elapsed_seconds: 2, cancel_requested: false, error: null, ...(result ? { result } : {}) };
}
function result(prompt, quote) {
  const text = `Passage text containing ${quote} and more words.`;
  const start = text.indexOf(quote);
  return {
    prompt, elapsed_seconds: 2, gaps: [], diagnostics: { states: ['Plan'], retrieval_calls: 1, retries: 0, model_calls: [] },
    excerpts: [{ n: 1, quote, quote_span: [start, start + quote.length], tag: 'TST-1', chunk_id: 'c1', accession: '0000000000-00-000001', url: 'https://www.sec.gov/Archives/edgar/data/1/2/x.htm', company: 'TST', filing_year: 2024, form: '10-K', section: 'Item 1A' }],
    plan: [{ index: 0, goal: 'g', query: 'q', filters: { ticker: 'TST', year: 2024, section: '', accession: '000000000000000001' }, filing_resolved: true }],
    hops: [{ subquery: 0, attempt: 0, query: 'q', goal: 'g', score: 4, accepted: true, rationale: 'r', proposed_rewrite: '', terminal_reason: null, retrieved_count: 1, context: [{ id: 'c1', origin: 'fresh' }], elapsed_seconds: 1 }],
    evidence: [{ id: 'c1', tag: 'TST-1', accession: '0000000000-00-000001', url: 'https://www.sec.gov/Archives/edgar/data/1/2/x.htm', company: 'TST', filing_year: 2024, form: '10-K', section: 'Item 1A', text }],
  };
}
const A = 'aaaaaaaa-0000-4000-8000-000000000001', B = 'bbbbbbbb-0000-4000-8000-000000000002', C = 'cccccccc-0000-4000-8000-000000000003';

async function boot(handler) {
  const elements = new Map();
  const tagFor = (id) => (html.match(new RegExp(`<(\\w+)[^>]*\\sid="${id}"([^>]*)>`)) || []);
  for (const id of html.match(/\sid="([^"]+)"/g).map((m) => m.slice(5, -1))) {
    const found = tagFor(id), node = new Element(found[1] || 'div');
    node.hidden = /\shidden(\s|$|=)/.test(found[0] || '');
    elements.set(id, node);
  }
  const timers = []; let timerId = 0;
  const calls = [];
  Object.assign(globalThis, {
    document: { getElementById: (id) => { if (!elements.has(id)) throw new Error(`script uses unknown id ${id}`); return elements.get(id); }, createElement: (tag) => new Element(tag), createTextNode: (t) => new TextNode(t), body: new Element('body') },
    performance: { now: () => Date.now() },
    setTimeout: (fn, ms) => { timers.push({ id: ++timerId, fn, ms, kind: 'timeout' }); return timerId; },
    setInterval: (fn, ms) => { timers.push({ id: ++timerId, fn, ms, kind: 'interval' }); return timerId; },
    clearTimeout: (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); },
    clearInterval: (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); },
    fetch: (url, init) => { const call = { method: init.method, url, body: init.body, headers: init.headers }; calls.push(call); return handler(init.method, url, init, call); },
  });
  vm.runInThisContext(source);
  await flush();
  const $ = (id) => elements.get(id);
  return {
    $, calls, timers, flush,
    text: (id) => $(id).textContent,
    advance: async (ms) => { for (const t of timers.filter((x) => x.kind === 'timeout' && x.ms <= ms)) { timers.splice(timers.indexOf(t), 1); t.fn(); } await flush(); },
    historyButtons: () => $('history-list').all((e) => e.tag === 'button'),
    count: (method, prefix) => calls.filter((c) => c.method === method && c.url.startsWith(prefix)).length,
  };
}

const baseRoutes = (extra = {}) => (method, url, init) => {
  const key = `${method} ${url}`;
  if (extra[key]) return extra[key](init);
  if (key === 'GET /api/catalog') return Promise.resolve(reply(200, { filing_count: 1, company_count: 1, filings: [], samples: [] }));
  if (key === 'GET /api/health') return Promise.resolve(reply(200, { status: 'ready' }));
  if (key === 'GET /api/audits') return Promise.resolve(reply(200, { audits: extra.history || [], active_id: null }));
  return Promise.resolve(reply(404, { error: { message: `unscripted ${key}` } }));
};
const summary = (id, prompt) => ({ id, status: 'completed', prompt_preview: prompt, elapsed_seconds: 2, created_at: '2026-11-01T00:00:00Z', cancel_requested: false });

const tests = {
  async 'opening history shows the audited question and leaves the editable draft alone'() {
    const world = await boot(baseRoutes({ history: [summary(A, 'OLD')], [`GET /api/audits/${A}`]: () => Promise.resolve(reply(200, job(A, 'completed', 'OLD QUESTION about NVDA', result('OLD QUESTION about NVDA', 'old verified quote')))) }));
    world.$('question').value = 'NEW DRAFT';
    await world.historyButtons()[0].click(); await world.flush();
    const shown = world.text('results-body');
    assert.match(shown, /Audited question/); assert.match(shown, /OLD QUESTION about NVDA/); assert.match(shown, /old verified quote/);
    assert.strictEqual(world.$('question').value, 'NEW DRAFT');
  },
  async 'starting a different job clears the previous results immediately'() {
    const next = deferred();
    const world = await boot(baseRoutes({ history: [summary(A, 'OLD')], [`GET /api/audits/${A}`]: () => Promise.resolve(reply(200, job(A, 'completed', 'OLD QUESTION', result('OLD QUESTION', 'old verified quote')))),
      'POST /api/audits': () => Promise.resolve(reply(202, { id: B })), [`GET /api/audits/${B}`]: () => next.promise }));
    await world.historyButtons()[0].click(); await world.flush();
    assert.match(world.text('results-body'), /old verified quote/);
    world.$('question').value = 'a brand new question';
    await world.$('ask-form').dispatch('submit'); await world.flush();  // job B accepted, its status request still pending
    assert.doesNotMatch(world.text('results-body'), /old verified quote|OLD QUESTION/);
    assert.strictEqual(world.$('status-card').hidden, true);
  },
  async 'following a 429 to the active job clears stale results at once'() {
    const pending = deferred();
    const world = await boot(baseRoutes({ history: [summary(A, 'OLD')], [`GET /api/audits/${A}`]: () => Promise.resolve(reply(200, job(A, 'completed', 'OLD QUESTION', result('OLD QUESTION', 'old verified quote')))),
      'POST /api/audits': () => Promise.resolve(reply(429, { error: { code: 'busy', active_id: C } })), [`GET /api/audits/${C}`]: () => pending.promise }));
    await world.historyButtons()[0].click(); await world.flush();
    world.$('question').value = 'q';
    await world.$('ask-form').dispatch('submit'); await world.flush();
    assert.doesNotMatch(world.text('results-body'), /old verified quote/);
    assert.match(world.text('form-error'), /already running/);
    pending.resolve(reply(200, job(C, 'running', 'ACTIVE QUESTION')));
    await world.flush();
    assert.match(world.text('status-prompt-text'), /ACTIVE QUESTION/);
    assert.strictEqual(world.$('status-pill').textContent, 'Running');
  },
  async 'a slow response for a previously selected job never repaints over the current one'() {
    const slowA = deferred();
    const world = await boot(baseRoutes({ history: [summary(A, 'A'), summary(B, 'B')], [`GET /api/audits/${A}`]: () => slowA.promise,
      [`GET /api/audits/${B}`]: () => Promise.resolve(reply(200, job(B, 'completed', 'B QUESTION', result('B QUESTION', 'b verified quote')))) }));
    const [first, second] = world.historyButtons();
    first.click(); await world.flush();  // A requested, response outstanding
    await second.click(); await world.flush();  // user switches to B, which completes
    slowA.resolve(reply(200, job(A, 'completed', 'A QUESTION', result('A QUESTION', 'a verified quote'))));
    await world.flush();
    const shown = world.text('results-body');
    assert.match(shown, /B QUESTION/); assert.doesNotMatch(shown, /A QUESTION|a verified quote/);
    assert.match(world.text('status-id'), /bbbbbbbb/);
  },
  async 'a hung submit is aborted after 15 s, never retried, and history is refreshed'() {
    const world = await boot(baseRoutes({ 'POST /api/audits': (init) => hang(init) }));
    assert.strictEqual(world.timers.filter((t) => t.ms === 15000).length, 0, 'finished requests must clear their timeout');
    world.$('question').value = 'audit something';
    const before = world.count('GET', '/api/audits');
    const submitting = world.$('ask-form').dispatch('submit'); await world.flush();
    assert.strictEqual(world.timers.filter((t) => t.ms === 15000).length, 1);
    await world.advance(15000); await submitting; await world.flush();
    assert.match(world.text('form-error'), /did not answer within 15 seconds/); assert.match(world.text('form-error'), /could not confirm/);
    assert.strictEqual(world.count('POST', '/api/audits'), 1);
    assert.ok(world.count('GET', '/api/audits') > before, 'history must be refreshed after an ambiguous submit');
    assert.strictEqual(world.$('submit-button').disabled, false);
    await world.advance(120000);
    assert.strictEqual(world.count('POST', '/api/audits'), 1, 'no automatic POST retry');
  },
  async 'cancel stays available when the cancel request itself fails'() {
    const world = await boot(baseRoutes({ history: [{ ...summary(A, 'RUN'), status: 'running' }], [`GET /api/audits/${A}`]: () => Promise.resolve(reply(200, job(A, 'running', 'RUNNING QUESTION'))),
      [`DELETE /api/audits/${A}`]: () => Promise.reject(new Error('network down')) }));
    await world.historyButtons()[0].click(); await world.flush();
    assert.strictEqual(world.$('cancel-button').hidden, false);
    await world.$('cancel-button').click(); await world.flush();
    assert.strictEqual(world.$('cancel-button').disabled, false);
    assert.match(world.text('form-error'), /could not be reached/);
  },
  async 'polling failures keep cancel enabled and never overlap requests'() {
    let inflight = 0, peak = 0, fail = false;
    const world = await boot(baseRoutes({ history: [{ ...summary(A, 'RUN'), status: 'running' }], [`GET /api/audits/${A}`]: async (init) => {
      let live = true; const end = () => { if (live) { live = false; inflight -= 1; } };
      inflight += 1; peak = Math.max(peak, inflight); init.signal.addEventListener('abort', end);  // an aborted request is no longer in flight
      await new Promise((r) => setImmediate(r));
      if (init.signal.aborted) throw new Error('aborted');
      end();
      return fail ? reply(503, null) : reply(200, job(A, 'running', 'RUNNING QUESTION'));
    } }));
    const [button] = world.historyButtons();
    button.click(); await button.click(); await world.flush();  // the same job selected twice in quick succession
    fail = true;
    for (let i = 0; i < 4; i += 1) await world.advance(10000);
    assert.strictEqual(peak, 1, 'at most one status request in flight');
    assert.strictEqual(world.$('cancel-button').disabled, false);
    assert.strictEqual(world.timers.filter((t) => t.kind === 'timeout').length, 1, 'exactly one pending poll chain');
  },
};

(async () => {
  let failed = 0;
  for (const [name, fn] of Object.entries(tests)) {
    try { await fn(); console.log(`ok   ${name}`); } catch (error) { failed += 1; console.log(`FAIL ${name}\n${error.stack}`); }
  }
  process.exit(failed ? 1 : 0);
})();
