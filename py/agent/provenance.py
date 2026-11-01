"""Disk-backed identity AND text verification against the acquired SEC corpus."""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from contextlib import closing
from typing import Any
from urllib.parse import urlparse
from .filters import FilingFilter
from .models import Evidence


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            checksum.update(block)
    return checksum.hexdigest()


class ProvenanceRegistry:
    def __init__(self, corpus: Path, provenance: Path):
        self.corpus, self.provenance = corpus, provenance
        manifest = json.loads(provenance.read_text())
        if digest(corpus) != manifest['corpus_sha256']:
            raise ValueError('SEC source corpus checksum mismatch')
        self.sources = {source['accession']: source for source in manifest['sources']}
        if not 1 <= len(self.sources) <= 10000 or len(self.sources) != len(manifest['sources']):
            raise ValueError('registry must contain 1..10000 unique filings')
        for accession, source in self.sources.items():
            url = urlparse(source['url'])
            if not re.fullmatch(r'\d{18}', accession) or url.scheme != 'https' or url.netloc != 'www.sec.gov' or url.query or url.fragment:
                raise ValueError('invalid SEC provenance')
            if not url.path.startswith(f"/Archives/edgar/data/{source['cik']}/{accession}/"):
                raise ValueError('source URL does not match CIK/accession')
        # Read actual cover-page text. Filing year and fiscal period can differ.
        self.periods: dict[str, str] = {}
        self.sections: dict[str, set[str]] = {a: set() for a in self.sources}
        # Disk-backed offsets avoid both corpus-sized passage lists and a full
        # corpus scan for each retrieved citation. Original bytes remain authoritative.
        self._temporary = tempfile.TemporaryDirectory(prefix='finfilings-provenance-')
        self._offsets = Path(self._temporary.name) / 'offsets.sqlite'
        with closing(sqlite3.connect(self._offsets)) as db, corpus.open('rb') as stream:
            db.execute('CREATE TABLE offsets (id TEXT PRIMARY KEY, position INTEGER NOT NULL)')
            while True:
                position = stream.tell()
                line = stream.readline(4 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 4 * 1024 * 1024:
                    raise ValueError('SEC record exceeds 4 MiB')
                chunk = json.loads(line)
                accession = chunk['ID'].split('#')[0]
                if accession not in self.sources:
                    continue  # Published FinQA contexts do not establish SEC provenance.
                if not re.fullmatch(r'\d{18}#(?:item(?:1A|7|8)#)?passage-\d{6}', chunk['ID']):
                    raise ValueError('unrecognized SEC passage identifier')
                db.execute('INSERT INTO offsets VALUES (?,?)', (chunk['ID'], position))
                self.sections[accession].add(chunk['SectionItem'])
                if chunk['ID'].endswith('#passage-000000'):
                    match = re.search(r'for the fiscal year ended\s+([A-Za-z]+\s+\d{1,2}\s*,\s*\d{4})', chunk['Text'], re.I)
                    if match:
                        self.periods[accession] = ' '.join(match[1].split())
            db.commit()

    def close(self) -> None:
        self._temporary.cleanup()

    def catalog(self) -> list[dict[str, Any]]:
        return [dict(ticker=s['ticker'], filing_year=s['filing_year'], accession=a,
                     fiscal_period_end=self.periods.get(a),
                     form=s['form'], sections=sorted(self.sections[a])) for a, s in self.sources.items()]

    def resolve_filter(self, filters: FilingFilter) -> FilingFilter:
        if not filters.ticker or not filters.year:
            raise ValueError('planner must specify ticker and filing year for every subquery')
        matches = [a for a, s in self.sources.items() if s['ticker'] == filters.ticker and s['filing_year'] == filters.year]
        if filters.accession and filters.accession not in matches:
            raise ValueError('planner supplied an accession inconsistent with the trusted catalog')
        if len(matches) == 1:
            return filters.model_copy(update={'accession': matches[0]})
        return filters  # Missing filings stay missing: never substitute a different year.

    def verify(self, hit: dict[str, Any], filters: FilingFilter) -> Evidence | None:
        identifier, metadata, text = hit.get('id'), hit.get('metadata'), hit.get('text')
        if not isinstance(identifier, str) or not isinstance(metadata, dict) or not isinstance(text, str):
            raise ValueError('malformed retrieved evidence')
        if metadata.get('ID') != identifier or not filters.matches(metadata):
            raise ValueError('backend returned evidence outside filing filters')
        match = re.fullmatch(r'(\d{18})#(?:(item(?:1A|7|8))#)?passage-(\d{6})', identifier)
        if not match or match[1] not in self.sources:
            return None  # FinQA has no authoritative accession and cannot become a verified claim.
        accession, section, number = match.groups()
        source = self.sources[accession]
        for key, value in [('Ticker', source['ticker']), ('CIK', source['cik']), ('FilingYear', source['filing_year']), ('FormType', source['form'])]:
            if metadata.get(key) != value:
                raise ValueError('retrieved passage provenance mismatch')
        with closing(sqlite3.connect(self._offsets)) as db:
            row = db.execute('SELECT position FROM offsets WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('unknown SEC passage ID')
        with self.corpus.open('rb') as stream:
            stream.seek(row[0])
            line = stream.readline(4 * 1024 * 1024 + 1)
            if len(line) > 4 * 1024 * 1024:
                raise ValueError('SEC record exceeds 4 MiB')
            original = json.loads(line)
        expected = {k: v for k, v in original.items() if k not in ('Text', 'Tokens')}
        if original['ID'] != identifier or text != original['Text'] or metadata != expected:
            raise ValueError('retrieved content differs from verified SEC source')
        suffix = f'{section}-p{number}' if section else f'p{number}'
        return Evidence(id=identifier, text=text, metadata=metadata,
            accession=f'{accession[:10]}-{accession[10:12]}-{accession[12:]}', url=source['url'],
            tag=f"{source['ticker']}-{source['filing_year']}-{source['form'].replace('-', '')}-{suffix}")
