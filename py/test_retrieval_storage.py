"""Failure injection verifies storage diagnostics, bounded ownership and recovery; no LLM calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx
from common import open_store
from service import app_for

class StorageContracts(unittest.IsolatedAsyncioTestCase):
    async def test_http_storage_failure_is_degraded_and_recovers_without_retry(self):
        class Retrieval:
            def __init__(self):
                self.encoders = [object()]
                self.index = SimpleNamespace(ntotal=1)
                self.manifest = {'corpus_sha256': 'fixture'}
                self.encoder = SimpleNamespace(lock={'revision':'fixture'},model=SimpleNamespace(parameters=lambda:iter([SimpleNamespace(device='cpu')])))
                self.reranker = SimpleNamespace(lock={'revision':'fixture'},device='cpu',backend='fixture',artifact_sha256='fixture',batch_size=1)
                self.threads = 1
                self.calls = 0
                self.error = sqlite3.OperationalError('disk I/O error /private/path')
            def dense(self, request, worker):
                self.calls += 1
                if self.error: raise self.error
                return {'hits':[], 'corpus_sha256':'fixture'}
        retrieval=Retrieval()
        with ThreadPoolExecutor(max_workers=1) as executor:
            app=app_for(retrieval,executor,2,workers=1)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                for error in (sqlite3.OperationalError('disk I/O error /private/path'),OSError('device offline')):
                    retrieval.error=error
                    response=await client.post('/dense',json={'query':'test'})
                    self.assertEqual(response.status_code,503)
                    self.assertNotIn('private',response.text)
                    self.assertEqual(response.headers['retry-after'],'1')
                    response=await client.get('/health')
                    self.assertEqual(response.status_code,503)
                    self.assertEqual(response.json()['status'],'degraded')
                self.assertEqual(retrieval.calls,2,'storage errors must not trigger hidden inference retries')
                retrieval.error=None
                self.assertEqual((await client.post('/dense',json={'query':'test'})).status_code,200)
                health=(await client.get('/health')).json()
                self.assertEqual(health['status'],'ready')
                self.assertEqual(health['storage']['failures'],2)
                self.assertEqual(health['admission']['active'],0)

    def test_failed_connection_initialization_closes_connection(self):
        connection=Mock()
        connection.execute.side_effect=sqlite3.OperationalError('disk I/O error')
        with patch('common.sqlite3.connect',return_value=connection):
            with self.assertRaises(sqlite3.OperationalError):open_store('/unused/fixture.sqlite')
        connection.close.assert_called_once()

if __name__=='__main__':unittest.main()
