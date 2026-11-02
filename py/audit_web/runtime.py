"""Production wiring: the real, unchanged Agent(ClaudeJSON, HybridClient, ProvenanceRegistry) plus a bridge health probe."""
from dataclasses import dataclass
import json
import logging
from pathlib import Path
from agent.provenance import ProvenanceRegistry
from agent.provider import ClaudeJSON
from agent.retrieval import HybridClient
from agent.state_machine import Agent
import httpx
from . import limits
from .app import build_catalog
from .manager import AuditManager
from .projection import project_result

log = logging.getLogger('audit_web')


class BridgeProbe:
    """GET <bridge>/health and compare the corpus fingerprint. Returns a short state code; never raises."""

    MAX_BODY = 65536

    def __init__(self, backend_url: str, expected_corpus_sha256: str, transport: httpx.AsyncBaseTransport | None = None):
        self.url, self.expected = backend_url.rstrip('/'), expected_corpus_sha256
        self.client = httpx.AsyncClient(timeout=3.0, trust_env=False, transport=transport, limits=httpx.Limits(max_connections=2, max_keepalive_connections=1))

    async def __call__(self) -> str:
        try:
            async with self.client.stream('GET', self.url + '/health') as response:
                if response.status_code != 200:
                    return 'unhealthy'
                data = bytearray()
                async for block in response.aiter_bytes():  # incremental limit: an unbounded body is abandoned, never loaded
                    data.extend(block)
                    if len(data) > self.MAX_BODY:
                        return 'unhealthy'
            body = json.loads(data)
        except (httpx.HTTPError, ValueError):
            return 'unreachable'
        if not isinstance(body, dict) or body.get('status') != 'ready':
            return 'unhealthy'
        return 'ready' if body.get('corpus_sha256') == self.expected else 'fingerprint_mismatch'

    async def close(self) -> None:
        await self.client.aclose()


@dataclass
class Runtime:
    manager: AuditManager
    catalog: dict
    probe: BridgeProbe
    registry: ProvenanceRegistry
    backend: HybridClient
    _closed: bool = False

    async def close(self) -> None:
        """Idempotent. The app lifespan awaits manager.shutdown() first; this closes the HTTP clients and the registry."""
        if self._closed:
            return
        self._closed = True
        await self.manager.shutdown()
        await self.backend.__aexit__(None, None, None)
        await self.probe.close()
        self.registry.close()


def build_runtime(*, corpus: Path, provenance: Path, backend_url: str, expected_corpus_sha256: str, model: str = 'haiku', agent_mode: str = 'fast',
                  job_timeout: float = limits.JOB_TIMEOUT_MAX_SECONDS, model_timeout: float = limits.MODEL_TIMEOUT_MAX_SECONDS) -> Runtime:
    """One shared disk-backed registry. Agent limits (<=3 subqueries, <=2 retries, 5 contexts) are the agent's own, unchanged. Default mode is the real fast path."""
    if not 1 <= model_timeout <= limits.MODEL_TIMEOUT_MAX_SECONDS:
        raise ValueError(f'model timeout must be 1..{limits.MODEL_TIMEOUT_MAX_SECONDS} seconds')
    registry = ProvenanceRegistry(corpus, provenance)
    try:
        catalog = build_catalog(registry)
        backend = HybridClient(backend_url)
        agent = Agent(ClaudeJSON(model=model, timeout=model_timeout, concurrency=limits.MODEL_CONCURRENCY), backend, registry, mode=agent_mode)
        manager = AuditManager(agent, project_result, job_timeout=job_timeout)
        return Runtime(manager=manager, catalog=catalog, probe=BridgeProbe(backend_url, expected_corpus_sha256), registry=registry, backend=backend)
    except BaseException:
        registry.close()
        raise
