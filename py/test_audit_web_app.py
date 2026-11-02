"""HTTP surface: admission, request bounds, browser-attack guards, static assets and fixture end-to-end binding.

Every agent/model/backend here is a scripted TEST DOUBLE or a synthetic fixture corpus. Nothing here measures retrieval or
answer quality, and no timing here is a benchmark.
"""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx
from fastapi.testclient import TestClient
from agent.provenance import ProvenanceRegistry
from agent.state_machine import Agent
from audit_web import limits
from audit_web.app import allowed_hosts_for, build_catalog, create_app
from audit_web.manager import AuditManager
from audit_web.projection import project_result
from audit_web_testkit import BlockingAgent, InstantAgent, ScriptedBackend, ScriptedModel, PROMPT, write_fixture_corpus

UI = Path(__file__).resolve().parents[1] / 'ui'
BASE = 'http://127.0.0.1:8766'
HOSTS = allowed_hosts_for('127.0.0.1', 8766)
GOOD = {'X-Requested-With': 'finfilings-ui', 'Content-Type': 'application/json'}


class Fixture:
    """A fixture registry (real ProvenanceRegistry over a tiny synthetic corpus) shared by the module."""
    directory = registry = catalog = None


def setUpModule():
    Fixture.directory = tempfile.TemporaryDirectory()
    corpus, manifest = write_fixture_corpus(Path(Fixture.directory.name))
    Fixture.registry = ProvenanceRegistry(corpus, manifest)
    Fixture.catalog = build_catalog(Fixture.registry)


def tearDownModule():
    Fixture.registry.close()
    Fixture.directory.cleanup()


async def ready():
    await asyncio.sleep(0)
    return 'ready'


class AppCase(unittest.IsolatedAsyncioTestCase):
    probe = staticmethod(ready)

    def build(self, agent, **manager_kwargs):
        self.manager = AuditManager(agent, project_result, **manager_kwargs)
        self.app = create_app(manager=self.manager, catalog=Fixture.catalog, probe=self.probe, ui_dir=UI, allowed_hosts=HOSTS)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.manager.shutdown)
        return self.client

    async def post(self, prompt='Audit TST filing year 2024: suppliers.', headers=None, **kwargs):
        return await self.client.post('/api/audits', headers=headers or GOOD, content=json.dumps({'prompt': prompt}) if 'content' not in kwargs else None, **kwargs)

    async def wait(self, job_id, terminal=('completed', 'failed', 'cancelled')):
        for _ in range(400):
            body = (await self.client.get(f'/api/audits/{job_id}')).json()
            if body['status'] in terminal:
                return body
            await asyncio.sleep(0.01)
        self.fail('job did not finish')


class Admission(AppCase):
    async def test_simultaneous_posts_accept_exactly_one_and_reject_the_rest_with_retry_hint(self):
        self.build(BlockingAgent())
        responses = await asyncio.gather(*(self.post(f'Audit TST filing year 2024: topic {i}') for i in range(20)))
        codes = sorted(r.status_code for r in responses)
        self.assertEqual(codes, [202] + [429] * 19)
        for r in responses:
            if r.status_code == 429:
                self.assertEqual(r.headers['retry-after'], str(limits.RETRY_AFTER_SECONDS))
                self.assertEqual(r.json()['error']['code'], 'busy')
                self.assertTrue(r.json()['error']['active_id'])
        accepted = next(r for r in responses if r.status_code == 202).json()
        self.assertEqual(accepted['status_url'], f"/api/audits/{accepted['id']}")

    async def test_probe_failure_blocks_admission(self):
        async def down():
            return 'fingerprint_mismatch'
        self.probe = staticmethod(down)
        self.build(InstantAgent())
        r = await self.post()
        self.assertEqual((r.status_code, r.json()['error']['code']), (503, 'backend_unavailable'))
        self.assertIsNone(self.manager.active_id)
        self.assertEqual((await self.client.get('/api/audits')).json()['audits'], [])
        health = await self.client.get('/api/health')
        self.assertEqual((health.status_code, health.json()['backend']), (503, 'fingerprint_mismatch'))

    async def test_health_reports_limits_without_paths_or_secrets(self):
        self.build(InstantAgent())
        body = (await self.client.get('/api/health')).json()
        self.assertEqual(body['status'], 'ready')
        self.assertEqual(body['limits']['max_prompt_bytes'], 8192)
        blob = json.dumps(body)
        for forbidden in ('/', '\\', 'sha256', 'token', 'key'):
            self.assertNotIn(forbidden, blob.replace('max_', '').replace('_', ''), forbidden)

    async def test_status_and_result_consistency(self):
        agent = BlockingAgent()
        self.build(agent)
        job_id = (await self.post()).json()['id']
        await agent.started.wait()
        running = (await self.client.get(f'/api/audits/{job_id}')).json()
        self.assertEqual(running['status'], 'running')
        self.assertNotIn('result', running)
        cancel = await self.client.delete(f'/api/audits/{job_id}', headers=GOOD)
        self.assertEqual(cancel.status_code, 202)
        done = await self.wait(job_id)
        self.assertEqual(done['status'], 'cancelled')
        self.assertNotIn('result', done)
        self.assertIsNone(done['error'])
        again = await self.client.delete(f'/api/audits/{job_id}', headers=GOOD)
        self.assertEqual((again.status_code, again.json()['status']), (200, 'cancelled'))

    async def test_failed_job_is_sanitized_and_has_no_result(self):
        self.build(InstantAgent(RuntimeError('Claude generation failed, exit=1 /home/u/.claude/secret')))
        job_id = (await self.post()).json()['id']
        done = await self.wait(job_id)
        self.assertEqual(done['status'], 'failed')
        self.assertNotIn('result', done)
        self.assertEqual(done['error']['code'], 'model_unavailable')
        self.assertNotIn('secret', json.dumps(done))

    async def test_unknown_malformed_and_expired_ids_are_404(self):
        self.build(InstantAgent())
        for bad in ('00000000-0000-4000-8000-000000000000', 'nope', '..%2F..%2Fetc%2Fpasswd', 'A' * 36, '00000000-0000-1000-8000-000000000000'):
            self.assertEqual((await self.client.get(f'/api/audits/{bad}')).status_code, 404, bad)
            self.assertEqual((await self.client.delete(f'/api/audits/{bad}', headers=GOOD)).status_code, 404, bad)

    async def test_history_is_bounded_and_listing_omits_results(self):
        self.build(InstantAgent())
        ids = []
        for i in range(10):
            job_id = (await self.post(f'Audit TST filing year 2024: {i}')).json()['id']
            await self.wait(job_id)
            ids.append(job_id)
        listing = (await self.client.get('/api/audits')).json()
        self.assertEqual(len(listing['audits']), 8)
        self.assertTrue(all('result' not in a and 'prompt' not in a for a in listing['audits']))
        self.assertEqual((await self.client.get(f'/api/audits/{ids[0]}')).status_code, 404)


class RequestBounds(AppCase):
    async def asyncSetUp(self):
        self.build(InstantAgent())

    async def test_content_length_over_limit_is_413_before_parsing(self):
        r = await self.client.post('/api/audits', headers=GOOD, content=b'{' + b'a' * (limits.MAX_BODY_BYTES + 1))
        self.assertEqual((r.status_code, r.json()['error']['code']), (413, 'body_too_large'))

    async def test_chunked_body_over_limit_is_413(self):
        async def stream():
            for _ in range(10):
                yield b'x' * 4096
        request = self.client.build_request('POST', '/api/audits', headers=GOOD, content=stream())
        self.assertNotIn('content-length', request.headers)  # genuinely chunked
        r = await self.client.send(request)
        self.assertEqual(r.status_code, 413)
        self.assertIsNone(self.manager.active_id)

    async def test_raw_asgi_chunked_without_content_length_is_bounded_while_streaming(self):
        sent, pulls = [], 0

        async def receive():
            nonlocal pulls
            pulls += 1
            return {'type': 'http.request', 'body': b'y' * 8192, 'more_body': pulls < 1000}

        async def send(message):
            sent.append(message)
        scope = dict(type='http', method='POST', path='/api/audits', raw_path=b'/api/audits', query_string=b'', http_version='1.1', scheme='http', server=('127.0.0.1', 8766),
                     client=('127.0.0.1', 5), root_path='', headers=[(b'host', b'127.0.0.1:8766'), (b'x-requested-with', b'finfilings-ui'),
                     (b'content-type', b'application/json'), (b'transfer-encoding', b'chunked')])
        await self.app(scope, receive, send)
        self.assertEqual(sent[0]['status'], 413)
        self.assertLessEqual(pulls, 4)  # stopped reading right after the bound; did not drain 8 MB

    async def test_body_exactly_at_limit_is_not_rejected_as_too_large(self):
        pad = 'a' * (limits.MAX_BODY_BYTES - len('{"prompt":""}'))
        r = await self.client.post('/api/audits', headers=GOOD, content=json.dumps({'prompt': pad}, separators=(',', ':')))
        self.assertEqual(r.status_code, 422)  # prompt bytes (not body bytes) exceed 8 KiB
        self.assertEqual(r.json()['error']['code'], 'prompt_too_long')

    async def test_prompt_validation(self):
        cases = [
            (b'not json', 400, 'invalid_json'), (b'', 400, 'invalid_json'), (b'[]', 422, 'invalid_request'), (b'{"prompt": 5}', 422, 'invalid_request'),
            (b'{"prompt":"x","extra":1}', 422, 'invalid_request'), (b'{}', 422, 'invalid_request'), (b'{"prompt":"   \\n "}', 422, 'prompt_empty'),
            (b'{"prompt":"bad \\u0000 nul"}', 422, 'invalid_prompt'), (b'{"prompt":"lone \\ud800 surrogate"}', 422, 'invalid_prompt'),
            (json.dumps({'prompt': '€' * 2800}, ensure_ascii=False).encode(), 422, 'prompt_too_long'),  # 8400 UTF-8 bytes in 2800 characters
            (b'[' * 5000, 400, 'invalid_json'),
        ]
        for body, status, code in cases:
            r = await self.client.post('/api/audits', headers=GOOD, content=body)
            self.assertEqual((r.status_code, r.json()['error']['code']), (status, code), body[:40])
        self.assertIsNone(self.manager.active_id)

    async def test_prompt_of_exactly_8192_bytes_is_accepted(self):
        r = await self.post('a' * limits.MAX_PROMPT_BYTES)
        self.assertEqual(r.status_code, 202)


class BrowserAttacks(AppCase):
    async def asyncSetUp(self):
        self.build(InstantAgent())

    async def test_host_header_allowlist_blocks_dns_rebinding(self):
        for host in ('evil.example', 'evil.example:8766', '127.0.0.1', '127.0.0.1:9999', '127.0.0.1.evil.example:8766', '0.0.0.0:8766', '', 'localhost:8766@evil.example'):
            for path in ('/', '/api/health', '/api/catalog'):
                r = await self.client.get(path, headers={'Host': host})
                self.assertEqual((r.status_code, r.json()['error']['code']), (403, 'bad_host'), (host, path))
        for host in ('127.0.0.1:8766', 'localhost:8766'):
            self.assertEqual((await self.client.get('/api/catalog', headers={'Host': host})).status_code, 200)

    async def test_origin_must_be_same_origin(self):
        for origin in ('http://evil.example', 'null', 'https://127.0.0.1:8766', 'http://127.0.0.1:1', 'http://localhost:8766.evil.example'):
            for method, extra in (('GET', {}), ('POST', GOOD)):
                r = await self.client.request(method, '/api/catalog' if method == 'GET' else '/api/audits', headers={**extra, 'Origin': origin}, content=None if method == 'GET' else b'{"prompt":"x"}')
                self.assertEqual((r.status_code, r.json()['error']['code']), (403, 'bad_origin'), (origin, method))
        ok = await self.client.post('/api/audits', headers={**GOOD, 'Origin': BASE}, content=b'{"prompt":"Audit TST filing year 2024: suppliers."}')
        self.assertEqual(ok.status_code, 202)

    async def test_mutations_require_json_content_type_and_same_origin_header(self):
        body = b'{"prompt":"Audit TST filing year 2024: suppliers."}'
        no_header = await self.client.post('/api/audits', headers={'Content-Type': 'application/json'}, content=body)
        self.assertEqual((no_header.status_code, no_header.json()['error']['code']), (403, 'csrf_header_required'))
        wrong_value = await self.client.post('/api/audits', headers={**GOOD, 'X-Requested-With': 'XMLHttpRequest'}, content=body)
        self.assertEqual(wrong_value.status_code, 403)
        for content_type in ('text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data; boundary=x', 'application/jsonx', ''):
            r = await self.client.post('/api/audits', headers={'X-Requested-With': 'finfilings-ui', 'Content-Type': content_type}, content=body)
            self.assertEqual((r.status_code, r.json()['error']['code']), (415, 'unsupported_media_type'), content_type)
        cross = await self.client.post('/api/audits', headers={**GOOD, 'Sec-Fetch-Site': 'cross-site'}, content=body)
        self.assertEqual((cross.status_code, cross.json()['error']['code']), (403, 'cross_site'))
        delete = await self.client.delete('/api/audits/00000000-0000-4000-8000-000000000000')
        self.assertEqual(delete.status_code, 403)
        self.assertIsNone(self.manager.active_id)
        charset = await self.client.post('/api/audits', headers={**GOOD, 'Content-Type': 'application/json; charset=utf-8'}, content=body)
        self.assertEqual(charset.status_code, 202)

    async def test_duplicate_security_headers_are_rejected(self):
        sent = []  # httpx merges duplicate headers, so the duplicate is built at the ASGI level

        async def receive():
            return {'type': 'http.request', 'body': b'{}', 'more_body': False}

        async def send(message):
            sent.append(message)
        scope = dict(type='http', method='GET', path='/', raw_path=b'/', query_string=b'', http_version='1.1', scheme='http', server=('127.0.0.1', 8766), client=('x', 1), root_path='',
                     headers=[(b'host', b'127.0.0.1:8766'), (b'host', b'evil.example')])
        await self.app(scope, receive, send)
        self.assertEqual(sent[0]['status'], 400)

    async def test_no_cors_is_ever_granted(self):
        preflight = await self.client.options('/api/audits', headers={'Origin': BASE, 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type'})
        self.assertEqual(preflight.status_code, 405)
        evil = await self.client.options('/api/audits', headers={'Origin': 'http://evil.example', 'Access-Control-Request-Method': 'POST'})
        self.assertEqual(evil.status_code, 403)
        for r in (preflight, evil, await self.client.get('/api/catalog'), await self.client.get('/')):
            self.assertFalse([h for h in r.headers if h.lower().startswith('access-control-')])

    async def test_security_headers_on_every_response_including_rejections(self):
        responses = [await self.client.get('/'), await self.client.get('/api/health'), await self.client.get('/nope'), await self.client.get('/', headers={'Host': 'evil.example'}),
                     await self.client.post('/api/audits', headers={'Content-Type': 'text/plain'}, content=b'x')]
        for r in responses:
            csp = r.headers['content-security-policy']
            self.assertIn("script-src 'self'", csp)
            self.assertIn("default-src 'none'", csp)
            self.assertNotIn('unsafe', csp)
            self.assertNotIn('*', csp)
            self.assertEqual(r.headers['x-content-type-options'], 'nosniff')
            self.assertEqual(r.headers['x-frame-options'], 'DENY')
            self.assertEqual(r.headers['referrer-policy'], 'no-referrer')
            self.assertEqual(r.headers['cache-control'], 'no-store')

    async def test_websocket_and_unknown_scope_are_refused(self):
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {'type': 'websocket.connect'}
        await self.app(dict(type='websocket', path='/ws', headers=[]), receive, send)
        self.assertEqual(sent, [{'type': 'websocket.close', 'code': 1008}])


class StaticAssets(AppCase):
    async def asyncSetUp(self):
        self.build(InstantAgent())

    async def test_only_the_four_fixed_assets_are_served_with_correct_types(self):
        expected = {'/': 'text/html', '/app.js': 'text/javascript', '/app.css': 'text/css', '/favicon.svg': 'image/svg+xml'}
        for path, mime in expected.items():
            r = await self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertTrue(r.headers['content-type'].startswith(mime), path)
        self.assertEqual((await self.client.get('/app.js')).content, (UI / 'app.js').read_bytes())

    async def test_missing_paths_traversal_and_docs_routes_are_404(self):
        for path in ('/index.html', '/ui/app.js', '/static/app.js', '/../README.md', '/%2e%2e/README.md', '/..%2fREADME.md', '/app.js/../../py/audit_web/app.py',
                     '/etc/passwd', '/%252e%252e/etc/passwd', '/docs', '/redoc', '/openapi.json', '/app.js%00.png', '/.git/config', '/ui/../CLAUDE.md', '/api', '/api/'):
            r = await self.client.get(path)
            self.assertEqual(r.status_code, 404, path)
            self.assertEqual(r.json()['error']['code'], 'not_found')
            self.assertNotIn('root:', r.text)

    async def test_missing_asset_fails_startup_explicitly(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(FileNotFoundError):
                create_app(manager=self.manager, catalog=Fixture.catalog, probe=ready, ui_dir=Path(empty), allowed_hosts=HOSTS)


class Catalog(AppCase):
    async def test_catalog_is_the_registry_catalog_with_catalog_derived_sample_notes(self):
        self.build(InstantAgent())
        body = (await self.client.get('/api/catalog')).json()
        self.assertEqual((body['filing_count'], body['company_count']), (1, 1))
        filing = body['filings'][0]
        self.assertEqual((filing['ticker'], filing['filing_year'], filing['accession'], filing['fiscal_period_end'], filing['sections']), ('TST', 2024, '0000000000-00-000007', 'December 31, 2023', ['Item 1A']))
        self.assertTrue(filing['url'].startswith('https://www.sec.gov/Archives/'))
        self.assertEqual(len(body['samples']), 3)
        notes = {s['id']: s['note'] for s in body['samples']}
        self.assertIn('NVDA 2024', notes['single-filing'])  # fixture corpus lacks the real filings: computed, not assumed
        self.assertTrue(all(s['prompt'] for s in body['samples']))

    def test_catalog_bound_is_enforced(self):
        class Huge:
            sources = {}

            def catalog(self):
                return [dict(ticker='T', filing_year=2024, accession='0' * 18, fiscal_period_end=None, form='10-K', sections=[])] * (limits.MAX_CATALOG_FILINGS + 1)
        with self.assertRaises(ValueError):
            build_catalog(Huge())

    def test_sample_prompts_match_the_recorded_regression_prompts(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bench_kit'))
        import eval_agent
        from audit_web.samples import SAMPLES
        recorded = {case['id']: case['prompt'] for case in eval_agent.cases()}
        self.assertEqual(SAMPLES[0]['prompt'], recorded['audit03'])
        self.assertEqual(SAMPLES[1]['prompt'], recorded['audit01'])
        self.assertTrue(all(len(s['prompt'].encode()) < limits.MAX_PROMPT_BYTES for s in SAMPLES))


class FixtureEndToEnd(AppCase):
    """Real Agent, real ProvenanceRegistry over the synthetic corpus; only the model and retrieval backend are scripted."""

    def real_agent(self, mode='legacy', **kwargs):
        return Agent(ScriptedModel(**kwargs), ScriptedBackend(), Fixture.registry, mode=mode)

    async def test_fast_mode_binds_citations_to_evidence_and_reports_its_real_route(self):
        self.build(self.real_agent('fast'))
        done = await self.wait((await self.post(PROMPT)).json()['id'])
        self.assertEqual(done['status'], 'completed', done.get('error'))
        result = done['result']
        self.assertEqual((result['diagnostics']['execution_mode'], result['diagnostics']['synthesis_route']), ('fast', 'host_quotes'))
        self.assertEqual([c['schema'] for c in result['diagnostics']['model_calls']], ['Plan', 'CritiqueWithQuotes', 'CritiqueWithQuotes'])
        evidence = {row['id']: row for row in result['evidence']}
        self.assertEqual(len(result['excerpts']), 2)
        for excerpt in result['excerpts']:
            start, end = excerpt['quote_span']
            self.assertEqual(' '.join(evidence[excerpt['chunk_id']]['text'][start:end].split()), excerpt['quote'])

    async def test_fast_mode_falls_back_with_one_synthesis_call_when_the_critic_proposes_no_quotes(self):
        self.build(self.real_agent('fast', quotes=False))
        done = await self.wait((await self.post(PROMPT)).json()['id'])
        self.assertEqual(done['status'], 'completed', done.get('error'))
        diag = done['result']['diagnostics']
        self.assertEqual(diag['synthesis_route'], 'fallback')
        self.assertEqual([c['schema'] for c in diag['model_calls']], ['Plan', 'CritiqueWithQuotes', 'CritiqueWithQuotes', 'HandleDraft'])
        self.assertEqual(len(done['result']['excerpts']), 2)

    async def test_citations_and_evidence_bind_to_the_real_result(self):
        self.build(self.real_agent())
        job_id = (await self.post(PROMPT)).json()['id']
        done = await self.wait(job_id)
        self.assertEqual(done['status'], 'completed', done.get('error'))
        result = done['result']
        self.assertEqual(len(result['excerpts']), 2)
        evidence = {row['id']: row for row in result['evidence']}
        for excerpt in result['excerpts']:
            row = evidence[excerpt['chunk_id']]
            start, end = excerpt['quote_span']
            self.assertEqual(' '.join(row['text'][start:end].split()), excerpt['quote'])
            self.assertEqual(row['url'], excerpt['url'])
            self.assertEqual(Fixture.registry.sources[excerpt['chunk_id'].split('#')[0]]['url'], excerpt['url'])  # trusted registry URL
        for hop in result['hops']:
            self.assertTrue(all(c['id'] in evidence for c in hop['context']))
        self.assertEqual([p['filters']['accession'] for p in result['plan']], ['000000000000000007'] * 2)
        self.assertEqual(result['gaps'], [])
        self.assertEqual({c['schema'] for c in result['diagnostics']['model_calls']}, {'Plan', 'Critique', 'HandleDraft'})
        raw = json.dumps(done)
        self.assertEqual(json.loads(raw)['result'], result)

    async def test_unavailable_scope_reports_real_gaps_and_no_fabricated_excerpt(self):
        self.build(self.real_agent(second_ticker='ZZZ'))
        done = await self.wait((await self.post(PROMPT)).json()['id'])
        self.assertEqual(done['status'], 'completed')
        result = done['result']
        self.assertEqual({e['company'] for e in result['excerpts']}, {'TST'})  # nothing for the unavailable scope
        gap_texts = [g['text'] for g in result['gaps']]
        self.assertTrue(any(t.startswith('Subquery 1 has no evidence meeting threshold') for t in gap_texts), gap_texts)
        self.assertEqual(next(g['subquery'] for g in result['gaps'] if g['text'].startswith('Subquery 1 ')), 1)
        self.assertEqual(result['plan'][1]['filing_resolved'], False)
        unavailable = [h for h in result['hops'] if h['subquery'] == 1]
        self.assertTrue(all(not h['accepted'] and h['context'] == [] for h in unavailable))


class BridgeProbeContract(unittest.IsolatedAsyncioTestCase):
    """The health probe reads the bridge's /health incrementally. Transports are scripted TEST DOUBLES."""
    SHA = 'a' * 64

    async def state(self, handler):
        from audit_web.runtime import BridgeProbe
        probe = BridgeProbe('http://127.0.0.1:1', self.SHA, transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(probe.close)
        return await probe()

    async def test_ready_and_fingerprint_mismatch(self):
        self.assertEqual(await self.state(lambda r: httpx.Response(200, json={'status': 'ready', 'corpus_sha256': self.SHA})), 'ready')
        self.assertEqual(await self.state(lambda r: httpx.Response(200, json={'status': 'ready', 'corpus_sha256': 'b' * 64})), 'fingerprint_mismatch')

    async def test_unhealthy_shapes(self):
        for response in (httpx.Response(503, json={'status': 'ready', 'corpus_sha256': self.SHA}), httpx.Response(200, json={'status': 'starting', 'corpus_sha256': self.SHA}),
                         httpx.Response(200, json=['not', 'a', 'dict'])):
            self.assertEqual(await self.state(lambda r, resp=response: resp), 'unhealthy')

    async def test_malformed_body_and_transport_errors(self):
        for body in (b'not json', b'', b'\xff\xfe', b'{"status": '):
            self.assertEqual(await self.state(lambda r, b=body: httpx.Response(200, content=b)), 'unreachable', body)

        def refuse(request):
            raise httpx.ConnectError('refused')
        self.assertEqual(await self.state(refuse), 'unreachable')

        def slow(request):
            raise httpx.ReadTimeout('slow')
        self.assertEqual(await self.state(slow), 'unreachable')

    async def test_oversized_stream_is_abandoned_without_reading_it_all(self):
        from audit_web.runtime import BridgeProbe
        pulled = []

        class Endless(httpx.AsyncByteStream):
            async def __aiter__(self):
                for i in range(100000):  # would be ~800 MB if fully consumed
                    pulled.append(i)
                    yield b'x' * 8192

        state = await self.state(lambda r: httpx.Response(200, stream=Endless()))
        self.assertEqual(state, 'unhealthy')
        self.assertLessEqual(len(pulled), BridgeProbe.MAX_BODY // 8192 + 2)

    async def test_body_exactly_at_limit_is_still_parsed(self):
        from audit_web.runtime import BridgeProbe
        base = json.dumps({'status': 'ready', 'corpus_sha256': self.SHA, 'pad': ''})
        body = json.dumps({'status': 'ready', 'corpus_sha256': self.SHA, 'pad': 'p' * (BridgeProbe.MAX_BODY - len(base))}).encode()
        self.assertEqual(len(body), BridgeProbe.MAX_BODY)
        self.assertEqual(await self.state(lambda r: httpx.Response(200, content=body)), 'ready')


class Lifespan(unittest.TestCase):
    def test_server_shutdown_cancels_and_awaits_the_audit_before_closing_resources(self):
        order = []
        agent = BlockingAgent()
        manager = AuditManager(agent, project_result)
        app = create_app(manager=manager, catalog=Fixture.catalog, probe=ready, ui_dir=UI, allowed_hosts=HOSTS, closers=[lambda: order.append(('closed', agent.cleaned))])
        with TestClient(app, base_url=BASE) as client:
            self.assertEqual(client.post('/api/audits', headers=GOOD, content=b'{"prompt":"Audit TST filing year 2024: x"}').status_code, 202)
            for _ in range(200):
                if agent.started.is_set():
                    break
                import time; time.sleep(0.01)
            self.assertTrue(agent.started.is_set())
        self.assertEqual(order, [('closed', True)])  # the agent task (and its process groups) were awaited first
        self.assertIsNone(manager.active_id)


if __name__ == '__main__':
    unittest.main()
