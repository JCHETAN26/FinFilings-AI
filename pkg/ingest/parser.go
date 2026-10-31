package ingest

// Deterministic 10-K section parser. It started as a Go port of the heading
// selection in py/ingest/sections.py ("deterministic-canonical-heading-v1")
// and emits the same chunk IDs and SectionItem tags; v2 adds the heading
// variants and the titled-heading fallback described below, which the Python
// parser does not implement.
//
// Pass 1 streams the raw filing into a disk spool of visible text lines
// ("<linked 0|1>\t<normalized text>\n"); HTML is reduced to block-level lines
// with hidden content (script, style, head, ix:header, ix:hidden, the hidden
// attribute, display:none, visibility:hidden) suppressed and lines inside
// <a href> marked as linked, except a fragment link whose target id or name
// appeared earlier in the document (a heading that links to its own anchor,
// as opposed to a forward table-of-contents or cross-reference link). Pass 2
// slides a 4-line window over the spool and records canonical "Item N.
// <Title>" headings; linked lines (tables of contents) never start a heading,
// titles split across up to four short blocks are joined, a short trailing
// parenthetical such as "(MD&A)" is ignored, and the title may also stand
// alone on one of the next three lines ("Item 7. <Company name>" followed by
// the title). Every 10-K item 1..16 is recognized so that each major
// section ends at the next heading of any item. A section is accepted only
// with >= MinSectionWords words (Item 8 may instead be a short
// "incorporated by reference" pointer). Items 1A, 7 and 8 must all be found
// exactly once and in that order, or the filing is rejected; there is no
// fallback that emits untagged or guessed-section text. Text outside those
// three sections is never emitted.
//
// Titled-heading fallback: only if Item headings cannot verify all three
// sections, the selection is retried with standalone exact-title lines (an
// unlinked line of at most 160 runes that is exactly a canonical item title,
// such as "Risk Factors") also counted as headings. This covers filers that
// lay out the 10-K in annual-report order without "Item N" labels. It applies
// the same checks plus one more: Item 8 must end at a later heading, not at
// the end of the document. ParseReport.HeadingMode records which mode was used.
//
// Not handled (the filing is rejected): items that only cross-reference page
// ranges of an annual report, sections that live in a separate Exhibit 13
// document, and headings that also appear in glossary/definition tables.
//
// Scope: 10-K and 10-K/A primary documents only. 10-Q filings use a different
// Part I/Part II item layout and return ErrUnsupportedForm.

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"html"
	"io"
	"log/slog"
	"os"
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// ParserVersion names the heading-selection algorithm recorded in reports.
const ParserVersion = "deterministic-canonical-heading-v2-go"

// Hard parser limits independent of options.
const (
	maxLineBytes       = 1 << 20 // one visible block / plain-text line / HTML tag
	maxHTMLDepth       = 256
	maxHeadingCands    = 512
	maxHeadingRunes    = 350
	maxTitleRunes      = 160     // standalone title line in the titled fallback
	maxAnchorIDs       = 1 << 16 // ids/names remembered for back-link detection
	maxAnchorIDBytes   = 256
	maxChunkTextBytes  = 32 << 10
	headingWindowLines = 4
)

// ErrUnsupportedForm reports a form the section parser does not handle.
var ErrUnsupportedForm = errors.New("ingest: section parser supports 10-K and 10-K/A only")

// ErrSections reports a filing whose major sections could not be verified.
// Nothing has been emitted when Parse returns it.
var ErrSections = errors.New("ingest: major sections missing or ambiguous")

// Format is the raw document encoding.
type Format string

const (
	FormatHTML Format = "html"
	FormatText Format = "text"
)

// Filing is the identity attached to every emitted chunk.
type Filing struct {
	Ticker    string
	CIK       string
	Year      int    // calendar year of the filing date
	Accession string // 18 digits
	Form      model.FormType
}

func (f Filing) validate() error {
	if f.Form != model.Form10K && f.Form != model.Form10KA {
		return fmt.Errorf("%w: %q", ErrUnsupportedForm, f.Form)
	}
	if !tickerPattern.MatchString(f.Ticker) || !cikPattern.MatchString(f.CIK) || f.Year < model.MinFilingYear || f.Year > model.MaxFilingYear ||
		len(f.Accession) != 18 || strings.Trim(f.Accession, "0123456789") != "" {
		return errors.New("ingest: invalid filing identity (ticker, CIK, year 1993..2100, 18-digit accession)")
	}
	return nil
}

// ParserOptions configures a Parser. Zero values select the defaults.
type ParserOptions struct {
	WordsPerChunk   int    // 32..1024, default 256
	MinSectionWords int    // 32..4096, default 128
	MaxInputBytes   int64  // 1..512 MiB, default 128 MiB
	SpoolDir        string // directory for the temporary visible-text spool; default os.TempDir()
	Logger          *slog.Logger
}

// Parser is stateless apart from its options and compiled patterns; it is
// safe for concurrent use. Each Parse holds at most the 4-line heading window,
// one 1 MiB block, the heading candidate list (<= 512) and one chunk of words
// in memory; everything else stays in the disk spool.
type Parser struct {
	opts   ParserOptions
	prefix *regexp.Regexp
	titles map[string]*regexp.Regexp
	order  []string // titles keys in 10-K order, for deterministic matching
	byRef  *regexp.Regexp
	paren  *regexp.Regexp
}

// NewParser validates opts and compiles the heading patterns.
func NewParser(opts ParserOptions) (*Parser, error) {
	if opts.WordsPerChunk == 0 {
		opts.WordsPerChunk = 256
	}
	if opts.MinSectionWords == 0 {
		opts.MinSectionWords = 128
	}
	if opts.MaxInputBytes == 0 {
		opts.MaxInputBytes = 128 << 20
	}
	if opts.WordsPerChunk < 32 || opts.WordsPerChunk > 1024 || opts.MinSectionWords < 32 || opts.MinSectionWords > 4096 || opts.MaxInputBytes < 1 || opts.MaxInputBytes > 512<<20 {
		return nil, errors.New("ingest: invalid parser chunk/section/input limits")
	}
	if opts.Logger == nil {
		opts.Logger = slog.New(slog.NewJSONHandler(io.Discard, nil))
	}
	titles := map[string]string{
		"1": `business`, "1A": `riskfactors`, "1B": `unresolvedstaffcomments`, "1C": `cybersecurity`,
		"2": `properties`, "3": `legalproceedings`, "4": `minesafetydisclosures`,
		"5":  `marketfor.*(?:equity|securities|stock)(?:.*)`,
		"6":  `(?:selectedfinancialdata|reserved)`,
		"7":  `managementsdiscussionandanalysis(?:offinancialconditionandresultsofoperations|ofresultsofoperationsandfinancialcondition)?`,
		"7A": `quantitativeandqualitativedisclosuresaboutmarketrisk`,
		"8":  `financialstatements(?:andsupplementarydata)?`,
		"9":  `changesinanddisagreementswithaccountantsonaccountingandfinancialdisclosure`,
		"9A": `controlsandprocedures`, "9B": `otherinformation`,
		"9C": `disclosureregardingforeignjurisdictionsthatpreventinspections`,
		"10": `directorsexecutiveofficersandcorporategovernance`,
		"11": `executivecompensation`,
		"12": `securityownershipofcertainbeneficialownersandmanagementandrelatedstockholdermatters`,
		"13": `certainrelationshipsandrelatedtransactionsanddirectorindependence`,
		"14": `principalaccountantfeesandservices`,
		"15": `exhibits(?:andfinancialstatementschedules|financialstatementschedules)?`,
		"16": `form10ksummary`,
	}
	p := &Parser{
		opts:   opts,
		prefix: regexp.MustCompile(`(?i)^item\s*(\d{1,2}\s?[abc]?)(?:[.\s:–—-]+|$)(.*)$`),
		titles: make(map[string]*regexp.Regexp, len(titles)),
		byRef:  regexp.MustCompile(`(?i)incorporat\w*\s+by\s+reference|information required by this item is set forth`),
		paren:  regexp.MustCompile(`\s*\([^()]{1,24}\)\s*$`),
		order:  []string{"1", "1A", "1B", "1C", "2", "3", "4", "5", "6", "7", "7A", "8", "9", "9A", "9B", "9C", "10", "11", "12", "13", "14", "15", "16"},
	}
	for item, pattern := range titles {
		p.titles[item] = regexp.MustCompile(`^(?:` + pattern + `)$`)
	}
	return p, nil
}

// SectionReport describes one emitted section.
type SectionReport struct {
	Words  int `json:"words"`
	Chunks int `json:"chunks"`
}

// ParseReport describes one parsed filing.
type ParseReport struct {
	RawBytes          int64                    `json:"raw_bytes"`
	RawSHA256         string                   `json:"raw_sha256"`
	VisibleWords      int                      `json:"visible_words"`
	CandidateHeadings int                      `json:"candidate_headings"`
	Chunks            int                      `json:"chunks"`
	Sections          map[string]SectionReport `json:"sections"`
	Parser            string                   `json:"parser"`
	HeadingMode       string                   `json:"heading_mode"` // "item" or "titled"
}

type spoolLine struct {
	start, end  int64
	wordsBefore int
	text        string
	linked      bool
}

type heading struct {
	item        string
	start       int64
	wordsBefore int
}

type sectionSpan struct {
	start, end int64
	words      int
}

// Parse reads one raw filing document and calls emit for every Item 1A, 7
// and 8 passage, in that order, each tagged with SectionItem "Item 1A",
// "Item 7" or "Item 8". emit is called only after all three sections have
// been located; an error returned after the first emit (an emit failure,
// an oversized chunk, an I/O error) means the filing was emitted partially
// and the caller must discard the whole build.
func (p *Parser) Parse(ctx context.Context, raw io.Reader, format Format, filing Filing, emit func(model.SECChunk) error) (report ParseReport, err error) {
	if err := filing.validate(); err != nil {
		return report, err
	}
	spool, err := os.CreateTemp(p.opts.SpoolDir, "finfilings-visible-*.lines")
	if err != nil {
		return report, fmt.Errorf("ingest: create parser spool: %w", err)
	}
	defer func() { err = errors.Join(err, spool.Close(), os.Remove(spool.Name())) }()

	hash := sha256.New()
	counted := &countingReader{r: io.LimitReader(raw, p.opts.MaxInputBytes+1)}
	src := bufio.NewReaderSize(io.TeeReader(counted, hash), 64<<10)
	out := bufio.NewWriterSize(spool, 64<<10)
	var words int
	switch format {
	case FormatHTML:
		words, err = spoolHTML(ctx, src, out)
	case FormatText:
		words, err = spoolText(ctx, src, out)
	default:
		err = fmt.Errorf("ingest: unknown format %q", format)
	}
	if err == nil && counted.n > p.opts.MaxInputBytes {
		err = fmt.Errorf("ingest: raw filing exceeds %d bytes", p.opts.MaxInputBytes)
	}
	if err != nil {
		return report, err
	}
	if err = out.Flush(); err != nil {
		return report, err
	}
	size, err := spool.Seek(0, io.SeekEnd)
	if err != nil {
		return report, err
	}
	report = ParseReport{RawBytes: counted.n, RawSHA256: hex.EncodeToString(hash.Sum(nil)), VisibleWords: words, Parser: ParserVersion, Sections: map[string]SectionReport{}}

	candidates, err := p.headings(ctx, spool, false)
	if err != nil {
		return report, err
	}
	report.CandidateHeadings, report.HeadingMode = len(candidates), "item"
	sections, err := p.selectSections(spool, size, words, candidates, false)
	if errors.Is(err, ErrSections) {
		titled, serr := p.headings(ctx, spool, true)
		if serr == nil {
			var ts map[string]sectionSpan
			if ts, serr = p.selectSections(spool, size, words, titled, true); serr == nil {
				sections, err = ts, nil
				report.CandidateHeadings, report.HeadingMode = len(titled), "titled"
			}
		}
		if serr != nil && !errors.Is(serr, ErrSections) {
			return report, serr
		}
		if err != nil {
			err = fmt.Errorf("%w; titled-heading fallback: %v", err, serr)
		}
	}
	if err != nil {
		return report, err
	}

	pending := make([]string, 0, p.opts.WordsPerChunk)
	for _, item := range []string{"1A", "7", "8"} {
		span := sections[item]
		number := 0
		flush := func() error {
			if len(pending) == 0 {
				return nil
			}
			text := strings.Join(pending, " ")
			pending = pending[:0]
			if len(text) > maxChunkTextBytes {
				return fmt.Errorf("ingest: Item %s chunk exceeds %d bytes", item, maxChunkTextBytes)
			}
			c := model.SECChunk{
				ID:     fmt.Sprintf("%s#item%s#passage-%06d", filing.Accession, item, number),
				Ticker: filing.Ticker, CIK: filing.CIK, FormType: filing.Form, FilingYear: filing.Year,
				SectionItem: "Item " + item, Text: text,
			}
			if err := c.Validate(); err != nil {
				return err
			}
			if err := emit(c); err != nil {
				return err
			}
			number++
			report.Chunks++
			return nil
		}
		r := bufio.NewReaderSize(io.NewSectionReader(spool, span.start, span.end-span.start), 64<<10)
		for {
			if err := ctx.Err(); err != nil {
				return report, err
			}
			line, rerr := readBoundedLine(r, maxLineBytes+3)
			if rerr == io.EOF && len(line) == 0 {
				break
			}
			if rerr != nil {
				return report, fmt.Errorf("ingest: invalid spool section range: %w", rerr)
			}
			tab := bytes.IndexByte(line, '\t')
			if tab < 0 {
				return report, errors.New("ingest: corrupt parser spool")
			}
			for _, w := range strings.Fields(string(line[tab+1:])) {
				pending = append(pending, w)
				if len(pending) == p.opts.WordsPerChunk {
					if err := flush(); err != nil {
						return report, err
					}
				}
			}
		}
		if err := flush(); err != nil {
			return report, err
		}
		report.Sections["Item "+item] = SectionReport{Words: span.words, Chunks: number}
		p.opts.Logger.Info("section_parsed", "accession", filing.Accession, "item", item, "words", span.words, "chunks", number)
	}
	return report, nil
}

// selectSections applies the section rules to one candidate list. boundedEnd
// additionally requires Item 8 to end at a later heading (titled fallback).
func (p *Parser) selectSections(spool *os.File, size int64, words int, candidates []heading, boundedEnd bool) (map[string]sectionSpan, error) {
	sections := map[string]sectionSpan{}
	for i, h := range candidates {
		end, endWords := size, words
		if i+1 < len(candidates) {
			end, endWords = candidates[i+1].start, candidates[i+1].wordsBefore
		}
		count := endWords - h.wordsBefore
		if h.item != "1A" && h.item != "7" && h.item != "8" {
			continue
		}
		shortReference := false
		if h.item == "8" && count >= 16 && count < p.opts.MinSectionWords {
			excerpt := make([]byte, min(4096, end-h.start))
			if _, err := spool.ReadAt(excerpt, h.start); err != nil {
				return nil, err
			}
			shortReference = p.byRef.Match(excerpt)
		}
		if count >= p.opts.MinSectionWords || shortReference {
			if _, dup := sections[h.item]; dup {
				return nil, fmt.Errorf("%w: duplicate body heading Item %s", ErrSections, h.item)
			}
			if boundedEnd && h.item == "8" && i+1 == len(candidates) {
				return nil, fmt.Errorf("%w: titled Item 8 runs to the end of the document", ErrSections)
			}
			sections[h.item] = sectionSpan{h.start, end, count}
		}
	}
	for _, item := range []string{"1A", "7", "8"} {
		if _, ok := sections[item]; !ok {
			return nil, fmt.Errorf("%w: Item %s not found with >= %d words (%d candidate headings)", ErrSections, item, p.opts.MinSectionWords, len(candidates))
		}
	}
	if !(sections["1A"].start < sections["7"].start && sections["7"].start < sections["8"].start) {
		return nil, fmt.Errorf("%w: Items 1A, 7, 8 are out of 10-K order", ErrSections)
	}
	return sections, nil
}

// headings scans the spool with a 4-line window and returns heading
// candidates with consecutive repeats of the same item collapsed. titled also
// accepts standalone exact-title lines (see the titled-heading fallback).
func (p *Parser) headings(ctx context.Context, spool *os.File, titled bool) ([]heading, error) {
	if _, err := spool.Seek(0, io.SeekStart); err != nil {
		return nil, err
	}
	r := bufio.NewReaderSize(spool, 64<<10)
	var offset int64
	words := 0
	next := func() (spoolLine, bool, error) {
		raw, err := readBoundedLine(r, maxLineBytes+3)
		if err == io.EOF && len(raw) == 0 {
			return spoolLine{}, false, nil
		}
		if err != nil {
			return spoolLine{}, false, fmt.Errorf("ingest: corrupt parser spool: %w", err)
		}
		if len(raw) < 3 || raw[1] != '\t' || (raw[0] != '0' && raw[0] != '1') {
			return spoolLine{}, false, errors.New("ingest: corrupt parser spool record")
		}
		text := string(raw[2 : len(raw)-1])
		l := spoolLine{start: offset, end: offset + int64(len(raw)), wordsBefore: words, text: text, linked: raw[0] == '1'}
		offset = l.end
		words += len(strings.Fields(text))
		return l, true, nil
	}
	var window []spoolLine
	fill := func() error {
		for len(window) < headingWindowLines {
			l, ok, err := next()
			if err != nil || !ok {
				return err
			}
			window = append(window, l)
		}
		return nil
	}
	var out []heading
	for {
		if err := fill(); err != nil {
			return nil, err
		}
		if len(window) == 0 {
			return out, nil
		}
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		item := p.heading(window)
		if item == "" && titled {
			item = p.standaloneTitle(window[0])
		}
		if item != "" && (len(out) == 0 || out[len(out)-1].item != item) {
			if len(out) == maxHeadingCands {
				return nil, fmt.Errorf("%w: more than %d heading candidates", ErrSections, maxHeadingCands)
			}
			out = append(out, heading{item: item, start: window[0].start, wordsBefore: window[0].wordsBefore})
		}
		window = window[1:]
	}
}

// heading returns the canonical item whose heading starts window[0], or "".
func (p *Parser) heading(window []spoolLine) string {
	first := window[0]
	if first.linked || utf8.RuneCountInString(first.text) > maxHeadingRunes {
		return ""
	}
	m := p.prefix.FindStringSubmatch(first.text)
	if m == nil {
		return ""
	}
	item := strings.ToUpper(strings.ReplaceAll(m[1], " ", ""))
	title, ok := p.titles[item]
	if !ok {
		return ""
	}
	tail := m[2]
	for i := range min(headingWindowLines, len(window)) {
		if i > 0 {
			following := window[i]
			if following.linked || p.prefix.MatchString(following.text) {
				break
			}
			// The title alone on a following line, e.g. "Item 7. <Company>"
			// then "Management's Discussion and Analysis ...".
			if utf8.RuneCountInString(following.text) <= maxTitleRunes && title.MatchString(p.titleKey(following.text)) {
				return item
			}
			tail += " " + following.text
		}
		if title.MatchString(p.titleKey(tail)) {
			return item
		}
		if i == 0 && strings.TrimSpace(tail) != "" && utf8.RuneCountInString(tail) > maxHeadingRunes {
			break
		}
	}
	return ""
}

// standaloneTitle returns the item whose canonical title is exactly l's
// text, for an unlinked short line, or "".
func (p *Parser) standaloneTitle(l spoolLine) string {
	if l.linked || utf8.RuneCountInString(l.text) > maxTitleRunes {
		return ""
	}
	key := p.titleKey(l.text)
	for _, item := range p.order {
		if p.titles[item].MatchString(key) {
			return item
		}
	}
	return ""
}

// titleKey compacts s after dropping one short trailing parenthetical, so
// "... Results of Operations (MD&A)" matches the Item 7 title.
func (p *Parser) titleKey(s string) string {
	return compact(p.paren.ReplaceAllString(s, ""))
}

// compact lower-cases s and keeps only ASCII letters and digits.
func compact(s string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(s) {
		if r >= 'a' && r <= 'z' || r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

type countingReader struct {
	r io.Reader
	n int64
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.n += int64(n)
	return n, err
}

func writeSpoolLine(out *bufio.Writer, linked bool, text string) (int, error) {
	fields := strings.Fields(text)
	if len(fields) == 0 {
		return 0, nil
	}
	flag := byte('0')
	if linked {
		flag = '1'
	}
	if err := out.WriteByte(flag); err != nil {
		return 0, err
	}
	if err := out.WriteByte('\t'); err != nil {
		return 0, err
	}
	for i, f := range fields {
		if i > 0 {
			if err := out.WriteByte(' '); err != nil {
				return 0, err
			}
		}
		if _, err := out.WriteString(f); err != nil {
			return 0, err
		}
	}
	return len(fields), out.WriteByte('\n')
}

// readBoundedLine assembles fragments without imposing bufio's internal
// buffer size as a document limit. Short lines borrow the reader buffer
// and remain valid until its next read.
func readBoundedLine(r *bufio.Reader, limit int) ([]byte, error) {
	var line []byte
	for {
		part, err := r.ReadSlice('\n')
		if len(line)+len(part) > limit {
			return nil, fmt.Errorf("ingest: line exceeds %d bytes", limit)
		}
		if len(line) == 0 && !errors.Is(err, bufio.ErrBufferFull) {
			return part, err
		}
		line = append(line, part...)
		if !errors.Is(err, bufio.ErrBufferFull) {
			return line, err
		}
	}
}

func spoolText(ctx context.Context, src *bufio.Reader, out *bufio.Writer) (int, error) {
	words := 0
	for {
		line, err := readBoundedLine(src, maxLineBytes)
		if len(line) > 0 {
			if !utf8.Valid(line) {
				return 0, errors.New("ingest: plain-text filing is not valid UTF-8")
			}
			n, werr := writeSpoolLine(out, false, string(line))
			if werr != nil {
				return 0, werr
			}
			words += n
		}
		if err == io.EOF {
			return words, nil
		}
		if err != nil {
			return 0, err
		}
		if cerr := ctx.Err(); cerr != nil {
			return 0, cerr
		}
	}
}

// visibleText reduces an HTML token stream to block-level visible lines. It
// mirrors VisibleLines in py/ingest/sections.py.
type visibleText struct {
	out    *bufio.Writer
	stack  []htmlElem
	text   strings.Builder
	linked bool
	words  int
	// ids holds element id/name values seen so far (at most maxAnchorIDs of
	// at most maxAnchorIDBytes each), so a fragment link back to an earlier
	// anchor is not treated as navigation. Past the cap, links stay linked.
	ids map[string]struct{}
}

type htmlElem struct {
	tag            string
	hidden, linked bool
}

var (
	blockTags = map[string]bool{"p": true, "div": true, "tr": true, "li": true, "h1": true, "h2": true, "h3": true, "h4": true, "h5": true, "h6": true, "table": true, "br": true, "hr": true}
	voidTags  = map[string]bool{"area": true, "base": true, "br": true, "col": true, "embed": true, "hr": true, "img": true, "input": true, "link": true, "meta": true, "param": true, "source": true, "track": true, "wbr": true}
	hiddenTag = map[string]bool{"script": true, "style": true, "head": true, "ix:header": true, "ix:hidden": true}
)

func (v *visibleText) flush() error {
	n, err := writeSpoolLine(v.out, v.linked, v.text.String())
	v.words += n
	v.text.Reset()
	v.linked = false
	return err
}

func (v *visibleText) data(s string) error {
	if len(v.stack) > 0 && v.stack[len(v.stack)-1].hidden {
		return nil
	}
	if v.text.Len()+len(s) > maxLineBytes {
		return fmt.Errorf("ingest: visible HTML block exceeds %d bytes", maxLineBytes)
	}
	v.text.WriteString(s)
	v.linked = v.linked || (len(v.stack) > 0 && v.stack[len(v.stack)-1].linked)
	return nil
}

func (v *visibleText) start(tag string, attrs map[string]string) error {
	if blockTags[tag] {
		if err := v.flush(); err != nil {
			return err
		}
	}
	var parent htmlElem
	if len(v.stack) > 0 {
		parent = v.stack[len(v.stack)-1]
	}
	style := strings.ReplaceAll(strings.ToLower(attrs["style"]), " ", "")
	_, hiddenAttr := attrs["hidden"]
	hidden := parent.hidden || hiddenTag[tag] || hiddenAttr || strings.Contains(style, "display:none") || strings.Contains(style, "visibility:hidden")
	for _, key := range []string{"id", "name"} {
		if id := attrs[key]; id != "" && len(id) <= maxAnchorIDBytes && len(v.ids) < maxAnchorIDs {
			if v.ids == nil {
				v.ids = map[string]struct{}{}
			}
			v.ids[id] = struct{}{}
		}
	}
	href := attrs["href"]
	_, backLink := v.ids[strings.TrimPrefix(href, "#")]
	backLink = backLink && strings.HasPrefix(href, "#")
	linked := parent.linked || (tag == "a" && href != "" && !backLink)
	if !voidTags[tag] {
		if len(v.stack) >= maxHTMLDepth {
			return fmt.Errorf("ingest: HTML nesting exceeds %d", maxHTMLDepth)
		}
		v.stack = append(v.stack, htmlElem{tag, hidden, linked})
	}
	return nil
}

func (v *visibleText) end(tag string) error {
	if blockTags[tag] {
		if err := v.flush(); err != nil {
			return err
		}
	}
	for i := len(v.stack) - 1; i >= 0; i-- {
		if v.stack[i].tag == tag {
			v.stack = v.stack[:i]
			break
		}
	}
	return nil
}

// spoolHTML is a bounded streaming scanner for the HTML subset found in EDGAR
// primary documents: start/end/self-closing tags with quoted or bare
// attributes, comments, declarations, processing instructions, CDATA, and
// raw-text script/style bodies. Character references are decoded with
// html.UnescapeString. It is not a full HTML5 tree builder (no implied end
// tags or foster parenting); unmatched end tags are ignored, as in Python's
// HTMLParser-based extractor.
func spoolHTML(ctx context.Context, src *bufio.Reader, out *bufio.Writer) (int, error) {
	v := &visibleText{out: out}
	var run []byte
	var token []byte
	emitRun := func() error {
		if len(run) == 0 {
			return nil
		}
		if !utf8.Valid(run) {
			return errors.New("ingest: HTML filing is not valid UTF-8")
		}
		err := v.data(html.UnescapeString(string(run)))
		run = run[:0]
		return err
	}
	// readUntil appends bytes through the first occurrence of delim to token.
	readUntil := func(delim string) error {
		for {
			b, err := src.ReadByte()
			if err != nil {
				return fmt.Errorf("ingest: unterminated HTML construct: %w", err)
			}
			token = append(token, b)
			if len(token) > maxLineBytes {
				return fmt.Errorf("ingest: HTML token exceeds %d bytes", maxLineBytes)
			}
			if bytes.HasSuffix(token, []byte(delim)) {
				return nil
			}
		}
	}
	for n := 0; ; n++ {
		if n&0xffff == 0 {
			if err := ctx.Err(); err != nil {
				return 0, err
			}
		}
		b, err := src.ReadByte()
		if err == io.EOF {
			if err := emitRun(); err != nil {
				return 0, err
			}
			if err := v.flush(); err != nil {
				return 0, err
			}
			return v.words, nil
		}
		if err != nil {
			return 0, err
		}
		if b != '<' {
			run = append(run, b)
			if len(run) > maxLineBytes {
				return 0, fmt.Errorf("ingest: HTML text run exceeds %d bytes", maxLineBytes)
			}
			continue
		}
		peek, _ := src.Peek(3)
		switch {
		case len(peek) >= 3 && string(peek) == "!--":
			if err := emitRun(); err != nil {
				return 0, err
			}
			token = token[:0]
			if err := readUntil("-->"); err != nil {
				return 0, err
			}
		case len(peek) >= 1 && (peek[0] == '!' || peek[0] == '?'):
			if err := emitRun(); err != nil {
				return 0, err
			}
			token = token[:0]
			delim := ">"
			if p, _ := src.Peek(8); string(p) == "![CDATA[" {
				delim = "]]>"
			}
			if err := readUntil(delim); err != nil {
				return 0, err
			}
		case len(peek) >= 2 && peek[0] == '/' && isASCIILetter(peek[1]):
			if err := emitRun(); err != nil {
				return 0, err
			}
			token = token[:0]
			if err := readUntil(">"); err != nil {
				return 0, err
			}
			name, _ := tagName(token[1 : len(token)-1])
			if err := v.end(name); err != nil {
				return 0, err
			}
		case len(peek) >= 1 && isASCIILetter(peek[0]):
			if err := emitRun(); err != nil {
				return 0, err
			}
			token = token[:0]
			if err := readTag(src, &token); err != nil {
				return 0, err
			}
			body := token[:len(token)-1]
			selfClosing := len(body) > 0 && body[len(body)-1] == '/'
			if selfClosing {
				body = body[:len(body)-1]
			}
			name, rest := tagName(body)
			if err := v.start(name, parseAttrs(rest)); err != nil {
				return 0, err
			}
			if (name == "script" || name == "style") && !selfClosing {
				// Raw text: skip to the matching end tag without tokenizing.
				// The end tag holds no '>' before its own, so only the bytes
				// since the previous '>' need checking.
				closing := []byte("</" + name)
				for {
					token = token[:0]
					if err := readUntil(">"); err != nil {
						return 0, err
					}
					if i := bytes.LastIndexByte(token, '<'); i >= 0 && bytes.HasPrefix(bytes.ToLower(token[i:]), closing) {
						break
					}
				}
				selfClosing = true
			}
			if selfClosing {
				if err := v.end(name); err != nil {
					return 0, err
				}
			}
		default:
			run = append(run, b) // a literal '<'
		}
	}
}

func isASCIILetter(b byte) bool { return b|0x20 >= 'a' && b|0x20 <= 'z' }

// readTag reads a start tag through its closing '>', honoring quoted
// attribute values.
func readTag(src *bufio.Reader, token *[]byte) error {
	var quote byte
	for {
		b, err := src.ReadByte()
		if err != nil {
			return fmt.Errorf("ingest: unterminated HTML tag: %w", err)
		}
		*token = append(*token, b)
		if len(*token) > maxLineBytes {
			return fmt.Errorf("ingest: HTML tag exceeds %d bytes", maxLineBytes)
		}
		switch {
		case quote != 0:
			if b == quote {
				quote = 0
			}
		case b == '"' || b == '\'':
			quote = b
		case b == '>':
			return nil
		}
	}
}

func tagName(body []byte) (string, []byte) {
	i := 0
	for i < len(body) && body[i] != ' ' && body[i] != '\t' && body[i] != '\n' && body[i] != '\r' && body[i] != '\f' && body[i] != '/' {
		i++
	}
	return strings.ToLower(string(body[:i])), body[i:]
}

// parseAttrs returns lower-cased attribute names mapped to decoded values
// ("" for valueless attributes).
func parseAttrs(s []byte) map[string]string {
	attrs := map[string]string{}
	i := 0
	space := func(b byte) bool { return b == ' ' || b == '\t' || b == '\n' || b == '\r' || b == '\f' || b == '/' }
	for i < len(s) {
		for i < len(s) && space(s[i]) {
			i++
		}
		start := i
		for i < len(s) && !space(s[i]) && s[i] != '=' {
			i++
		}
		if start == i {
			i++
			continue
		}
		name := strings.ToLower(string(s[start:i]))
		for i < len(s) && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r') {
			i++
		}
		value := ""
		if i < len(s) && s[i] == '=' {
			i++
			for i < len(s) && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r') {
				i++
			}
			if i < len(s) && (s[i] == '"' || s[i] == '\'') {
				q := s[i]
				i++
				vs := i
				for i < len(s) && s[i] != q {
					i++
				}
				value = string(s[vs:i])
				i++
			} else {
				vs := i
				for i < len(s) && !space(s[i]) {
					i++
				}
				value = string(s[vs:i])
			}
		}
		if _, seen := attrs[name]; !seen {
			attrs[name] = html.UnescapeString(value)
		}
	}
	return attrs
}
