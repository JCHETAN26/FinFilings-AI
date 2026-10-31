"""Bounded asynchronous HTTP bridge to the real Go/PQ8/cross-encoder pipeline."""
import json
from typing import Any
from urllib.parse import urlparse
import httpx
from .filters import FilingFilter


class HybridClient:
    def __init__(self, url: str = 'http://127.0.0.1:8765', timeout: float = 125):
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('invalid retrieval URL')
        self.url = url.rstrip('/')
        self.client = httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_connections=3, max_keepalive_connections=3), trust_env=False)

    async def __aenter__(self) -> 'HybridClient':
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.client.aclose()

    async def search(self, query: str, filters: FilingFilter) -> list[dict[str, Any]]:
        async with self.client.stream('POST', self.url + '/search', json=dict(query=query, rerank=True, filters=filters.model_dump())) as response:
            if response.is_error:
                detail = bytearray()
                async for block in response.aiter_bytes():
                    detail.extend(block[:4096-len(detail)])
                    if len(detail) >= 4096:
                        break
                raise RuntimeError(f'hybrid HTTP {response.status_code}: {detail.decode(errors="replace")}')
            data = bytearray()
            async for block in response.aiter_bytes():
                data.extend(block)
                if len(data) > 1 << 20:
                    raise ValueError('retrieval response exceeds 1 MiB')
        result = json.loads(data)
        hits = result.get('reranked', [])
        if not isinstance(hits, list) or len(hits) > 10:
            raise ValueError('invalid reranked response')
        identifiers = [hit.get('id') for hit in hits]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError('duplicate retrieved IDs')
        return hits
