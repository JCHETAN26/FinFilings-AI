# Phase 3 — ten live financial audit prompts

Published audit claims are verified SEC excerpts. Critic rationales describe retrieval gaps and are not independently audited financial conclusions. This table reports model-graded retrieval coverage and deterministic source/citation checks, not human-labeled answer accuracy.

| Audit | Topic | Citation checks | Retries per subquery | Final grades | Evidence coverage |
|---|---|---:|---|---|---|
| [audit01](evaluation-20261031T061549Z/audit01.md) | AAPL/NVDA supply chain | 6/6 valid | 0, 0 | 5, 4 | Complete |
| [audit02](evaluation-20261031T061549Z/audit02.md) | MSFT/AMZN cloud competition and costs | 8/8 valid | 0, 2 | 4, 1 | Partial; see gaps |
| [audit03](evaluation-20261031T061549Z/audit03.md) | NVDA export controls and manufacturing | 8/8 valid | 0, 0 | 5, 5 | Complete |
| [audit04](evaluation-20261031T061549Z/audit04.md) | AMZN AWS sales and operating expenses | 6/6 valid | 0, 2 | 4, 1 | Partial; see gaps |
| [audit05](evaluation-20261031T061549Z/audit05.md) | AAPL repurchases and dividends | 5/5 valid | 0, 0 | 5, 5 | Complete |
| [audit06](evaluation-20261031T061549Z/audit06.md) | MSFT cloud growth and cybersecurity | 8/8 valid | 0, 0 | 4, 5 | Complete |
| [audit07](evaluation-20261031T061549Z/audit07.md) | AMZN cash flow and leases | 7/7 valid | 0, 0 | 5, 4 | Complete |
| [audit08](evaluation-20261031T061549Z/audit08.md) | NVDA Data Center growth and gross margin | 6/6 valid | 0, 0 | 5, 4 | Complete |
| [audit09](evaluation-20261031T061549Z/audit09.md) | AAPL 2025 — missing filing | 0/0 valid | 2, 2 | 1, 1 | Correct abstention |
| [audit10](evaluation-20261031T061549Z/audit10.md) | MSFT Item 1A — unclassified section | 0/0 valid | 2, 2 | 1, 1 | Correct abstention |

Retries exclude the initial retrieval and are bounded by two per subquery. Grades range 1–5; acceptance requires at least 4. Zero citations on the two unavailable-evidence cases are required abstentions, not a citation recall score.

## audit01

Compare AAPL filing year 2023 and NVDA filing year 2024: find the disclosed dependence on third-party manufacturing and supply-chain risks. Retrieve evidence separately for each company.

No below-threshold evidence gaps were reported.

Full evidence and citations: [audit01.md](evaluation-20261031T061549Z/audit01.md). Structured trace: [audit01.json](evaluation-20261031T061549Z/audit01.json).

## audit02

Compare MSFT and AMZN, both filing year 2024: retrieve evidence on cloud business competition and the operating costs of their cloud infrastructure. Use separate company-specific retrieval steps.

- Subquery 1: Amazon cloud business competition and operating costs of cloud infrastructure, including competitors, AWS infrastructure costs, and data center operating expenses
- Final critic rationale: All four passages are non-responsive to the AMZN subquery. They cover the cover page, the table of contents, and exhibit lists (note officer certificates, credit agreements, and stock plans). None mentions AWS competitors, cloud competition, AWS segment operating expenses, technology and infrastructure costs, depreciation of servers or data centers, or data center operating costs. Nothing here can support the claim, so the evidence is irrelevant. The rewrite targets Item 1 Business and Item 7 MD&A for the AMZN filing with accession 000101872424000008, which covers fiscal year ended December 31, 2023.

Full evidence and citations: [audit02.md](evaluation-20261031T061549Z/audit02.md). Structured trace: [audit02.json](evaluation-20261031T061549Z/audit02.json).

## audit03

Audit NVDA filing year 2024: separately retrieve export control risks and dependence on external manufacturing capacity. Explain with filing excerpts only.

No below-threshold evidence gaps were reported.

Full evidence and citations: [audit03.md](evaluation-20261031T061549Z/audit03.md). Structured trace: [audit03.json](evaluation-20261031T061549Z/audit03.json).

## audit04

Audit AMZN filing year 2024: separately retrieve AWS sales disclosures and operating expense disclosures. Support both topics with direct excerpts.

- Subquery 1: operating expenses disclosure by category
- Final critic rationale: None of the five passages contain operating expense figures or categories. They are the exhibit index (Items 15/exhibits 4.x–10.x), the 10-K cover page, and the Item 15 financial statement index. The passages only list statement titles, such as "Consolidated Statements of Operations," without any line items (cost of sales, fulfillment, technology and infrastructure, sales and marketing, G&A) or amounts. They also contain no AWS sales disclosure. The evidence is therefore irrelevant to the subquery. The filing year (2024), accession (000101872424000008), and fiscal period end (December 31, 2023) are correctly identified, but the excerpts do not support any operating-expense answer.

Full evidence and citations: [audit04.md](evaluation-20261031T061549Z/audit04.md). Structured trace: [audit04.json](evaluation-20261031T061549Z/audit04.json).

## audit05

Audit AAPL filing year 2023: separately retrieve share repurchase authorization disclosures and cash dividend disclosures. Provide original supporting excerpts for both.

No below-threshold evidence gaps were reported.

Full evidence and citations: [audit05.md](evaluation-20261031T061549Z/audit05.md). Structured trace: [audit05.json](evaluation-20261031T061549Z/audit05.json).

## audit06

Audit MSFT filing year 2024: separately retrieve cloud services growth disclosures and cybersecurity risks. Ground both topics in the filing.

No below-threshold evidence gaps were reported.

Full evidence and citations: [audit06.md](evaluation-20261031T061549Z/audit06.md). Structured trace: [audit06.json](evaluation-20261031T061549Z/audit06.json).

## audit07

Audit AMZN filing year 2024: separately retrieve liquidity and cash-flow disclosures and operating lease obligations. Present supporting excerpts without deriving new numbers.

No below-threshold evidence gaps were reported.

Full evidence and citations: [audit07.md](evaluation-20261031T061549Z/audit07.md). Structured trace: [audit07.json](evaluation-20261031T061549Z/audit07.json).

## audit08

Audit NVDA filing year 2024: separately retrieve Data Center revenue growth disclosures and gross margin disclosures. Use evidence for each topic without calculating a new ratio.

No below-threshold evidence gaps were reported.

Full evidence and citations: [audit08.md](evaluation-20261031T061549Z/audit08.md). Structured trace: [audit08.json](evaluation-20261031T061549Z/audit08.json).

## audit09

Audit AAPL filing year 2025 only: separately retrieve revenue disclosures and share repurchase disclosures. Do not substitute filing year 2023 or any other year if 2025 evidence is unavailable.

- Subquery 0: Apple net sales revenue disclosures by product and segment
- Final critic rationale: No verified evidence was retrieved (evidence is empty). The catalog contains no AAPL filing for filing year 2025; its only AAPL entry is filing year 2023 (fiscal period ended September 30, 2023), which the audit prompt prohibits substituting. Revenue and share repurchase disclosures are also unsupported, so no excerpt can be scored as useful.

- Subquery 1: Apple share repurchase program disclosures and shares repurchased
- Final critic rationale: No verified evidence was provided (evidence array is empty). The filing catalog has no AAPL entry with filing_year 2025. Its only AAPL entry is filing_year 2023 (accession 000032019323000106, fiscal period ended September 30, 2023), which the audit prompt excludes. Nothing can be verified, so the score is 1. The rewrite keeps the filing-year-2025 scope and does not substitute 2023 or any other year.

Full evidence and citations: [audit09.md](evaluation-20261031T061549Z/audit09.md). Structured trace: [audit09.json](evaluation-20261031T061549Z/audit09.json).

## audit10

Audit MSFT filing year 2024, with the exact SectionItem filter "Item 1A": separately retrieve cybersecurity risk and cloud competition risk. Do not relax the exact section filter if unclassified passages are all that is available.

- Subquery 0: Microsoft cybersecurity risk factors: threats to systems, data breaches, and security incidents
- Final critic rationale: No evidence was retrieved (evidence list is empty), so the score is 1. The MSFT 2024 filing (accession 000095017024087843, fiscal period ended June 30, 2024) is catalogued only as "Full filing (item not classified)", so the exact "Item 1A" SectionItem filter may return nothing. Per the audit instructions, the filter is kept exact and not relaxed. The rewrite keeps the filing-year 2024 scope and the fiscal-period distinction. Cloud competition risk needs a separate query and is not covered here.

- Subquery 1: Microsoft cloud competition risk factors: competitive pressure in cloud computing and AI services
- Final critic rationale: No verified evidence was provided (evidence is empty), so the score is 1. The catalog lists MSFT filing_year 2024 (accession 000095017024087843) with fiscal_period_end June 30, 2024, so the rewrite keeps the filing-year filter and the June 30, 2024 period end. The Item 1A filter is kept exact, and the query is limited to cloud competition risk. The cybersecurity risk retrieval should be run as a separate query under the same filters.

Full evidence and citations: [audit10.md](evaluation-20261031T061549Z/audit10.md). Structured trace: [audit10.json](evaluation-20261031T061549Z/audit10.json).

## Measured totals

- Executed and independently rechecked: 10/10.
- Published exact-quote citations: 54.
- Retrieval calls: 32; total retries: 12.
- Positive cases with all subqueries linked to published evidence: 6/8.
- Deliberate unavailable-evidence cases: 2/2.
- Median full-audit time: 23.338 seconds.

Validation: 16 Python tests, Go normal/race/purego tests, and go vet passed. The exact model IDs, token usage, timings, per-hop relevance rationales and source identities remain in the raw traces. No claim of comprehensive financial reasoning correctness is made.
