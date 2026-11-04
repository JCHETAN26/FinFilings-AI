"""Real HTTP transport against a serial loopback fixture; synthetic bytes, no model endpoints."""
from contextlib import contextmanager
import http.server
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
import urllib.request
from artifact_testkit import encode, release, sha
import artifact_manager as m

ALLOWED_PORTS=set()


@contextmanager
def server(body,mode='range'):
    requests=[]
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            requests.append(self.headers.get('Range'))
            offset=int(self.headers.get('Range','bytes=0-').split('=')[1].split('-')[0])
            data=body
            if mode=='redirect':
                self.send_response(302);self.send_header('Location','http://example.invalid/bundle.tar');self.end_headers();return
            if mode=='interrupt' and len(requests)==1:
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers()
                self.wfile.write(body[:len(body)//2]);self.wfile.flush();self.close_connection=True;return
            if mode in ('range','interrupt','bad-range') and offset:
                data=body[offset:];self.send_response(206)
                self.send_header('Content-Range',f'bytes {offset if mode!="bad-range" else offset+1}-{len(body)-1}/{len(body)}')
            else:self.send_response(200)
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    httpd=http.server.HTTPServer(('127.0.0.1',0),Handler)
    port=httpd.server_address[1];ALLOWED_PORTS.add(port)
    thread=threading.Thread(target=lambda:httpd.serve_forever(poll_interval=.02),daemon=True)
    thread.start()
    try:yield f'http://127.0.0.1:{port}/bundle.tar',requests
    finally:
        httpd.shutdown();thread.join(timeout=3);httpd.server_close();ALLOWED_PORTS.remove(port)
        if thread.is_alive():raise RuntimeError('fixture server did not stop')


class DownloadContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.body=b'unit fixture transport bytes\n'*200
        self.part=self.root/'bundle.part'

    def download(self,url):
        return m.download(dict(url=url,bytes=len(self.body),sha256=sha(self.body)),self.part,True)

    def test_resume_partial_download_uses_exact_range(self):
        self.part.write_bytes(self.body[:200])
        with server(self.body) as (url,requests):self.download(url)
        self.assertEqual(requests,['bytes=200-']);self.assertEqual(self.part.read_bytes(),self.body)

    def test_interrupted_transport_retries_from_written_offset(self):
        with server(self.body,'interrupt') as (url,requests):self.download(url)
        self.assertEqual(requests,[None,f'bytes={len(self.body)//2}-'])
        self.assertEqual(self.part.read_bytes(),self.body)

    def test_server_ignoring_range_restarts_without_append(self):
        self.part.write_bytes(self.body[:200])
        with server(self.body,'ignore') as (url,requests):self.download(url)
        self.assertEqual(self.part.read_bytes(),self.body)

    def test_wrong_content_range_is_rejected(self):
        self.part.write_bytes(self.body[:200])
        with server(self.body,'bad-range') as (url,requests):
            with self.assertRaisesRegex(ValueError,'Content-Range'):self.download(url)
        self.assertEqual(self.part.stat().st_size,200)

    def test_checksum_mismatch_deletes_corrupt_partial(self):
        with server(b'x'*len(self.body)) as (url,requests):
            with self.assertRaisesRegex(ValueError,'checksum'):self.download(url)
        self.assertFalse(self.part.exists())

    def test_oversized_response_is_rejected_without_installation(self):
        with server(self.body+b'EXTRA') as (url,requests):
            with self.assertRaisesRegex(ValueError,'pinned size'):self.download(url)

    def test_remote_non_https_redirect_is_rejected_before_connection(self):
        with server(self.body,'redirect') as (url,requests):
            with self.assertRaisesRegex(ValueError,'HTTPS'):self.download(url)

    def test_https_downgrade_redirect_is_rejected(self):
        request=urllib.request.Request('https://example.invalid/artifact')
        with self.assertRaisesRegex(ValueError,'downgrade'):
            m.Redirects(True).redirect_request(request,None,302,'Found',{},'http://127.0.0.1:1/file')

    def test_cli_install_and_activate_from_http_without_model_calls(self):
        bundle,data,_=release(self.root)
        with server(bundle.read_bytes()) as (url,requests):
            data['bundle']['url']=url;raw=encode(data);manifest=self.root/'release.json';manifest.write_bytes(raw)
            spec=importlib.util.spec_from_file_location('artifact_cli',Path(__file__).resolve().parents[1]/'scripts/artifacts.py')
            cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
            store=self.root/'store'
            self.assertEqual(cli.main(['--store',str(store),'install','--manifest',str(manifest),'--sha256',sha(raw),'--allow-loopback-http','--activate']),0)
            self.assertEqual(m.Store(store).state()['active'],data['release_id'])
            self.assertEqual(len(requests),1)
            m.Store(store).verify(data['release_id'])


if __name__=='__main__':unittest.main()
