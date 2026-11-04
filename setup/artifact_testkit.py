"""SYNTHETIC UNIT FIXTURES ONLY: no real SEC/model contents or quality/latency claims."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py'))
import artifact_manager as manager

PROFILE = dict(corpus_dir='data/corpus', ingest_report='indexes/ingest/report.json',
    original_provenance='data/sec/provenance.json', sparse_index='indexes/sparse',
    vector_index='indexes/vector', reranker_model='models/reranker',
    model_lock='data/models.json', source_enrollment='complete_report')


def encode(value):
    return (json.dumps(value, sort_keys=True) + '\n').encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def payload(label='one'):
    accession='000000000000000001'; raw=('synthetic filing ' + label).encode()
    url=f'https://www.sec.gov/Archives/edgar/data/1/{accession}/fixture.htm'
    original=encode({'fixture': 'original corpus'})
    corpus=b''.join(encode({'ID': f'{accession}#passage-{i}', 'Text': label, 'Ticker':'TST'}) for i in range(3))
    sections={section:{'chunks':1} for section in ('Item 1A','Item 7','Item 8')}
    report=dict(version=1,parser=manager.PARSER,raw_retained=True,skipped=[],chunks=3,
        sections={k:1 for k in sections},filings=[dict(
        ref=dict(Accession=accession,CIK='1',PrimaryDocument='fixture.htm',Ticker='TST',Form='10-K',FilingDate='2024-01-01'),
        download=dict(url=url,bytes=len(raw),sha256=sha(raw)),
        parse=dict(parser=manager.PARSER,raw_sha256=sha(raw),raw_bytes=len(raw),sections=sections,chunks=3))])
    report_bytes=encode(report)
    originals=dict(corpus_sha256=sha(original),sources=[dict(accession=accession,ticker='TST',cik='1',filing_year=2024,form='10-K',url=url)])
    provenance=dict(corpus_sha256=sha(corpus),new_sec_report_sha256=sha(report_bytes),sec_sha256=sha(original))
    files={'setup-profile.json':encode(PROFILE),'data/corpus/chunks.jsonl':corpus,
        'data/corpus/provenance.json':encode(provenance),'data/sec/chunks.jsonl':original,
        'data/sec/provenance.json':encode(originals),'indexes/ingest/report.json':report_bytes,
        f'indexes/ingest/raw/{accession}.raw':raw,'indexes/sparse/CORPUS_SHA256':sha(corpus).encode(),'indexes/sparse/MANIFEST':b'synthetic sparse manifest'}
    lock={}
    for i,name in enumerate(sorted(manager.MODELS),1):
        revision=str(i)*40; weights=('synthetic weights '+name).encode()
        lock[name]=dict(revision=revision,weights_sha256=sha(weights),path='../models/snapshots/'+revision)
        files['models/snapshots/'+revision+'/model.safetensors']=weights
        for name,value in [('config.json',b'{}'),('tokenizer_config.json',b'{}'),('vocab.txt',b'unit fixture vocabulary')]:
            files['models/snapshots/'+revision+'/'+name]=value
    vector=dict(corpus_sha256=sha(corpus),documents=3,dimension=384,subvectors=128,nbits=8,
        normalized=True,model='BAAI/bge-small-en-v1.5',model_revision=lock['BAAI/bge-small-en-v1.5']['revision'],files={})
    for name in ('index.pq8.faiss','vectors.f32.npy','passages.sqlite','centroids.npy'):
        value=('synthetic native artifact '+name).encode(); files['indexes/vector/'+name]=value;vector['files'][name]=sha(value)
    files['indexes/vector/manifest.json']=encode(vector)
    files['data/models.json']=encode(lock)
    onnx=b'synthetic ONNX artifact'; files['models/reranker/model.onnx']=onnx
    cross=lock['cross-encoder/ms-marco-MiniLM-L6-v2']
    files['models/reranker/manifest.json']=encode(dict(model_revision=cross['revision'],weights_sha256=cross['weights_sha256'],dtype='float32',max_length=512,files={'model.onnx':sha(onnx)}))
    return files


def archive(path, files):
    manifest=dict(version=1,files={name:dict(bytes=len(value),sha256=sha(value)) for name,value in files.items()})
    with tarfile.open(path,'w') as tar:
        for name,value in {**files,'bundle-manifest.json':encode(manifest)}.items():
            info=tarfile.TarInfo(name);info.size=len(value);tar.addfile(info,io.BytesIO(value))
    return path


def release(root, label='one'):
    bundle=archive(root/(label+'.tar'),payload(label))
    result=manager.inspect_bundle(bundle)
    recipe=dict(release_id='test-'+label,source_commit='a'*40,corpus=result['corpus'],models=result['models'])
    output=root/('prepared-'+label)
    manager.prepare(bundle,recipe,'https://example.invalid/'+label+'.tar',output)
    raw=(output/'release.json').read_bytes()
    return bundle,manager.json_bytes(raw),raw
