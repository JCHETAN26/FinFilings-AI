#!/usr/bin/env python3
"""Download four public 10-Ks and stream non-overlapping visible-text passages to JSONL.
Standard library only. No fabricated text, repetition, or corpus-sized in-memory list.
"""
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import time
import urllib.request

FILINGS = (
    ('AAPL', '320193', 2023, '000032019323000106', 'aapl-20230930.htm'),
    ('MSFT', '789019', 2024, '000095017024087843', 'msft-20240630.htm'),
    ('NVDA', '1045810', 2024, '000104581024000029', 'nvda-20240128.htm'),
    ('AMZN', '1018724', 2024, '000101872424000008', 'amzn-20231231.htm'),
)


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            h.update(block)
    return h.hexdigest()


class Passages(HTMLParser):
    def __init__(self, output, ticker, cik, year, accession, words):
        super().__init__(convert_charrefs=True)
        self.output, self.ticker, self.cik = output, ticker, cik
        self.year, self.accession, self.limit = year, accession, words
        self.stack, self.pending = [], []
        self.count, self.word_count = 0, 0
        self.word_tail = ""

    def handle_starttag(self, tag, attrs):
        self.flush_word()
        if tag in ('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'):
            return
        attrs = dict(attrs)
        style = attrs.get('style', '').lower().replace(' ', '')
        hidden = (bool(self.stack and self.stack[-1][1]) or
                  tag in ('script', 'style', 'head', 'ix:header', 'ix:hidden') or
                  'hidden' in attrs or 'display:none' in style or 'visibility:hidden' in style)
        self.stack.append((tag, hidden))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        self.flush_word()
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.stack and self.stack[-1][1]:
            return
        text = self.word_tail + data
        self.word_tail = ""
        if len(text) > 1 << 20:
            raise ValueError("visible text fragment exceeds 1 MiB")
        words = text.split()
        if text and not text[-1].isspace() and words:
            self.word_tail = words.pop()
        for word in words:
            self.add_word(word)

    def add_word(self, word):
        self.pending.append(word)
        if len(self.pending) == self.limit:
            self.emit()

    def flush_word(self):
        if self.word_tail:
            word, self.word_tail = self.word_tail, ""
            self.add_word(word)

    def close(self):
        super().close()
        self.flush_word()

    def emit(self):
        if not self.pending:
            return
        record = dict(ID=f'{self.accession}#passage-{self.count:06d}', Ticker=self.ticker,
                      CIK=self.cik, FormType='10-K', FilingYear=self.year,
                      SectionItem='Full filing (item not classified)', Text=' '.join(self.pending))
        self.output.write(json.dumps(record, ensure_ascii=False) + '\n')
        self.count += 1
        self.word_count += len(self.pending)
        self.pending.clear()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=Path, default=Path('data/sec'))
    ap.add_argument('--words', type=int, default=256)
    ap.add_argument('--user-agent', default=os.environ.get('SEC_USER_AGENT'))
    args = ap.parse_args()
    if not args.user_agent or not 32 <= args.words <= 2048:
        ap.error('provide --user-agent with contact information, and --words in [32, 2048]')
    args.out.mkdir(parents=True, exist_ok=True)
    sources = []
    tmp = args.out / 'chunks.jsonl.tmp'
    try:
        with tmp.open('w', encoding='utf-8') as output:
            for ticker, cik, year, accession, filename in FILINGS:
                url = f'https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{filename}'
                raw = args.out / f'{ticker}-{accession}.html'
                if not raw.exists():
                    request = urllib.request.Request(url, headers={'User-Agent': args.user_agent, 'Accept-Encoding': 'identity'})
                    partial = raw.with_suffix('.download')
                    try:
                        with urllib.request.urlopen(request, timeout=60) as response, partial.open('wb') as target:
                            if 'html' not in response.headers.get('Content-Type', '').lower():
                                raise ValueError(f'{url}: expected HTML')
                            size = 0
                            for block in iter(lambda: response.read(65536), b''):
                                size += len(block)
                                if size > 32 << 20:
                                    raise ValueError('filing exceeds 32 MiB download limit')
                                target.write(block)
                        partial.replace(raw)
                    finally:
                        partial.unlink(missing_ok=True)
                    time.sleep(1)  # At most one serial request per second; no retry burst.
                parser = Passages(output, ticker, cik, year, accession, args.words)
                with raw.open(encoding='utf-8') as source:
                    for block in iter(lambda: source.read(65536), ''):
                        parser.feed(block)
                parser.close()
                parser.emit()
                if parser.count < 10:
                    raise ValueError(f'{ticker}: too few passages; refusing probable error document')
                sources.append(dict(ticker=ticker, cik=cik, filing_year=year, form='10-K', url=url,
                                    accession=accession, raw_sha256=sha256(raw), raw_bytes=raw.stat().st_size,
                                    passages=parser.count, whitespace_words=parser.word_count))
                print(f'{ticker}: {parser.count} passages, {parser.word_count} words', flush=True)
        tmp.replace(args.out / 'chunks.jsonl')
    finally:
        tmp.unlink(missing_ok=True)
    corpus = args.out / 'chunks.jsonl'
    manifest = dict(acquired_at_utc=datetime.now(timezone.utc).isoformat(), words_per_passage=args.words,
                    chunking='non-overlapping visible HTML text; no duplication; items unclassified',
                    corpus_sha256=sha256(corpus), corpus_bytes=corpus.stat().st_size,
                    passages=sum(s['passages'] for s in sources), sources=sources)
    (args.out / 'provenance.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
