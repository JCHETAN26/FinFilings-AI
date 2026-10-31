"""Deterministic two-pass 10-K heading selection and section-bounded chunks."""
from collections import deque
from dataclasses import dataclass
import codecs
import hashlib
from html.parser import HTMLParser
import json
import logging
from pathlib import Path
import re
import tempfile


@dataclass(frozen=True)
class Filing:
    ticker: str
    cik: str
    year: int
    accession: str
    form: str = '10-K'

    def validate(self) -> None:
        if not all(isinstance(value,str) for value in (self.ticker,self.cik,self.accession,self.form)) or type(self.year) is not int:
            raise ValueError('filing strings and integer filing year are required')
        if not re.fullmatch(r'[A-Z0-9.\-]{1,16}', self.ticker) or not re.fullmatch(r'\d{1,10}',self.cik):
            raise ValueError('invalid ticker/CIK')
        if not 1993 <= self.year <= 2100 or not re.fullmatch(r'\d{18}',self.accession) or self.form not in ('10-K','10-K/A'):
            raise ValueError('require a 10-K identity, 18-digit accession and filing year 1993..2100')


class VisibleLines(HTMLParser):
    """Stream normalized block text; suppress hidden/IX metadata and mark links."""
    def __init__(self, output):
        super().__init__(convert_charrefs=True)
        self.output, self.stack, self.parts = output, [], []
        self.size, self.linked, self.words = 0, False, 0
        self.blocks = frozenset(('p','div','tr','li','h1','h2','h3','h4','h5','h6','table','br','hr'))
        self.voids = frozenset(('area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'))

    def flush(self) -> None:
        text = ' '.join(''.join(self.parts).split())
        if text:
            self.output.write((str(int(self.linked))+'\t'+text+'\n').encode())
            self.words += len(text.split())
        self.parts.clear(); self.size, self.linked = 0, False

    def handle_starttag(self, tag, attrs):
        if tag in self.blocks:
            self.flush()
        attrs = dict(attrs)
        hidden = bool(self.stack and self.stack[-1][1]) or tag in ('script','style','head','ix:header','ix:hidden') or 'hidden' in attrs
        style = attrs.get('style','').lower().replace(' ','')
        hidden = hidden or 'display:none' in style or 'visibility:hidden' in style
        linked = bool(self.stack and self.stack[-1][2]) or (tag=='a' and bool(attrs.get('href')))
        if tag not in self.voids:
            if len(self.stack) >= 256:
                raise ValueError('HTML nesting exceeds 256')
            self.stack.append((tag,hidden,linked))

    def handle_endtag(self, tag):
        if tag in self.blocks:
            self.flush()
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:
                del self.stack[i:];break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag,attrs);self.handle_endtag(tag)

    def handle_data(self, data):
        if self.stack and self.stack[-1][1]:
            return
        self.size += len(data.encode())
        if self.size > 1 << 20 or len(self.parts) >= 8192:
            raise ValueError('visible block exceeds bounded parser capacity')
        self.parts.append(data)
        self.linked |= bool(self.stack and self.stack[-1][2])


@dataclass(frozen=True)
class Line:
    start: int
    end: int
    words_before: int
    text: str
    linked: bool


@dataclass(frozen=True)
class Heading:
    item: str
    start: int
    words_before: int


def lines(path: Path):
    count = 0
    with path.open('rb') as source:
        while True:
            start = source.tell()
            raw = source.readline((1 << 20)+16)
            if not raw:
                break
            if not raw.endswith(b'\n'):
                raise ValueError('invalid or oversized visible text record')
            flag, text = raw.decode().rstrip('\n').split('\t',1)
            yield Line(start,source.tell(),count,text,flag=='1')
            count += len(text.split())


class SectionParser:
    def __init__(self, words_per_chunk: int = 256, min_section_words: int = 128, max_input_bytes: int = 128 << 20):
        if not 32 <= words_per_chunk <= 1024 or not 32 <= min_section_words <= 4096:
            raise ValueError('invalid chunk/section limits')
        self.words_per_chunk, self.minimum = words_per_chunk, min_section_words
        if not 1 <= max_input_bytes <= 512 << 20:
            raise ValueError('invalid input byte limit')
        self.max_input_bytes = max_input_bytes
        self.prefix = re.compile(r'^item\s*(\d{1,2}\s?[abc]?)(?:[.\s:–—-]+|$)(.*)$',re.I)
        self.titles = {
            '1':r'business', '1A':r'riskfactors', '1B':r'unresolvedstaffcomments', '1C':r'cybersecurity',
            '2':r'properties', '3':r'legalproceedings', '4':r'minesafetydisclosures',
            '5':r'marketfor.*(?:equity|securities|stock)(?:.*)',
            '6':r'(?:selectedfinancialdata|reserved)',
            '7':r'managementsdiscussionandanalysis(?:offinancialconditionandresultsofoperations|ofresultsofoperationsandfinancialcondition)?',
            '7A':r'quantitativeandqualitativedisclosuresaboutmarketrisk',
            '8':r'financialstatements(?:andsupplementarydata)?',
            '9':r'changesinanddisagreementswithaccountantsonaccountingandfinancialdisclosure',
            '9A':r'controlsandprocedures', '9B':r'otherinformation',
            '9C':r'disclosureregardingforeignjurisdictionsthatpreventinspections',
            '10':r'directorsexecutiveofficersandcorporategovernance',
            '11':r'executivecompensation',
            '12':r'securityownershipofcertainbeneficialownersandmanagementandrelatedstockholdermatters',
            '13':r'certainrelationshipsandrelatedtransactionsanddirectorindependence',
            '14':r'principalaccountantfeesandservices',
            '15':r'exhibits(?:andfinancialstatementschedules|financialstatementschedules)?',
            '16':r'form10ksummary',
        }
        self.log = logging.getLogger(__name__)

    def _heading(self, window: deque[Line]) -> str | None:
        first = window[0]
        if first.linked or len(first.text) > 350:
            return None
        match = self.prefix.fullmatch(first.text)
        if not match:
            return None
        item = match[1].replace(' ','').upper()
        pattern = self.titles.get(item)
        if pattern is None:
            return None
        tail = match[2]
        for index in range(min(4,len(window))):
            if index:
                following = window[index]
                if following.linked or self.prefix.match(following.text):
                    break
                tail += ' '+following.text
            compact = ''.join(re.findall(r'[a-z0-9]+',tail.casefold()))
            if re.fullmatch(pattern,compact):
                return item
            if tail.strip() and index == 0:
                # A split title may occupy several short block elements.
                if len(tail)>350:
                    break
        return None

    def _spool(self, raw: Path, output: Path, format: str) -> dict:
        checksum = hashlib.sha256()
        size = 0
        with output.open('wb') as target:
            if format=='html':
                parser = VisibleLines(target)
                decoder = codecs.getincrementaldecoder('utf-8')()
                with raw.open('rb') as source:
                    while block := source.read(65536):
                        checksum.update(block);size+=len(block)
                        if size>self.max_input_bytes:
                            raise ValueError('raw filing exceeds byte limit')
                        parser.feed(decoder.decode(block))
                        if len(parser.rawdata)>1 << 20:
                            raise ValueError('unconsumed HTML token exceeds 1 MiB')
                parser.feed(decoder.decode(b'',final=True))
                parser.close();parser.flush()
                words = parser.words
            elif format=='text':
                words=0
                with raw.open('rb') as source:
                    while line:=source.readline((1 << 20)+1):
                        if len(line)>1 << 20:
                            raise ValueError('plain-text line exceeds 1 MiB')
                        checksum.update(line);size+=len(line)
                        if size>self.max_input_bytes:
                            raise ValueError('raw filing exceeds byte limit')
                        text=' '.join(line.decode('utf-8').split())
                        if text:
                            target.write(('0\t'+text+'\n').encode());words+=len(text.split())
            else:
                raise ValueError('format must be html or text')
        return dict(raw_bytes=size,raw_sha256=checksum.hexdigest(),visible_words=words)

    def parse(self, raw: Path, output: Path, filing: Filing, format: str = 'html', spool_parent: Path | None = None) -> dict:
        filing.validate()
        if output.exists() or output.is_symlink():
            raise FileExistsError(output)
        output.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='finfilings-parser-',dir=spool_parent) as temp:
            spool=Path(temp)/'visible.lines'
            report=self._spool(raw,spool,format)
            candidates=[];window=deque()
            iterator=iter(lines(spool))
            for _ in range(4):
                entry=next(iterator,None)
                if entry is not None:window.append(entry)
            while window:
                item=self._heading(window)
                if item:
                    if not candidates or candidates[-1].item!=item:
                        candidates.append(Heading(item,window[0].start,window[0].words_before))
                        if len(candidates)>512:
                            raise ValueError('heading candidate limit exceeded')
                window.popleft()
                entry=next(iterator,None)
                if entry is not None:window.append(entry)
            sections={}
            for i,header in enumerate(candidates):
                end=candidates[i+1].start if i+1<len(candidates) else spool.stat().st_size
                end_words=candidates[i+1].words_before if i+1<len(candidates) else report['visible_words']
                count=end_words-header.words_before
                short_reference = False
                if header.item == '8' and 16 <= count < self.minimum:
                    with spool.open('rb') as source:
                        source.seek(header.start)
                        excerpt = source.read(min(4096,end-header.start)).decode('utf-8')
                    short_reference = bool(re.search(r'incorporat\w*\s+by\s+reference|information required by this item is set forth',excerpt,re.I))
                if header.item in ('1A','7','8') and (count>=self.minimum or short_reference):
                    if header.item in sections:
                        raise ValueError(f'ambiguous duplicate body heading: Item {header.item}')
                    sections[header.item]=(header.start,end,count)
            missing=set(('1A','7','8'))-sections.keys()
            if missing:
                raise ValueError(f'missing/unverified major sections {sorted(missing)}; candidates={[(h.item,h.words_before) for h in candidates]}')
            if not sections['1A'][0]<sections['7'][0]<sections['8'][0]:
                raise ValueError('major sections are out of 10-K order')
            counts={};total=0
            # Acquire exclusive ownership before the cleanup try block; a
            # racing caller's pre-existing output must never be unlinked.
            target = output.open('x',encoding='utf-8')
            try:
                with target, spool.open('rb') as source:
                    for item in ('1A','7','8'):
                        start,end,word_count=sections[item];source.seek(start);pending=[];number=0
                        def emit():
                            nonlocal number,total
                            if not pending:return
                            text=' '.join(pending)
                            if len(text.encode())>32768:
                                raise ValueError('chunk exceeds 32 KiB text budget')
                            record=dict(ID=f'{filing.accession}#item{item}#passage-{number:06d}',Ticker=filing.ticker,
                                CIK=filing.cik,FormType=filing.form,FilingYear=filing.year,SectionItem='Item '+item,Text=text)
                            target.write(json.dumps(record,ensure_ascii=False)+'\n');pending.clear();number+=1;total+=1
                        while source.tell()<end:
                            line=source.readline((1 << 20)+16)
                            if not line or source.tell()>end:
                                raise ValueError('invalid section byte range')
                            text=line.decode().rstrip('\n').split('\t',1)[1]
                            for word in text.split():
                                pending.append(word)
                                if len(pending)==self.words_per_chunk:emit()
                        emit();counts['Item '+item]=number
                        self.log.info('section_parsed item=%s words=%d chunks=%d',item,word_count,number)
            except BaseException:
                output.unlink(missing_ok=True);raise
            report.update(chunks=total,sections={f'Item {item}':dict(visible_start=start,visible_end=end,words=count,chunks=counts['Item '+item]) for item,(start,end,count) in sections.items()},candidate_headings=len(candidates),parser='deterministic-canonical-heading-v1')
            return report
