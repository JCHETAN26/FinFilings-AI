"""Sample prompts for the input box and the catalog-derived availability note shown with each.

Samples 1-2 are the historical regression prompts audit03 and audit01 (a test asserts they are byte-identical to
`bench_kit/eval_agent.py`). Sample 3 deliberately asks about a filing year; whether that filing exists is computed from the
actual enrolled catalog at startup, never assumed. No sample carries an expected answer.
"""
SAMPLES = (
    dict(id='single-filing', label='Risks in one filing',
         prompt='Audit NVDA filing year 2024: separately retrieve export control risks and dependence on external manufacturing capacity. Explain with filing excerpts only.',
         scopes=(('NVDA', 2024),)),
    dict(id='two-companies', label='Compare two companies',
         prompt='Compare AAPL filing year 2023 and NVDA filing year 2024: find the disclosed dependence on third-party manufacturing and supply-chain risks. Retrieve evidence separately for each company.',
         scopes=(('AAPL', 2023), ('NVDA', 2024))),
    dict(id='missing-year', label='Check a filing year that may be absent',
         prompt='Audit NVDA filing year 2023 only: separately retrieve revenue disclosures and share repurchase disclosures. Do not substitute any other filing year if 2023 evidence is unavailable.',
         scopes=(('NVDA', 2023),)),
)


def describe(samples, filings: list[dict]) -> list[dict]:
    present = {(f['ticker'], f['filing_year']) for f in filings}
    rows = []
    for sample in samples:
        scopes = [dict(ticker=t, filing_year=y, in_corpus=(t, y) in present) for t, y in sample['scopes']]
        missing = [f"{s['ticker']} {s['filing_year']}" for s in scopes if not s['in_corpus']]
        note = ('All requested filings are in this corpus.' if not missing else
                'Not in this corpus: ' + ', '.join(missing) + '. The audit should report an evidence gap rather than substitute another year.')
        rows.append(dict(id=sample['id'], label=sample['label'], prompt=sample['prompt'], scopes=scopes, note=note))
    return rows
