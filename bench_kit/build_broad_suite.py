#!/usr/bin/env python3
"""Build the frozen 30-case broad validation suite from the enrolled expanded corpus.

Offline only: reads the checksum-verified corpus, the original and ingestion provenance, and the
saved ten-audit report. It never contacts a model, a retrieval service or the network.

Positive topics were chosen after reading the actual passages named in each anchor. Anchors are
reviewer evidence for source-informed regression fixtures, not independent human gold and not an
answer-accuracy label. They are stored here but are never passed to the agent.
"""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_suite as suite_lib  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FROZEN_DATE = '2026-11-01'
LABEL = ('Source-informed regression fixtures. Prompts for audit01..audit10 are the historical regression prompts verbatim; '
         'broad11..broad30 were written after reading the cited passages. Anchors are reviewer evidence only: not independent '
         'human gold, not an answer-accuracy score, and never exposed to the agent. Availability means the filing year '
         '(and exact section) exists in the enrolled corpus, not that any topic is answerable.')


def S(*pairs):
    return [dict(ticker=t, year=y) for t, y in pairs]


def A(ticker, year, suffix, needle, topic):
    return dict(ticker=ticker, year=year, suffix=suffix, needle=needle, topic=topic)


# (id, kind, prompt, scopes, section, anchors)
NEW_CASES = [
    ('broad11', 'positive', 'Compare KO and PG, both filing year 2025, with the exact SectionItem filter "Item 7": retrieve each company\'s disclosure of how foreign currency exchange rates affected its reported revenue or sales. Use separate company-specific retrieval steps and do not calculate new figures.',
     S(('KO', 2025), ('PG', 2025)), 'Item 7',
     [A('KO', 2025, 'item7#passage-000066', r'total impact of foreign currency exchange rate fluctuations on net operating revenues', 'currency effect on revenue'),
      A('PG', 2025, 'item7#passage-000029', r'unfavorable foreign exchange', 'currency effect on net sales')]),
    ('broad12', 'positive', 'Compare PFE and MRK, both filing year 2025, with the exact SectionItem filter "Item 1A": retrieve each company\'s risk disclosures about patent protection and generic or biosimilar competition. Use separate company-specific retrieval steps.',
     S(('PFE', 2025), ('MRK', 2025)), 'Item 1A',
     [A('PFE', 2025, 'item1A#passage-000029', r'generic companies are employing aggressive strategies', 'generic challenges to patents'),
      A('MRK', 2025, 'item1A#passage-000003', r'dependent on its patent rights', 'dependence on patent rights')]),
    ('broad13', 'positive', 'Audit BAC filing year 2026, with the exact SectionItem filter "Item 7": separately retrieve regulatory capital ratio disclosures (common equity tier 1) and liquidity coverage ratio disclosures. Present supporting excerpts without deriving new numbers.',
     S(('BAC', 2026)), 'Item 7',
     [A('BAC', 2026, 'item7#passage-000060', r'Common equity tier 1 capital ratio', 'CET1 capital metrics'),
      A('BAC', 2026, 'item7#passage-000075', r'Liquidity Coverage Ratio', 'LCR and HQLA')]),
    ('broad14', 'positive', 'Audit TGT filing year 2026: separately retrieve the inventory shrink estimate and its sensitivity, and the capital expenditure disclosures. Support both topics with direct excerpts.',
     S(('TGT', 2026)), '',
     [A('TGT', 2026, 'item7#passage-000016', r'shrink estimate', 'shrink reserve sensitivity'),
      A('TGT', 2026, 'item7#passage-000012', r'Capital expenditures in 2025', 'capital expenditures')]),
    ('broad15', 'positive', 'Audit TSLA filing year 2026: separately retrieve tariff and trade-policy risks affecting supply chain costs, and cost of revenues and gross margin disclosures. Use direct excerpts only.',
     S(('TSLA', 2026)), '',
     [A('TSLA', 2026, 'item1A#passage-000002', r'heightened import tariffs', 'tariff supply-chain cost'),
      A('TSLA', 2026, 'item7#passage-000020', r'Cost of Revenues and Gross Margin', 'cost of revenues table')]),
    ('broad16', 'positive', 'Compare V filing year 2024 and V filing year 2025, with the exact SectionItem filter "Item 1A": retrieve the disclosed risks from regulation of interchange rates in each filing year. Retrieve evidence separately for each filing year.',
     S(('V', 2024), ('V', 2025)), 'Item 1A',
     [A('V', 2024, 'item1A#passage-000003', r'debit interchange', 'interchange regulation 2024 filing'),
      A('V', 2025, 'item1A#passage-000004', r'interchange', 'interchange regulation 2025 filing')]),
    ('broad17', 'positive', 'Compare COP and OXY, both filing year 2025, with the exact SectionItem filter "Item 7": retrieve each company\'s disclosure of how commodity prices affect earnings, cash flows or asset valuation. Retrieve evidence separately for each company.',
     S(('COP', 2025), ('OXY', 2025)), 'Item 7',
     [A('COP', 2025, 'item7#passage-000008', r'earnings and operating cash flows generally correlate', 'commodity price sensitivity'),
      A('OXY', 2025, 'item7#passage-000059', r'prolonged declines in commodity prices', 'commodity prices and valuation')]),
    ('broad18', 'positive', 'Compare MET and AFL, both filing year 2025: retrieve MET\'s interest rate risk disclosures and AFL\'s yen and foreign currency translation disclosures. Use separate company-specific retrieval steps and do not derive new numbers.',
     S(('MET', 2025), ('AFL', 2025)), '',
     [A('MET', 2025, 'item1A#passage-000001', r'Interest Rate Risks', 'interest rate risk'),
      A('AFL', 2025, 'item7#passage-000018', r'Aflac Japan.s premiums', 'yen translation')]),
    ('broad19', 'positive', 'Compare CRM and ADBE, both filing year 2026, with the exact SectionItem filter "Item 8": retrieve each company\'s remaining performance obligation (contracted revenue not yet recognized) disclosures. Use separate company-specific retrieval steps.',
     S(('CRM', 2026), ('ADBE', 2026)), 'Item 8',
     [A('CRM', 2026, 'item8#passage-000036', r'Remaining performance obligation represents', 'RPO definition'),
      A('ADBE', 2026, 'item8#passage-000027', r'remaining performance obligations represents contracted revenue', 'RPO definition')]),
    ('broad20', 'positive', 'Compare AMD and TXN, both filing year 2026: retrieve AMD\'s dependence on third-party foundries such as TSMC and TXN\'s capital expenditures for semiconductor manufacturing. Use separate company-specific retrieval steps.',
     S(('AMD', 2026), ('TXN', 2026)), '',
     [A('AMD', 2026, 'item1A#passage-000023', r'If TSMC is not able to manufacture wafers', 'foundry dependence'),
      A('TXN', 2026, 'item7#passage-000007', r'Capital expenditures were', 'manufacturing capital expenditures')]),
    ('broad21', 'positive', 'Compare UNH filing year 2025 and CVS filing year 2026, both with the exact SectionItem filter "Item 7": retrieve how each company defines or discusses its medical care ratio or medical benefit ratio. Retrieve evidence separately for each company.',
     S(('UNH', 2025), ('CVS', 2026)), 'Item 7',
     [A('UNH', 2025, 'item7#passage-000008', r'Medical care ratio \(MCR\) is calculated', 'MCR definition'),
      A('CVS', 2026, 'item7#passage-000017', r'Medical benefit ratio is calculated', 'MBR definition')]),
    ('broad22', 'positive', 'Compare SBUX filing year 2024 and SBUX filing year 2025, with the exact SectionItem filter "Item 7": retrieve comparable store sales and new store opening disclosures for each filing year. Retrieve evidence separately for each filing year.',
     S(('SBUX', 2024), ('SBUX', 2025)), 'Item 7',
     [A('SBUX', 2024, 'item7#passage-000002', r'comparable store sales', 'comparable store sales 2024 filing'),
      A('SBUX', 2025, 'item7#passage-000002', r'net new company-operated store growth', 'store growth 2025 filing')]),
    ('broad23', 'positive', 'Audit ISRG filing year 2026, with the exact SectionItem filter "Item 7": separately retrieve tariff impacts on product cost and da Vinci procedure growth disclosures. Ground both topics in the filing without calculating new rates.',
     S(('ISRG', 2026)), 'Item 7',
     [A('ISRG', 2026, 'item7#passage-000007', r'subject to tariffs', 'tariff product cost'),
      A('ISRG', 2026, 'item7#passage-000024', r'U\.S\. da Vinci procedures grew', 'procedure growth')]),
    ('broad24', 'positive', 'Compare LLY and JNJ, both filing year 2026, with the exact SectionItem filter "Item 7": retrieve LLY\'s product revenue drivers such as Mounjaro and Zepbound and JNJ\'s segment sales disclosures. Use separate company-specific retrieval steps.',
     S(('LLY', 2026), ('JNJ', 2026)), 'Item 7',
     [A('LLY', 2026, 'item7#passage-000011', r'driven by Mounjaro and Zepbound', 'LLY product revenue drivers'),
      A('JNJ', 2026, 'item7#passage-000004', r'Innovative Medicine segment sales in 2025', 'JNJ segment sales')]),
    ('broad25', 'positive', 'Compare HPE filing year 2024 and HPE filing year 2025, with the exact SectionItem filter "Item 7": retrieve share repurchase authorization and repurchase activity disclosures for each filing year. Retrieve evidence separately for each filing year.',
     S(('HPE', 2024), ('HPE', 2025)), 'Item 7',
     [A('HPE', 2024, 'item7#passage-000036', r'remaining authorization of approximately', 'repurchase authorization 2024 filing'),
      A('HPE', 2025, 'item7#passage-000036', r'remaining authorization of approximately', 'repurchase authorization 2025 filing')]),
    ('broad26', 'positive', 'Compare HD and WMT, both filing year 2026, with the exact SectionItem filter "Item 8": retrieve each company\'s segment reporting disclosures. Use separate company-specific retrieval steps.',
     S(('HD', 2026), ('WMT', 2026)), 'Item 8',
     [A('HD', 2026, 'item8#passage-000026', r'SEGMENT REPORTING AND NET SALES', 'segment reporting'),
      A('WMT', 2026, 'item8#passage-000055', r'Segments and Disaggregated Revenue', 'segments and disaggregated revenue')]),
    ('broad27', 'negative', 'Audit TSLA filing year 2024 only: separately retrieve automotive gross margin disclosures and tariff or supply-chain risks. Do not substitute filing year 2025 or 2026, or any other company, if 2024 evidence is unavailable.',
     S(('TSLA', 2024)), '', []),
    ('broad28', 'negative', 'Audit NVDA filing year 2024, with the exact SectionItem filter "Item 7": separately retrieve Data Center revenue discussion and supply cost commentary. Do not relax the exact section filter if only unclassified passages are available.',
     S(('NVDA', 2024)), 'Item 7', []),
    ('broad29', 'mixed', 'Compare GS filing year 2026 and BAC filing year 2025: retrieve each firm\'s regulatory capital ratio disclosures. Retrieve evidence separately for each company and do not substitute another filing year if one filing is unavailable.',
     S(('GS', 2026), ('BAC', 2025)), '',
     [A('GS', 2026, 'item8#passage-000178', r'calculates its CET1 capital, Tier 1 capital and Total capital ratios', 'firm capital ratios')]),
    ('broad30', 'mixed', 'Compare KO filing year 2025 and AMZN filing year 2024, both with the exact SectionItem filter "Item 7": retrieve each company\'s foreign currency impact disclosures. Retrieve evidence separately for each company and do not relax the section filter.',
     S(('KO', 2025), ('AMZN', 2024)), 'Item 7',
     [A('KO', 2025, 'item7#passage-000066', r'total impact of foreign currency exchange rate fluctuations on net operating revenues', 'currency effect on revenue')]),
]


def excerpt(text: str, needle: str) -> str:
    flat = suite_lib.normalize(text)
    match = re.search(needle, flat, re.I)
    if match is None:
        raise ValueError(f'anchor needle not found: {needle!r}')
    start = max(0, match.start() - 80)
    while start and flat[start - 1] != ' ':
        start -= 1
    end = min(len(flat), start + 500)
    while end < len(flat) and flat[end] != ' ':
        end -= 1
    return flat[start:end]


def build(corpus: Path, original: Path, report: Path, history: Path, output: Path) -> dict:
    sources = suite_lib.build_trusted_sources(original, report)
    accessions = {s['accession'] for s in sources}
    by_scope = {(s['ticker'], s['filing_year']): s['accession'] for s in sources}
    sys.path.insert(0, str(ROOT / 'py'))
    import eval_agent  # historical regression prompts, verbatim
    regression = eval_agent.cases()
    for case in regression:  # cross-check against the saved real-run prompts
        saved = json.loads((history / f"{case['id']}.json").read_text())['prompt']
        if saved != case['prompt']:
            raise ValueError(f"{case['id']} prompt differs from saved historical run")
    wanted = set()
    for case_id, _, _, scopes, _, anchors in NEW_CASES:
        for anchor in anchors:
            accession = by_scope[(anchor['ticker'], anchor['year'])]
            anchor['id'] = f"{accession}#{anchor['suffix']}"
            wanted.add(anchor['id'])
    sections, found = suite_lib.scan_corpus(corpus, accessions, wanted)
    if set(found) != wanted:
        raise ValueError(f'anchor passages missing from corpus: {sorted(wanted - set(found))}')
    skipped = {s['ref']['Ticker'] for s in json.loads(report.read_text())['skipped']}
    cases = []

    def expected(scopes, section):
        return [suite_lib.scope_availability(sources, sections, s['ticker'], s['year'], section) for s in scopes]

    for case in regression:
        scopes = [dict(ticker=t, year=y) for t, y in case['scopes']]
        cases.append(dict(id=case['id'], kind='regression', prompt=case['prompt'], scopes=scopes,
            section=case.get('section', ''), expected_scopes=expected(scopes, case.get('section', ''))))
    for case_id, kind, prompt, scopes, section, anchors in NEW_CASES:
        rows = []
        for anchor in anchors:
            chunk = found[anchor['id']]
            if section and chunk['SectionItem'] != section:
                raise ValueError(f"{case_id}: anchor {anchor['id']} is outside exact section {section}")
            rows.append(dict(ticker=anchor['ticker'], year=anchor['year'], id=anchor['id'], section_item=chunk['SectionItem'],
                topic=anchor['topic'], excerpt=excerpt(chunk['Text'], anchor['needle']),
                passage_text_sha256=suite_lib.sha256_text(chunk['Text'])))
        entry = dict(id=case_id, kind=kind, prompt=prompt, scopes=scopes, section=section, expected_scopes=expected(scopes, section))
        if anchors:
            entry['reviewer_evidence'] = dict(label='source-informed regression fixture; not independent gold; not exposed to the agent', anchors=rows)
        for row in entry['expected_scopes']:
            if not row['available'] and row['reason'] == 'filing_year_absent' and row['ticker'] in skipped:
                raise ValueError(f"{case_id}: {row['ticker']} {row['year']} is absent because ingestion skipped it, not because it was never enrolled")
        cases.append(entry)
    suite = dict(schema=suite_lib.SCHEMA, suite_id='broad-validation-v1', frozen_on=FROZEN_DATE, label=LABEL,
        corpus_sha256=suite_lib.sha256_file(corpus), enrolled_filings=len(sources), case_count=len(cases),
        case_ids=[c['id'] for c in cases], cases=cases)
    suite_lib.validate_suite(suite)
    suite_lib.check_expected_scopes(suite, sources, sections)
    if output.exists():
        raise FileExistsError(f'{output} already exists; a frozen suite is never overwritten')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(suite, indent=2, ensure_ascii=False) + '\n')
    digest = suite_lib.sha256_file(output)
    output.with_suffix('.sha256').write_text(f'{digest}  {output.name}\n')
    return dict(suite=str(output), sha256=digest, coverage=suite_lib.suite_coverage(suite))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=ROOT / 'data/sec-scale-v2-evaluation-20261031/chunks.jsonl')
    parser.add_argument('--original-provenance', type=Path, default=ROOT / 'data/sec/provenance.json')
    parser.add_argument('--ingest-report', type=Path, default=ROOT / 'indexes/sec-scale-v2-20261031/report.json')
    parser.add_argument('--history', type=Path, default=ROOT / 'bench_kit/results/phase3-expanded-20261031/evaluation')
    parser.add_argument('--output', type=Path, default=ROOT / 'bench_kit/suites/broad-validation-v1.json')
    args = parser.parse_args()
    print(json.dumps(build(args.corpus, args.original_provenance, args.ingest_report, args.history, args.output), indent=2))
