"""Versioned immutable artifact delivery. Standard library only; never loads model providers.

One writer, one streaming download, bounded reads, hash-pinned manifests, and atomic state.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SCHEMA = 'finfilings-release/v1'
PARSER = 'deterministic-canonical-heading-v3-go'
MODELS = {'BAAI/bge-small-en-v1.5', 'cross-encoder/ms-marco-MiniLM-L6-v2'}
PROFILE_KEYS = {'corpus_dir', 'ingest_report', 'original_provenance', 'sparse_index',
                'vector_index', 'reranker_model', 'model_lock', 'source_enrollment'}
LOG = logging.getLogger('finfilings.artifacts')
MAX_BYTES = 20 << 30
MAX_JSON = 8 << 20
CHUNK = 1 << 20


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,79}', value) or value in ('.', '..'):
        raise ValueError('invalid release ID')
    return value


def checksum(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('invalid SHA-256')
    return value


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('invalid relative artifact path')
    p = PurePosixPath(value)
    if p.is_absolute() or '..' in p.parts or str(p) != value or value == '.':
        raise ValueError('unsafe artifact path')
    return value


def model_member(lock_name, snapshot):
    if not isinstance(snapshot, str) or snapshot.startswith('/') or '\\' in snapshot:
        raise ValueError('model snapshot must be portable')
    return relative(posixpath.normpath(posixpath.join(posixpath.dirname(lock_name), snapshot)))


def json_bytes(raw):
    if len(raw) > MAX_JSON:
        raise ValueError('JSON exceeds size limit')
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('duplicate JSON key')
            out[key] = value
        return out
    return json.loads(raw, object_pairs_hook=unique)


def read_json(path):
    with Path(path).open('rb') as stream:
        return json_bytes(stream.read(MAX_JSON + 1))


def no_links(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlink in artifact path')
    return path


def atomic_json(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) != PROFILE_KEYS or profile['source_enrollment'] != 'complete_report':
        raise ValueError('release requires a complete-report setup profile')
    for key, value in profile.items():
        if key != 'source_enrollment':
            relative(value)
    return profile


def validate_release(data):
    if data.get('schema') != SCHEMA:
        raise ValueError('unsupported release schema')
    identifier(data['release_id'])
    if not re.fullmatch(r'[0-9a-f]{40}', data.get('source_commit', '')):
        raise ValueError('source commit must be pinned')
    checksum(data['bundle']['sha256']); checksum(data['bundle_manifest_sha256'])
    check_url(data['bundle']['url'], allow_loopback=True)
    if type(data['bundle']['bytes']) is not int or not 0 < data['bundle']['bytes'] <= MAX_BYTES:
        raise ValueError('invalid bundle size')
    relative(data['bundle']['name'])
    if '/' in data['bundle']['name']:
        raise ValueError('bundle filename must be a basename')
    checksum(data['corpus']['sha256'])
    if data['corpus']['parser'] != PARSER or any(type(data['corpus'][k]) is not int or data['corpus'][k] < 1 for k in ('passages', 'filings')):
        raise ValueError('invalid corpus identity')
    if set(data['models']) != MODELS:
        raise ValueError('unexpected models')
    for model in data['models'].values():
        if not re.fullmatch(r'[0-9a-f]{40}', model['revision']):
            raise ValueError('model revision must be pinned')
        checksum(model['weights_sha256'])
    validate_profile(data['profile'])
    return data


def inspect_bundle(bundle, destination=None):
    """Validate all bytes before extraction. Never calls tar.extract or trusts links."""
    if Path(bundle).stat().st_size > MAX_BYTES:
        raise ValueError('bundle exceeds size limit')
    with tarfile.open(bundle, 'r:') as archive:
        members = []
        names = {}
        total = 0
        for member in archive:
            relative(member.name)
            if not member.isfile() or member.name in names:
                raise ValueError('nonregular or duplicate tar member')
            total += member.size
            if len(members) >= 20000 or total > MAX_BYTES:
                raise ValueError('archive exceeds limits')
            members.append(member); names[member.name] = member
        def document(name):
            member = names[name]
            if member.size > MAX_JSON:
                raise ValueError('archive JSON too large')
            with archive.extractfile(member) as stream:
                return json_bytes(stream.read(MAX_JSON + 1))
        manifest = document('bundle-manifest.json')
        if manifest.get('version') != 1 or set(names) != set(manifest['files']) | {'bundle-manifest.json'}:
            raise ValueError('archive manifest entries differ')
        for member in members:
            h = hashlib.sha256()
            with archive.extractfile(member) as stream:
                for block in iter(lambda: stream.read(CHUNK), b''):
                    h.update(block)
            if member.name != 'bundle-manifest.json' and manifest['files'][member.name] != {'bytes': member.size, 'sha256': h.hexdigest()}:
                raise ValueError('artifact checksum mismatch: ' + member.name)
        profile = validate_profile(document('setup-profile.json'))
        corpus_name = profile['corpus_dir'] + '/chunks.jsonl'
        provenance = document(profile['corpus_dir'] + '/provenance.json')
        report = document(profile['ingest_report'])
        report_sha = manifest['files'][profile['ingest_report']]['sha256']
        corpus_sha = manifest['files'][corpus_name]['sha256']
        if provenance['corpus_sha256'] != corpus_sha or provenance['new_sec_report_sha256'] != report_sha:
            raise ValueError('corpus/report provenance mismatch')
        if report.get('parser') != PARSER or report.get('raw_retained') is not True or report.get('skipped'):
            raise ValueError('not a complete retained parser-v3 corpus')
        filings = report['filings']
        if not 1 <= len(filings) <= 10000:
            raise ValueError('invalid filing count')
        seen = set(); sections = {}; chunks = 0
        sources = {}
        for filing in filings:
            ref, downloaded, parsed = filing['ref'], filing['download'], filing['parse']
            accession = ref['Accession']
            if not re.fullmatch(r'\d{18}', accession) or accession in seen or not re.fullmatch(r'\d{1,10}', ref['CIK']):
                raise ValueError('invalid/duplicate source identity')
            seen.add(accession)
            relative(ref['PrimaryDocument'])
            if '/' in ref['PrimaryDocument']:
                raise ValueError('invalid source document')
            expected_url = f"https://www.sec.gov/Archives/edgar/data/{ref['CIK']}/{accession}/{ref['PrimaryDocument']}"
            raw_name = posixpath.dirname(profile['ingest_report']) + '/raw/' + accession + '.raw'
            if (downloaded['url'] != expected_url or manifest['files'][raw_name] != {'bytes': downloaded['bytes'], 'sha256': downloaded['sha256']}
                    or parsed['raw_sha256'] != downloaded['sha256'] or parsed['raw_bytes'] != downloaded['bytes'] or parsed['parser'] != PARSER):
                raise ValueError('raw filing identity mismatch')
            if set(parsed['sections']) != {'Item 1A', 'Item 7', 'Item 8'} or any(type(v['chunks']) is not int or v['chunks'] < 1 for v in parsed['sections'].values()):
                raise ValueError('missing parser-v3 sections')
            if parsed['chunks'] != sum(v['chunks'] for v in parsed['sections'].values()):
                raise ValueError('source passage totals mismatch')
            chunks += parsed['chunks']
            for name, value in parsed['sections'].items():
                sections[name] = sections.get(name, 0) + value['chunks']
            sources[accession] = filing
        if report['chunks'] != chunks or report['sections'] != sections:
            raise ValueError('report totals mismatch')
        original = document(profile['original_provenance'])
        if original['corpus_sha256'] != manifest['files']['data/sec/chunks.jsonl']['sha256'] or provenance['sec_sha256'] != original['corpus_sha256']:
            raise ValueError('original provenance mismatch')
        for old in original['sources']:
            current = sources[old['accession']]
            ref = current['ref']
            if (old['ticker'], old['cik'], old['filing_year'], old['form'], old['url']) != (ref['Ticker'], ref['CIK'], int(ref['FilingDate'][:4]), ref['Form'], current['download']['url']):
                raise ValueError('original filing metadata changed')
        with archive.extractfile(names[corpus_name]) as stream:
            passages = 0
            while True:
                line = stream.readline((1 << 20) + 1)
                if not line:
                    break
                if len(line) > 1 << 20 or not line.strip():
                    raise ValueError('invalid corpus line')
                passages += 1
        vector = document(profile['vector_index'] + '/manifest.json')
        if (vector['corpus_sha256'] != corpus_sha or vector['documents'] != passages
                or vector['dimension'] != 384 or vector['subvectors'] != 128 or vector['nbits'] != 8
                or vector['normalized'] is not True or vector['model'] != 'BAAI/bge-small-en-v1.5'):
            raise ValueError('vector index belongs to a different corpus/configuration')
        if set(vector['files']) != {'vectors.f32.npy', 'passages.sqlite', 'index.pq8.faiss', 'centroids.npy'}:
            raise ValueError('incomplete vector index manifest')
        for name, expected in vector['files'].items():
            if manifest['files'][profile['vector_index'] + '/' + relative(name)]['sha256'] != expected:
                raise ValueError('vector inner checksum mismatch')
        if not any(profile['sparse_index'] + '/' + name in names for name in ('MANIFEST', 'shards.json')):
            raise ValueError('missing sparse index manifest')
        sparse_fp = profile['sparse_index'] + '/CORPUS_SHA256'
        with archive.extractfile(names[sparse_fp]) as stream:
            if stream.read(128).decode().strip() != corpus_sha:
                raise ValueError('sparse index belongs to a different corpus')
        models = document(profile['model_lock'])
        if set(models) != MODELS:
            raise ValueError('unexpected model snapshots')
        summary_models = {}
        for name, model in models.items():
            member = model_member(profile['model_lock'], model['path']) + '/model.safetensors'
            snapshot = model_member(profile['model_lock'], model['path'])
            for required in ('config.json', 'tokenizer_config.json', 'vocab.txt'):
                if snapshot + '/' + required not in manifest['files']:
                    raise ValueError('incomplete local model snapshot')
            if manifest['files'][member]['sha256'] != model['weights_sha256']:
                raise ValueError('model lock weights mismatch')
            summary_models[name] = {k:model[k] for k in ('revision', 'weights_sha256')}
        reranker = document(profile['reranker_model'] + '/manifest.json')
        cross = summary_models['cross-encoder/ms-marco-MiniLM-L6-v2']
        if (reranker['model_revision'] != cross['revision'] or reranker['weights_sha256'] != cross['weights_sha256']
                or reranker['dtype'] != 'float32' or reranker['max_length'] != 512
                or manifest['files'][profile['reranker_model'] + '/model.onnx']['sha256'] != reranker['files']['model.onnx']
                or vector['model_revision'] != summary_models['BAAI/bge-small-en-v1.5']['revision']):
            raise ValueError('inference model identity mismatch')
        result = dict(profile=profile, models=summary_models,
            corpus=dict(sha256=corpus_sha, passages=passages, filings=len(filings), parser=PARSER),
            bundle_manifest_sha256=hashlib.sha256(archive.extractfile(names['bundle-manifest.json']).read(MAX_JSON + 1)).hexdigest())
        if destination is not None:
            destination = no_links(destination)
            if destination.exists():
                raise ValueError('extraction destination must be fresh')
            destination.mkdir(parents=True)
            for member in members:
                target = destination / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as output, archive.extractfile(member) as stream:
                    shutil.copyfileobj(stream, output, CHUNK)
        return result


def check_url(url, allow_loopback=False):
    parts = urllib.parse.urlsplit(url)
    if parts.username or parts.password or parts.fragment or not parts.hostname:
        raise ValueError('invalid artifact URL')
    if parts.scheme == 'https':
        return url
    if allow_loopback and parts.scheme == 'http' and parts.hostname in ('127.0.0.1', '::1'):
        return url
    raise ValueError('artifact URLs require HTTPS (test loopback HTTP must be explicit)')


class Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_loopback):
        self.allow_loopback = allow_loopback
    def redirect_request(self, request, fp, code, message, headers, url):
        check_url(url, self.allow_loopback)
        if urllib.parse.urlsplit(request.full_url).scheme == 'https' and urllib.parse.urlsplit(url).scheme != 'https':
            raise ValueError('HTTPS downgrade redirect')
        return super().redirect_request(request, fp, code, message, headers, url)


def opener(allow_loopback):
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirects(allow_loopback))


def load_release(source, expected_sha256, allow_loopback=False):
    checksum(expected_sha256)
    if str(source).startswith(('https://', 'http://')):
        with opener(allow_loopback).open(check_url(str(source), allow_loopback), timeout=30) as response:
            raw = response.read(MAX_JSON + 1)
    else:
        with no_links(source).open('rb') as stream:
            raw = stream.read(MAX_JSON + 1)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('release manifest checksum mismatch')
    return validate_release(json_bytes(raw)), raw


def download(bundle, part, allow_loopback=False):
    """At most three serial attempts; keep valid partial bytes after interrupted transport."""
    size = bundle['bytes']; checksum(bundle['sha256'])
    url = check_url(bundle['url'], allow_loopback)
    no_links(part)
    if part.exists() and part.stat().st_size > size:
        part.unlink()
    deadline = time.monotonic() + 1800
    for attempt in range(3):
        offset = part.stat().st_size if part.exists() else 0
        if offset == size:
            break
        LOG.info('Download attempt %d/3: resuming at %d of %d bytes', attempt + 1, offset, size)
        headers = {'Accept-Encoding': 'identity'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        try:
            with opener(allow_loopback).open(urllib.request.Request(url, headers=headers), timeout=30) as response:
                if response.headers.get('Content-Encoding', 'identity') != 'identity':
                    raise ValueError('encoded artifact response')
                append = offset > 0 and response.status == 206
                if response.status == 206:
                    expected = f'bytes {offset}-{size-1}/{size}'
                    if response.headers.get('Content-Range') != expected:
                        raise ValueError('invalid resume Content-Range')
                elif response.status != 200:
                    raise ValueError('unexpected artifact response')
                written = offset if append else 0
                with part.open('ab' if append else 'wb') as stream:
                    while True:
                        if time.monotonic() > deadline:
                            raise TimeoutError('artifact download exceeded 30-minute budget')
                        block = response.read(min(CHUNK, size - written + 1))
                        if not block:
                            break
                        written += len(block)
                        if written > size:
                            raise ValueError('download exceeds pinned size')
                        stream.write(block)
                    stream.flush(); os.fsync(stream.fileno())
                if written != size:
                    raise OSError('incomplete artifact download')
            break
        except (OSError, urllib.error.URLError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == 2 or time.monotonic() > deadline:
                raise
            LOG.warning('Download interrupted (%s); retaining partial bytes for retry', type(exc).__name__)
            time.sleep(0.1 * (attempt + 1))
    if part.stat().st_size != size or digest(part) != bundle['sha256']:
        part.unlink(missing_ok=True)
        raise ValueError('download checksum mismatch')
    LOG.info('Bundle checksum verified (%d bytes)', size)
    return part


class Store:
    def __init__(self, root):
        self.root = no_links(root)

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock = no_links(self.root / '.lock')
        with lock.open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('artifact store busy; another operation is running') from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def directory(self, version):
        return no_links(self.root / 'releases' / identifier(version))

    def state(self):
        path = no_links(self.root / 'state.json')
        if not path.exists():
            return {'version': 1, 'active': None, 'previous': None}
        state = read_json(path)
        if state.get('version') != 1:
            raise ValueError('invalid store state')
        for key in ('active', 'previous'):
            if state.get(key) is not None:
                identifier(state[key])
        return state

    def verify(self, version, directory=None):
        directory = no_links(directory) if directory is not None else self.directory(version)
        release = validate_release(read_json(no_links(directory / 'release.json')))
        expected = read_json(no_links(directory / 'receipt.json'))
        if digest(directory / 'release.json') != expected['manifest_sha256'] or release['release_id'] != version:
            raise ValueError('installed release manifest modified')
        payload = directory / 'payload'
        manifest_path = no_links(payload / 'bundle-manifest.json')
        if digest(manifest_path) != release['bundle_manifest_sha256']:
            raise ValueError('installed bundle manifest modified')
        files = read_json(manifest_path)['files']
        actual = set()
        for path in payload.rglob('*'):
            if path.is_symlink():
                raise ValueError('installed artifact symlink')
            if path.is_file():
                actual.add(path.relative_to(payload).as_posix())
        if actual != set(files) | {'bundle-manifest.json'}:
            raise ValueError('installed artifact entries changed')
        for name, entry in files.items():
            path = no_links(payload / relative(name))
            if path.stat().st_size != entry['bytes'] or digest(path) != entry['sha256']:
                raise ValueError('installed artifact checksum mismatch: ' + name)
        return release, payload

    def install(self, release, raw, bundle_file=None, allow_loopback=False):
        validate_release(release)
        if validate_release(json_bytes(raw)) != release:
            raise ValueError('manifest bytes differ from release object')
        with self.locked():
            version = release['release_id']; target = self.directory(version)
            LOG.info('Installing immutable release %s', version)
            if target.exists():
                old, _ = self.verify(version)
                if digest(target / 'release.json') != hashlib.sha256(raw).hexdigest():
                    raise ValueError('release ID is immutable; manifest differs')
                return {'release_id': version, 'status': 'already_installed'}
            target.parent.mkdir(parents=True, exist_ok=True)
            if bundle_file is None:
                downloads = no_links(self.root / 'downloads'); downloads.mkdir(exist_ok=True)
                part = no_links(downloads / (release['bundle']['sha256'] + '.part'))
                retained = min(part.stat().st_size, release['bundle']['bytes']) if part.exists() else 0
                if shutil.disk_usage(self.root).free < 2 * release['bundle']['bytes'] - retained:
                    raise ValueError('insufficient free space for download and installation')
                bundle_file = download(release['bundle'], part, allow_loopback)
            bundle_file = no_links(bundle_file)
            if bundle_file.stat().st_size != release['bundle']['bytes'] or digest(bundle_file) != release['bundle']['sha256']:
                raise ValueError('bundle identity mismatch')
            if shutil.disk_usage(self.root).free < release['bundle']['bytes']:
                raise ValueError('insufficient free space for installation')
            stage = Path(tempfile.mkdtemp(prefix='.install-', dir=target.parent))
            try:
                result = inspect_bundle(bundle_file, stage / 'payload')
                for key in ('corpus', 'models', 'profile', 'bundle_manifest_sha256'):
                    if result[key] != release[key]:
                        raise ValueError('release contents differ from manifest: ' + key)
                (stage / 'release.json').write_bytes(raw)
                atomic_json(stage / 'receipt.json', {'manifest_sha256': hashlib.sha256(raw).hexdigest()})
                self.verify(version, directory=stage)
                LOG.info('Payload verification passed for %s', version)
                os.rename(stage, target)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
            return {'release_id': version, 'status': 'installed', 'activated': False}

    def activate(self, version=None, rollback=False):
        with self.locked():
            state = self.state()
            if rollback:
                version = state['previous']
                if version is None:
                    raise ValueError('no previous release to roll back to')
            self.verify(version)
            if state['active'] != version:
                atomic_json(self.root / 'state.json', {'version': 1, 'active': version, 'previous': state['active']})
            return self.state()

    def active(self):
        with self.locked():
            version = self.state()['active']
            if version is None:
                raise ValueError('no active artifact release')
            release, payload = self.verify(version)
            return release, payload


def prepare(bundle, recipe, url, output):
    """Create publishable metadata only after checking a real bundle against the pinned recipe."""
    bundle = no_links(bundle); output = no_links(output)
    identifier(recipe['release_id']); check_url(url)
    if output.exists():
        raise ValueError('release output must be fresh')
    result = inspect_bundle(bundle)
    for key in ('corpus', 'models'):
        if result[key] != recipe[key]:
            raise ValueError('bundle differs from pinned release recipe: ' + key)
    data = validate_release(dict(schema=SCHEMA, release_id=recipe['release_id'], source_commit=recipe['source_commit'],
        bundle=dict(name=Path(urllib.parse.urlsplit(url).path).name, url=url, bytes=bundle.stat().st_size, sha256=digest(bundle)),
        **result, validation={'payload_integrity': 'verified', 'generation_calls': 0, 'live_audits': 'not_run'}))
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.prepare-', dir=output.parent))
    try:
        atomic_json(stage / 'release.json', data)
        manifest_sha = digest(stage / 'release.json')
        (stage / 'SHA256SUMS').write_text(f"{manifest_sha}  release.json\n{data['bundle']['sha256']}  {data['bundle']['name']}\n")
        os.rename(stage, output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {'release_id': data['release_id'], 'manifest_sha256': manifest_sha, 'output': str(output), 'published': False}
