"""Identified EDGAR downloads with shared no-burst pacing and atomic cache files."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class EdgarClient:
    """Reuse ONE client for a process/account's traffic, including redirects/retries.

    At most two bodies download concurrently, one header request starts at a
    time, and starts are separated by >=1/rate seconds. Coordinate other
    processes/machines separately: this limiter cannot police external traffic.
    """
    def __init__(self, user_agent: str, rate: float = 5, timeout: float = 120,
                 max_bytes: int = 128 << 20, retries: int = 2,
                 allowed_hosts: tuple[str, ...] = ('www.sec.gov', 'data.sec.gov'),
                 allow_http: bool = False):
        if not re.search(r'\S+@[^\s@]+\.[^\s@]+', user_agent) or len(user_agent.split()) < 2 or any(ord(c) < 32 for c in user_agent):
            raise ValueError('User-Agent must identify your project and a contact email')
        if not 0 < rate <= 10 or not 1 <= timeout <= 600 or not 1 <= max_bytes <= 512 << 20 or not 0 <= retries <= 4:
            raise ValueError('invalid SEC rate/timeout/size/retry limits')
        self.user_agent, self.interval, self.timeout = user_agent, 1 / rate + .001, timeout
        self.max_bytes, self.retries = max_bytes, retries
        self.allowed_hosts, self.allow_http = allowed_hosts, allow_http
        self.lock, self.downloads = threading.Lock(), threading.BoundedSemaphore(2)
        self.last_start, self.not_before = 0., 0.
        self.opener = build_opener(NoRedirect)
        self.log = logging.getLogger(__name__)

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        schemes = ('http', 'https') if self.allow_http else ('https',)
        if parsed.scheme not in schemes or parsed.hostname not in self.allowed_hosts or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('URL is outside the configured EDGAR hosts/HTTPS policy')
        if not self.allow_http and parsed.port not in (None, 443):
            raise ValueError('unexpected SEC port')

    def _open(self, url: str, deadline: float):
        self._validate_url(url)
        with self.lock:
            delay = max(self.last_start + self.interval, self.not_before) - time.monotonic()
            if time.monotonic() + max(delay, 0) >= deadline:
                raise TimeoutError('SEC request deadline exceeded while pacing')
            if delay > 0:
                time.sleep(delay)
            request = Request(url, headers={'User-Agent':self.user_agent, 'Accept-Encoding':'identity'})
            # Pace conservatively from header completion, not a reserved start
            # time: scheduler/network stalls cannot bunch subsequent dispatches.
            try:
                return self.opener.open(request, timeout=max(.001, deadline-time.monotonic()))
            except HTTPError as exc:
                if exc.code in (429,500,502,503,504):
                    # Publish cooldown while still holding the dispatch lock;
                    # another downloader cannot race past Retry-After.
                    self.not_before = max(self.not_before,time.monotonic()+max(1.,self._retry_after(exc.headers.get('Retry-After'))))
                raise
            finally:
                self.last_start = time.monotonic()

    @staticmethod
    def _retry_after(value: str | None) -> float:
        if not value:
            return 0.
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return 0.
        return max(0., min(delay, 600.))

    def fetch(self, url: str, destination: Path) -> dict:
        self._validate_url(url)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        temporary = destination.with_name(destination.name+'.download')
        # Exclusive staging name protects cooperating concurrent downloads.
        with temporary.open('xb') as target:
            try:
                result = self._download(url, target)
                target.flush(); os.fsync(target.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(destination)
            temporary.rename(destination)
            descriptor = os.open(destination.parent, os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
        self.log.info('download_complete %s', json.dumps(result, sort_keys=True))
        return result

    def _download(self, url: str, target) -> dict:
        deadline = time.monotonic()+self.timeout
        if not self.downloads.acquire(timeout=self.timeout):
            raise TimeoutError('SEC download slots exhausted')
        try:
            current, redirects, attempt = url, 0, 0
            while True:
                response = None
                try:
                    response = self._open(current, deadline)
                    if response.status != 200:
                        raise ValueError(f'expected complete SEC HTTP 200 response, got {response.status}')
                    expected = response.headers.get('Content-Length')
                    if expected and int(expected) > self.max_bytes:
                        raise ValueError('SEC response exceeds byte limit')
                    checksum, size = hashlib.sha256(), 0
                    # read1 returns available bytes without waiting to fill an
                    # entire block, so a slow trickle still checks the deadline.
                    while block := response.read1(32768):
                        if time.monotonic() > deadline:
                            raise TimeoutError('SEC body deadline exceeded')
                        size += len(block)
                        if size > self.max_bytes:
                            raise ValueError('SEC response exceeds byte limit')
                        try:
                            target.write(block)
                        except OSError as exc:
                            raise RuntimeError('failed to write SEC staging file') from exc
                        checksum.update(block)
                    if size == 0 or (expected and size != int(expected)):
                        raise ValueError('empty or truncated SEC response')
                    return dict(url=current, bytes=size, sha256=checksum.hexdigest(), attempts=attempt+1, redirects=redirects)
                except HTTPError as exc:
                    status, location = exc.code, exc.headers.get('Location')
                    retry_delay = self._retry_after(exc.headers.get('Retry-After'))
                    exc.close()
                    if status in (301,302,303,307,308) and location:
                        redirects += 1
                        if redirects > 5:
                            raise ValueError('too many SEC redirects') from exc
                        current = urljoin(current, location); self._validate_url(current)
                        continue
                    if status not in (429,500,502,503,504) or attempt >= self.retries:
                        raise RuntimeError(f'SEC HTTP {status}; request stopped, no access-control bypass') from exc
                    attempt += 1
                    delay = max(retry_delay, 2 ** (attempt-1))
                    with self.lock:
                        self.not_before = max(self.not_before, time.monotonic()+delay)
                    self.log.warning('download_retry status=%d attempt=%d delay_seconds=%.3f url=%s',status,attempt,delay,current)
                except (URLError, OSError) as exc:
                    if attempt >= self.retries or time.monotonic() >= deadline:
                        raise RuntimeError(f'SEC transport failed after {attempt+1} attempts') from exc
                    attempt += 1
                    target.seek(0); target.truncate()
                    with self.lock:
                        self.not_before = max(self.not_before,time.monotonic()+2**(attempt-1))
                    self.log.warning('transport_retry attempt=%d url=%s',attempt,current)
                finally:
                    if response is not None:
                        response.close()
        finally:
            self.downloads.release()
