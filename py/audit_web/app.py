"""FastAPI application: loopback-only JSON API and fixed static assets around AuditManager."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
from typing import Awaitable, Callable, Iterable
from fastapi import FastAPI, Request, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from . import limits
from .manager import AuditManager, Busy, Closed, Job
from .projection import is_sec_archive_url
from .samples import SAMPLES, describe

log = logging.getLogger('audit_web')
CSRF_HEADER, CSRF_VALUE = 'x-requested-with', 'finfilings-ui'
JOB_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}')
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'")
SECURITY_HEADERS = [(b'content-security-policy', CSP.encode()), (b'x-content-type-options', b'nosniff'), (b'referrer-policy', b'no-referrer'),
                    (b'x-frame-options', b'DENY'), (b'cross-origin-opener-policy', b'same-origin'), (b'cross-origin-resource-policy', b'same-origin'),
                    (b'permissions-policy', b'camera=(), microphone=(), geolocation=()'), (b'cache-control', b'no-store')]
ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.css': ('app.css', 'text/css; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/favicon.svg': ('favicon.svg', 'image/svg+xml')}


def allowed_hosts_for(host: str, port: int, extra_ports: Iterable[int] = ()) -> frozenset[str]:
    """Exact Host header values accepted. Anything else (DNS rebinding, other ports) is rejected."""
    names = {'127.0.0.1': ['127.0.0.1', 'localhost'], 'localhost': ['127.0.0.1', 'localhost'], '::1': ['[::1]', 'localhost']}[host]
    return frozenset(f'{name}:{p}' for name in names for p in (port, *extra_ports))


def error_body(code: str, message: str, **extra: object) -> bytes:
    return json.dumps(dict(error=dict(code=code, message=message, **extra))).encode()


class Guard:
    """Pure ASGI guard that runs before routing: Host/Origin allowlists, mutation CSRF header, content type and
    Content-Length bounds, and security headers on every response (including its own rejections)."""

    def __init__(self, app, allowed_hosts: frozenset[str], max_body: int = limits.MAX_BODY_BYTES):
        self.app, self.hosts, self.max_body = app, allowed_hosts, max_body

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            return await self.app(scope, receive, send)
        if scope['type'] != 'http':
            return await send({'type': 'websocket.close', 'code': 1008}) if scope['type'] == 'websocket' else None
        started = False

        async def guarded_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
                present = {k.lower() for k, _ in message.get('headers', [])}
                message = dict(message, headers=list(message.get('headers', [])) + [h for h in SECURITY_HEADERS if h[0] not in present])
            await send(message)

        async def reject(status: int, code: str, text: str, extra_headers=()):
            body = error_body(code, text)
            await guarded_send({'type': 'http.response.start', 'status': status, 'headers': [(b'content-type', b'application/json'),
                                (b'content-length', str(len(body)).encode()), *extra_headers]})
            await guarded_send({'type': 'http.response.body', 'body': body})

        headers: dict[str, str] = {}
        for key, value in scope['headers']:
            name = key.decode('latin-1').lower()
            if name in ('host', 'origin', 'content-length', 'content-type', 'sec-fetch-site', CSRF_HEADER) and name in headers:
                return await reject(400, 'duplicate_header', 'Duplicate security-relevant header.')
            headers[name] = value.decode('latin-1')
        if headers.get('host') not in self.hosts:
            return await reject(403, 'bad_host', 'This workbench only answers requests addressed to its own loopback address and port.')
        origin = headers.get('origin')
        if origin is not None and origin.rstrip('/') != 'http://' + headers['host']:
            return await reject(403, 'bad_origin', 'Cross-origin requests are not allowed.')
        length = headers.get('content-length')
        if length is not None:
            if not length.isdigit() or len(length) > 9:
                return await reject(400, 'bad_content_length', 'Invalid Content-Length.')
            if int(length) > self.max_body:
                return await reject(413, 'body_too_large', f'Request body exceeds {self.max_body} bytes.')
        if scope['method'] not in ('GET', 'HEAD', 'OPTIONS'):
            if headers.get(CSRF_HEADER) != CSRF_VALUE:
                return await reject(403, 'csrf_header_required', 'Missing same-origin request header.')
            if headers.get('sec-fetch-site', 'same-origin') != 'same-origin':
                return await reject(403, 'cross_site', 'Cross-site requests are not allowed.')
            if scope['method'] == 'POST' and headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                return await reject(415, 'unsupported_media_type', 'Content-Type must be application/json.')
        await self.app(scope, receive, guarded_send)


class BodyTooLarge(Exception):
    pass


async def read_limited_body(request: Request, maximum: int = limits.MAX_BODY_BYTES) -> bytes:
    """Stream the body and stop at the bound, so chunked requests cannot bypass Content-Length checks."""
    data = bytearray()
    async for block in request.stream():
        data.extend(block)
        if len(data) > maximum:
            raise BodyTooLarge()
    return bytes(data)


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def build_catalog(registry) -> dict:
    """Bounded filing catalog from the registry (<=10000 entries, filings only, never passages)."""
    entries = registry.catalog()
    if not 1 <= len(entries) <= limits.MAX_CATALOG_FILINGS:
        raise ValueError('catalog size outside 1..10000')
    filings = []
    for entry in entries:
        accession = entry['accession']
        url = registry.sources[accession]['url']
        if not is_sec_archive_url(url):
            raise ValueError('catalog contains an untrusted SEC link')
        filings.append(dict(ticker=entry['ticker'], filing_year=entry['filing_year'], form=entry['form'],
                            accession=f'{accession[:10]}-{accession[10:12]}-{accession[12:]}', fiscal_period_end=entry['fiscal_period_end'],
                            sections=entry['sections'], url=url))
    filings.sort(key=lambda f: (f['ticker'], f['filing_year']))
    return dict(filing_count=len(filings), company_count=len({f['ticker'] for f in filings}), filings=filings, samples=describe(SAMPLES, filings))


def create_app(*, manager: AuditManager, catalog: dict, probe: Callable[[], Awaitable[str]], ui_dir: Path, allowed_hosts: frozenset[str],
               closers: Iterable[Callable[[], Awaitable[None] | None]] = ()) -> Guard:
    assets = {}
    for route, (name, mime) in ASSETS.items():
        path = Path(ui_dir) / name
        if not path.is_file():
            raise FileNotFoundError(f'missing UI asset {name}')
        assets[route] = (path.read_bytes(), mime)
    catalog_bytes = json.dumps(catalog, ensure_ascii=False, separators=(',', ':')).encode()
    closers = tuple(closers)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await manager.shutdown()  # cancel + await the agent task and its model process groups first
            for close in closers:
                try:
                    outcome = close()
                    if outcome is not None:
                        await outcome
                except Exception:
                    log.exception('shutdown hook failed')

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def respond(status: int, payload: dict | bytes, headers: dict | None = None) -> Response:
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        return Response(body, status_code=status, media_type='application/json', headers=headers)

    def fail(status: int, code: str, message: str, headers: dict | None = None, **extra) -> Response:
        return Response(error_body(code, message, **extra), status_code=status, media_type='application/json', headers=headers)

    def view(job: Job, *, detail: bool) -> dict:
        data = dict(id=job.id, status=job.status, created_at=iso(job.created_at), elapsed_seconds=round(manager.elapsed(job), 3),
                    cancel_requested=job.cancel_requested, error=job.error)
        if detail:
            data['prompt'] = job.prompt
        else:
            data['prompt_preview'] = job.prompt[:160]
        return data

    def lookup(job_id: str) -> Job | None:
        return manager.get(job_id) if JOB_ID.fullmatch(job_id) else None

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request, exc):
        return fail(exc.status_code, 'not_found' if exc.status_code == 404 else 'http_error', 'Not found.' if exc.status_code == 404 else 'Request not allowed.')

    @app.exception_handler(Exception)
    async def unexpected(_request, exc):
        log.error('unhandled API error', exc_info=exc)
        return fail(500, 'internal_error', 'Unexpected server error. Details are in the local server log.')

    def static_route(route: str):
        body, mime = assets[route]

        async def serve():
            return Response(body, media_type=mime)
        return serve

    for route in assets:
        app.add_api_route(route, static_route(route), methods=['GET'], include_in_schema=False)

    @app.get('/api/health')
    async def health():
        state = await probe()
        limits_view = dict(max_prompt_bytes=limits.MAX_PROMPT_BYTES, max_body_bytes=limits.MAX_BODY_BYTES, max_history=manager.max_history,
                           history_ttl_seconds=int(manager.ttl), max_result_bytes=manager.max_result_bytes, job_timeout_seconds=int(manager.job_timeout))
        return respond(200 if state == 'ready' else 503, dict(status='ready' if state == 'ready' else 'unavailable', backend=state,
                       active_audit=manager.active_id is not None, limits=limits_view))

    @app.get('/api/catalog')
    async def get_catalog():
        return respond(200, catalog_bytes)

    @app.get('/api/audits')
    async def list_audits():
        return respond(200, dict(audits=[view(j, detail=False) for j in manager.list()], active_id=manager.active_id,
                                 max_history=manager.max_history, history_ttl_seconds=int(manager.ttl)))

    @app.post('/api/audits')
    async def create_audit(request: Request):
        try:
            body = await read_limited_body(request)
        except BodyTooLarge:
            return fail(413, 'body_too_large', f'Request body exceeds {limits.MAX_BODY_BYTES} bytes.')
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            return fail(400, 'invalid_json', 'The request body is not valid JSON.')
        if not isinstance(data, dict) or set(data) != {'prompt'} or not isinstance(data['prompt'], str):
            return fail(422, 'invalid_request', 'Send a JSON object with exactly one string field: "prompt".')
        prompt = data['prompt']
        try:
            size = len(prompt.encode())
        except UnicodeEncodeError:
            return fail(422, 'invalid_prompt', 'The question contains characters that cannot be encoded.')
        if not prompt.strip():
            return fail(422, 'prompt_empty', 'Enter an audit question.')
        if size > limits.MAX_PROMPT_BYTES:
            return fail(422, 'prompt_too_long', f'The question is {size} bytes; the limit is {limits.MAX_PROMPT_BYTES}.')
        if CONTROL.search(prompt):
            return fail(422, 'invalid_prompt', 'The question contains control characters.')
        busy = dict(headers={'Retry-After': str(limits.RETRY_AFTER_SECONDS)})
        if manager.active_id is not None:
            return fail(429, 'busy', 'Another audit is already running. Wait for it to finish or cancel it, then try again.', active_id=manager.active_id, **busy)
        state = await probe()  # corpus fingerprint is verified against the retrieval bridge before admission
        if state != 'ready':
            return fail(503, 'backend_unavailable', 'The retrieval service is not ready, so the audit was not started.', state=state)
        try:
            job = manager.submit(prompt)
        except Busy as exc:
            return fail(429, 'busy', 'Another audit is already running. Wait for it to finish or cancel it, then try again.', active_id=exc.active_id, **busy)
        except Closed:
            return fail(503, 'shutting_down', 'The server is shutting down.')
        return respond(202, dict(id=job.id, status=job.status, status_url=f'/api/audits/{job.id}'), {'Location': f'/api/audits/{job.id}'})

    @app.get('/api/audits/{job_id}')
    async def get_audit(job_id: str):
        job = lookup(job_id)
        if job is None:
            return fail(404, 'not_found', 'No such audit. Finished audits are kept for a limited time only.')
        head = json.dumps(view(job, detail=True), ensure_ascii=False).encode()
        if job.status == 'completed' and job.result_bytes is not None:
            head = head[:-1] + b',"result":' + job.result_bytes + b'}'  # result serialized once, spliced without re-encoding
        return respond(200, head)

    @app.delete('/api/audits/{job_id}')
    async def cancel_audit(job_id: str):
        if lookup(job_id) is None:
            return fail(404, 'not_found', 'No such audit. Finished audits are kept for a limited time only.')
        job = manager.cancel(job_id)
        return respond(202 if job.status not in ('completed', 'failed', 'cancelled') else 200, view(job, detail=False))

    return Guard(app, allowed_hosts)  # type: ignore[return-value]
