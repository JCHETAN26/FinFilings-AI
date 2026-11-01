package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/index"
	"github.com/JCHETAN26/FinFillings-AI/pkg/ingest"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// Bounds for this single-document replay harness.
const (
	maxReplayChunks = 4096 // far above any one filing (MS: 361)
	wordsPerChunk   = 256
	anchorWords     = 30
)

type replayConfig struct {
	source, corpus, indexDir, resultsDir, originals string
	expectSHA256                                    string
}

// chunkMeta is ALL the replay keeps in memory per passage: identity, section, word count,
// a hash of the text and where the JSON line lives in passages.jsonl. The text itself stays on
// disk and is read back one passage at a time (bounded by maxPassageLine).
type chunkMeta struct {
	ID, Section string
	Words       int
	TextSHA256  string
	Offset      int64
	Length      int
}

// maxPassageLine bounds one re-read passage line (a passage is at most 32 KiB of text).
const maxPassageLine = 1 << 20

type variantRun struct {
	Name         string
	Parser       string
	Report       ingest.ParseReport
	PassagesPath string
	PassagesSHA  string
	SegmentsPath string
	Chunks       []chunkMeta
	BySection    map[string][]int
	SectionSHA   map[string]string // sha256 of the section's chunk texts joined by one space, computed while streaming
	FileSHA256   map[string]string
	Reopen       map[string]any
	Counts       map[string]int
	// text reads one passage back from disk; tests substitute a counting reader.
	text func(chunkMeta) (string, error)
}

// readPassage reads exactly one passage line back from the JSONL file.
func readPassage(path string, m chunkMeta) (string, error) {
	if m.Length <= 0 || m.Length > maxPassageLine {
		return "", fmt.Errorf("passage %s has an invalid recorded length %d", m.ID, m.Length)
	}
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	buf := make([]byte, m.Length)
	_, rerr := f.ReadAt(buf, m.Offset)
	if cerr := f.Close(); cerr != nil || (rerr != nil && rerr != io.EOF) {
		return "", errors.Join(cerr, rerr)
	}
	var c model.SECChunk
	if err := json.Unmarshal(buf, &c); err != nil {
		return "", err
	}
	if c.ID != m.ID {
		return "", fmt.Errorf("passage at offset %d is %s, expected %s", m.Offset, c.ID, m.ID)
	}
	return c.Text, nil
}

type countWriter struct {
	w io.Writer
	n int64
}

func (c *countWriter) Write(p []byte) (int, error) {
	n, err := c.w.Write(p)
	c.n += int64(n)
	return n, err
}

func sha256Hex(b []byte) string { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }

func fileSHA(path string) (string, int64, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	return hex.EncodeToString(h.Sum(nil)), n, err
}

func fdCount() int {
	e, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		return -1
	}
	return len(e)
}

// parseVariant parses one raw document with the selected parser behaviour and exports
// immutable artifacts: passages.jsonl, quarter-shard segments, CORPUS_SHA256, parse report.
func parseVariant(ctx context.Context, name string, legacy bool, rawPath string, filing ingest.Filing, filed time.Time, dir string) (*variantRun, error) {
	if err := os.MkdirAll(filepath.Dir(dir), 0o755); err != nil {
		return nil, err
	}
	if err := os.Mkdir(dir, 0o755); err != nil {
		return nil, fmt.Errorf("fresh output required: %w", err)
	}
	work := filepath.Join(dir, ".work")
	if err := os.Mkdir(work, 0o700); err != nil {
		return nil, err
	}
	parser, err := ingest.NewParser(ingest.ParserOptions{SpoolDir: work, LegacyTitledHeadings: legacy})
	if err != nil {
		return nil, err
	}
	raw, err := os.Open(rawPath)
	if err != nil {
		return nil, err
	}
	defer raw.Close()
	segments := filepath.Join(dir, ingest.SegmentsDirName)
	writer, err := index.NewShardedWriter(segments, index.ShardedWriterOptions{})
	if err != nil {
		return nil, err
	}
	closed := false
	defer func() {
		if !closed {
			if aerr := writer.Abort(); aerr != nil {
				fmt.Fprintln(os.Stderr, "parserreplay: abort sharded writer:", aerr)
			}
		}
	}()
	passagesPath := filepath.Join(dir, ingest.PassagesName)
	pf, err := os.OpenFile(passagesPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, err
	}
	buf := bufio.NewWriterSize(pf, 64<<10)
	digest := sha256.New()

	run := &variantRun{Name: name, SegmentsPath: segments, PassagesPath: passagesPath, BySection: map[string][]int{}, Counts: map[string]int{}, SectionSHA: map[string]string{}}
	cw := &countWriter{w: io.MultiWriter(buf, digest)}
	enc := json.NewEncoder(cw)
	enc.SetEscapeHTML(false)
	hashers := map[string]hash.Hash{}
	key := index.ShardKeyForDate(filed)
	rep, err := parser.Parse(ctx, raw, ingest.FormatHTML, filing, func(c model.SECChunk) error {
		if len(run.Chunks) >= maxReplayChunks {
			return errors.New("replay chunk bound exceeded")
		}
		if err := writer.Add(ctx, key, c); err != nil {
			return err
		}
		start := cw.n
		if err := enc.Encode(c); err != nil {
			return err
		}
		h, ok := hashers[c.SectionItem]
		if !ok {
			h = sha256.New()
			hashers[c.SectionItem] = h
		}
		if _, err := io.WriteString(h, c.Text+" "); err != nil {
			return err
		}
		run.BySection[c.SectionItem] = append(run.BySection[c.SectionItem], len(run.Chunks))
		run.Chunks = append(run.Chunks, chunkMeta{ID: c.ID, Section: c.SectionItem, Words: len(strings.Fields(c.Text)), TextSHA256: sha256Hex([]byte(c.Text)), Offset: start, Length: int(cw.n - start)})
		run.Counts[c.SectionItem]++
		return nil
	})
	if err != nil {
		return nil, errors.Join(fmt.Errorf("%s parse: %w", name, err), buf.Flush(), pf.Close())
	}
	run.Report, run.Parser = rep, rep.Parser
	for section, h := range hashers {
		run.SectionSHA[section] = hex.EncodeToString(h.Sum(nil))
	}
	run.text = func(m chunkMeta) (string, error) { return readPassage(passagesPath, m) }
	if err := buf.Flush(); err != nil {
		return nil, errors.Join(err, pf.Close())
	}
	if err := pf.Sync(); err != nil {
		return nil, errors.Join(err, pf.Close())
	}
	if err := pf.Close(); err != nil {
		return nil, err
	}
	run.PassagesSHA = hex.EncodeToString(digest.Sum(nil))
	closed = true
	if err := writer.Close(); err != nil {
		return nil, err
	}
	fp := filepath.Join(segments, "CORPUS_SHA256")
	if err := os.WriteFile(fp, []byte(run.PassagesSHA+"\n"), 0o644); err != nil {
		return nil, err
	}
	reportJSON, err := json.MarshalIndent(rep, "", "  ")
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(filepath.Join(dir, "parse-report.json"), append(reportJSON, '\n'), 0o644); err != nil {
		return nil, err
	}
	if err := os.Remove(work); err != nil { // errors if the parser left spool files behind
		return nil, fmt.Errorf("parser work directory not empty after parse: %w", err)
	}
	// Immutable: hash every artifact, then drop write permission (best effort on this filesystem).
	run.FileSHA256 = map[string]string{}
	err = filepath.Walk(dir, func(p string, info os.FileInfo, werr error) error {
		if werr != nil || info.IsDir() {
			return werr
		}
		sum, _, err := fileSHA(p)
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(dir, p)
		if err != nil {
			return err
		}
		run.FileSHA256[rel] = sum
		return os.Chmod(p, 0o444)
	})
	return run, err
}

type searchProbe struct {
	Name    string   `json:"name"`
	Query   string   `json:"query"`
	Filter  string   `json:"filter"`
	Hits    []string `json:"hit_ids"`
	Allowed bool     `json:"all_hits_match_filter"`
}

// reopen opens the exported segments through the existing mmap shard router, runs
// section-filtered searches, closes it and checks descriptors/goroutines return to baseline.
func reopen(ctx context.Context, run *variantRun, probes []probeSpec) error {
	baseFD, baseGo := fdCount(), runtime.NumGoroutine()
	router, err := index.OpenShardRouter(run.SegmentsPath, index.ShardRouterOptions{MaxConcurrentSearches: 2, Workers: 1})
	if err != nil {
		return err
	}
	stats, err := router.Stats()
	if err != nil {
		return errors.Join(err, router.Close())
	}
	result := map[string]any{"docs": stats.Docs, "segments": stats.Segments, "shards": len(router.Shards())}
	var out []searchProbe
	var kept []model.ChunkMeta
	for _, spec := range probes {
		hits, err := router.SearchFiltered(ctx, spec.query, 10, spec.filter)
		if err != nil {
			return errors.Join(err, router.Close())
		}
		p := searchProbe{Name: spec.name, Query: spec.query, Filter: fmt.Sprintf("%+v", spec.filter), Allowed: true, Hits: []string{}}
		for _, h := range hits {
			p.Hits = append(p.Hits, h.Chunk.ID)
			kept = append(kept, h.Chunk)
			if !spec.filter.Matches(h.Chunk) {
				p.Allowed = false
			}
		}
		out = append(out, p)
	}
	if err := router.Close(); err != nil {
		return err
	}
	// Hit metadata is copied out of the mapping and must stay valid after Close.
	for _, m := range kept {
		if m.ID == "" || m.SectionItem == "" {
			return errors.New("hit metadata invalid after router close")
		}
	}
	if _, err := router.Search(ctx, "revenue", 1); err == nil {
		return errors.New("search after Close unexpectedly succeeded")
	}
	runtime.GC()
	time.Sleep(50 * time.Millisecond)
	result["probes"] = out
	result["cleanup"] = map[string]any{"fds_before": baseFD, "fds_after": fdCount(), "goroutines_before": baseGo, "goroutines_after": runtime.NumGoroutine(),
		"search_after_close_rejected": true, "hit_metadata_valid_after_close": true}
	if fdCount() > baseFD || runtime.NumGoroutine() > baseGo {
		return fmt.Errorf("resources leaked after router close: fds %d->%d goroutines %d->%d", baseFD, fdCount(), baseGo, runtime.NumGoroutine())
	}
	run.Reopen = result
	return nil
}

type probeSpec struct {
	name, query string
	filter      model.FilingFilter
}

type sectionDiff struct {
	LegacyWords                 int      `json:"v2_words"`
	CurrentWords                int      `json:"v3_words"`
	LegacyChunks                int      `json:"v2_chunks"`
	CurrentChunks               int      `json:"v3_chunks"`
	LegacyStartWord             int      `json:"v2_start_word"`
	CurrentStart                int      `json:"v3_start_word"`
	IdenticalChunkPrefix        int      `json:"identical_leading_chunks"`
	FirstDifferingChunk         string   `json:"first_changed_chunk_id"`
	ChangedIDs                  []string `json:"changed_chunk_ids"`
	OnlyLegacyIDs               []string `json:"ids_only_in_v2"`
	OnlyCurrentIDs              []string `json:"ids_only_in_v3"`
	CurrentWordsAreLegacyPrefix bool     `json:"v3_text_is_prefix_of_v2_text"`
	RemovedWords                int      `json:"words_removed_from_section"`
	RemovedFirstWords           string   `json:"removed_text_first_30_words"`
	RemovedLast                 string   `json:"removed_text_last_30_words"`
	RemovedStartsAtDocumentWord int      `json:"removed_text_starts_at_document_word"`
	CurrentTailBeforeBoundary   string   `json:"v3_section_last_30_words"`
	Unchanged                   bool     `json:"section_unchanged"`
}

func firstWords(s string, n int) []string {
	f := strings.Fields(s)
	return f[:min(n, len(f))]
}

func lastWords(s string, n int) []string {
	f := strings.Fields(s)
	return f[max(0, len(f)-n):]
}

// sectionTail returns the last n words of a section by reading at most the last few passages
// (a passage holds up to 256 words, so one read normally suffices).
func sectionTail(run *variantRun, section string, n int) (string, error) {
	idx := run.BySection[section]
	var words []string
	for k := len(idx) - 1; k >= 0 && len(words) < n; k-- {
		text, err := run.text(run.Chunks[idx[k]])
		if err != nil {
			return "", err
		}
		words = append(lastWords(text, n), words...)
	}
	return strings.Join(words[max(0, len(words)-n):], " "), nil
}

// diffSection compares one section of two runs from per-passage metadata, reading back only the
// few passages needed for the boundary anchors. It never materialises a section's text.
func diffSection(section string, a, b *variantRun) (sectionDiff, error) {
	d := sectionDiff{LegacyWords: a.Report.Sections[section].Words, CurrentWords: b.Report.Sections[section].Words,
		LegacyChunks: len(a.BySection[section]), CurrentChunks: len(b.BySection[section]),
		LegacyStartWord: a.Report.Sections[section].StartWord, CurrentStart: b.Report.Sections[section].StartWord}
	ai, bi := a.BySection[section], b.BySection[section]
	aByID := make(map[string]chunkMeta, len(ai))
	for _, i := range ai {
		aByID[a.Chunks[i].ID] = a.Chunks[i]
	}
	bIDs := make(map[string]bool, len(bi))
	prefixOpen := true
	for k, i := range bi {
		c := b.Chunks[i]
		bIDs[c.ID] = true
		old, ok := aByID[c.ID]
		switch {
		case !ok:
			d.OnlyCurrentIDs = append(d.OnlyCurrentIDs, c.ID)
		case old.TextSHA256 != c.TextSHA256:
			d.ChangedIDs = append(d.ChangedIDs, c.ID)
			if prefixOpen {
				d.FirstDifferingChunk, prefixOpen = c.ID, false
			}
		case prefixOpen:
			d.IdenticalChunkPrefix = k + 1
		}
	}
	for _, i := range ai {
		if !bIDs[a.Chunks[i].ID] {
			d.OnlyLegacyIDs = append(d.OnlyLegacyIDs, a.Chunks[i].ID)
		}
	}
	d.Unchanged = a.SectionSHA[section] == b.SectionSHA[section] && len(d.ChangedIDs)+len(d.OnlyLegacyIDs)+len(d.OnlyCurrentIDs) == 0 && d.LegacyStartWord == d.CurrentStart
	if d.Unchanged || len(bi) == 0 || len(bi) > len(ai) {
		return d, nil
	}
	// Is the current section's text a prefix of the legacy section's? Every passage but the last must
	// be identical, and the last must be identical or a leading part of the legacy passage at that index.
	last := len(bi) - 1
	for k := range last {
		if b.Chunks[bi[k]].TextSHA256 != a.Chunks[ai[k]].TextSHA256 || b.Chunks[bi[k]].ID != a.Chunks[ai[k]].ID {
			return d, nil
		}
	}
	curLast, err := b.text(b.Chunks[bi[last]])
	if err != nil {
		return d, err
	}
	oldAtLast, err := a.text(a.Chunks[ai[last]])
	if err != nil {
		return d, err
	}
	if !strings.HasPrefix(oldAtLast, curLast) {
		return d, nil
	}
	d.CurrentWordsAreLegacyPrefix = true
	d.RemovedWords = d.LegacyWords - d.CurrentWords
	if d.RemovedWords <= 0 {
		return d, nil
	}
	// First removed words: the remainder of the legacy passage at the boundary, then following passages.
	rest := strings.TrimSpace(oldAtLast[len(curLast):])
	words := strings.Fields(rest)
	for k := last + 1; len(words) < anchorWords && k < len(ai); k++ {
		text, err := a.text(a.Chunks[ai[k]])
		if err != nil {
			return d, err
		}
		words = append(words, strings.Fields(text)...)
	}
	d.RemovedFirstWords = strings.Join(words[:min(anchorWords, len(words))], " ")
	if d.RemovedLast, err = sectionTail(a, section, anchorWords); err != nil {
		return d, err
	}
	d.RemovedStartsAtDocumentWord = d.CurrentStart + d.CurrentWords
	d.CurrentTailBeforeBoundary, err = sectionTail(b, section, anchorWords)
	return d, err
}

// readBoundedLine reads one newline-terminated line of at most limit bytes.
func readBoundedLine(r *bufio.Reader, limit int) ([]byte, error) {
	var line []byte
	for {
		part, err := r.ReadSlice('\n')
		if len(line)+len(part) > limit {
			return nil, fmt.Errorf("line exceeds %d bytes", limit)
		}
		line = append(line, part...)
		if !errors.Is(err, bufio.ErrBufferFull) {
			return line, err
		}
	}
}

// corpusCompare checks each variant's passages against the active expanded corpus records for
// the same accession. It streams both files; memory is bounded by the accession's own records.
func corpusCompare(corpus, accession string, runs ...*variantRun) (map[string]any, error) {
	f, err := os.Open(corpus)
	if err != nil {
		return nil, err
	}
	type rec struct{ sect, sha string }
	want := map[string]rec{}
	r := bufio.NewReaderSize(f, 1<<20)
	for {
		line, rerr := readBoundedLine(r, 4<<20)
		if len(line) > 0 && bytes.Contains(line[:min(len(line), 64)], []byte(accession)) {
			var c model.SECChunk
			if err := json.Unmarshal(line, &c); err != nil {
				return nil, errors.Join(err, f.Close())
			}
			if strings.HasPrefix(c.ID, accession+"#") {
				if len(want) >= maxReplayChunks {
					return nil, errors.Join(errors.New("corpus holds more records for the accession than the replay bound"), f.Close())
				}
				want[c.ID] = rec{c.SectionItem, sha256Hex([]byte(c.Text))}
			}
		}
		if rerr == io.EOF {
			break
		}
		if rerr != nil {
			return nil, errors.Join(rerr, f.Close())
		}
	}
	if err := f.Close(); err != nil {
		return nil, err
	}
	out := map[string]any{"corpus_records_for_accession": len(want)}
	for _, run := range runs {
		same, diffText, diffSect, missing := 0, 0, 0, 0
		seen := make(map[string]bool, len(run.Chunks))
		for _, c := range run.Chunks {
			seen[c.ID] = true
			w, ok := want[c.ID]
			switch {
			case !ok:
				missing++ // emitted here, absent from the corpus
			case w.sect != c.Section:
				diffSect++
			case w.sha != c.TextSHA256:
				diffText++
			default:
				same++
			}
		}
		onlyCorpus := 0
		for id := range want {
			if !seen[id] {
				onlyCorpus++
			}
		}
		out[run.Name] = map[string]int{"identical": same, "text_differs": diffText, "section_differs": diffSect, "not_in_corpus": missing, "only_in_corpus": onlyCorpus}
	}
	return out, nil
}

type originalFiling struct {
	Ticker    string `json:"ticker"`
	CIK       string `json:"cik"`
	Year      int    `json:"filing_year"`
	Form      string `json:"form"`
	Accession string `json:"accession"`
	RawSHA    string `json:"raw_sha256"`
	RawBytes  int64  `json:"raw_bytes"`
}

// parseDigest parses one raw file and returns the sha256 of its canonical passage stream.
func parseDigest(ctx context.Context, path string, s originalFiling, legacy bool) (digest string, report ingest.ParseReport, err error) {
	spool, err := os.MkdirTemp("", "parserreplay-orig-*")
	if err != nil {
		return "", report, err
	}
	defer func() {
		left, rerr := os.ReadDir(spool)
		if rerr == nil && len(left) != 0 {
			rerr = fmt.Errorf("parser left %d spool file(s) behind", len(left))
		}
		err = errors.Join(err, rerr, os.RemoveAll(spool))
	}()
	p, err := ingest.NewParser(ingest.ParserOptions{SpoolDir: spool, LegacyTitledHeadings: legacy})
	if err != nil {
		return "", report, err
	}
	f, err := os.Open(path)
	if err != nil {
		return "", report, err
	}
	h := sha256.New()
	enc := json.NewEncoder(h)
	enc.SetEscapeHTML(false)
	report, err = p.Parse(ctx, f, ingest.FormatHTML, ingest.Filing{Ticker: s.Ticker, CIK: s.CIK, Year: s.Year, Accession: s.Accession, Form: model.FormType(s.Form)},
		func(c model.SECChunk) error { return enc.Encode(c) })
	if cerr := f.Close(); cerr != nil {
		err = errors.Join(err, cerr)
	}
	return hex.EncodeToString(h.Sum(nil)), report, err
}

// originalsRegression parses the four original filings with both behaviours and requires
// identical output, byte for byte, in item-heading mode.
func originalsRegression(ctx context.Context, dir string) ([]map[string]any, error) {
	var prov struct {
		Sources []originalFiling `json:"sources"`
	}
	data, err := os.ReadFile(filepath.Join(dir, "provenance.json"))
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(data, &prov); err != nil {
		return nil, err
	}
	var rows []map[string]any
	for _, s := range prov.Sources {
		path := filepath.Join(dir, fmt.Sprintf("%s-%s.html", s.Ticker, s.Accession))
		sum, n, err := fileSHA(path)
		if err != nil {
			rows = append(rows, map[string]any{"ticker": s.Ticker, "accession": s.Accession, "status": "raw source unavailable: " + err.Error()})
			continue
		}
		row := map[string]any{"ticker": s.Ticker, "accession": s.Accession, "raw_sha256": sum, "raw_bytes": n, "matches_recorded_provenance": sum == s.RawSHA && n == s.RawBytes, "status": "ok"}
		var digests [2]string
		var reports [2]ingest.ParseReport
		for i, legacy := range []bool{true, false} {
			digests[i], reports[i], err = parseDigest(ctx, path, s, legacy)
			if err != nil {
				row["status"] = fmt.Sprintf("parse error (legacy=%v): %v", legacy, err)
				break
			}
		}
		row["passages_sha256_v2_legacy"], row["passages_sha256_v3"] = digests[0], digests[1]
		row["identical_output"] = row["status"] == "ok" && digests[0] == digests[1]
		row["heading_mode"], row["sections_v3"] = reports[1].HeadingMode, reports[1].Sections
		row["ignored_titled_headings_v3"] = reports[1].IgnoredTitles
		rows = append(rows, row)
	}
	return rows, nil
}

func runReplay(ctx context.Context, cfg replayConfig) (map[string]any, error) {
	started := time.Now().UTC()
	srcSHA, srcBytes, err := fileSHA(cfg.source)
	if err != nil {
		return nil, err
	}
	summary := map[string]any{
		"tool": "cmd/parserreplay replay", "started_utc": started.Format(time.RFC3339),
		"source": map[string]any{"path": cfg.source, "bytes": srcBytes, "sha256": srcSHA, "recorded_bytes": msBytes, "recorded_sha256": msSHA256,
			"matches_recorded_filing": srcBytes == msBytes && srcSHA == msSHA256,
			"url":                     "https://www.sec.gov/Archives/edgar/data/895421/000089542126000086/ms-20251231.htm"},
	}
	if cfg.expectSHA256 != "" && cfg.expectSHA256 != srcSHA {
		return summary, fmt.Errorf("source sha256 %s != expected %s", srcSHA, cfg.expectSHA256)
	}
	if _, err := os.Lstat(filepath.Join(cfg.resultsDir, "replay-summary.json")); err == nil {
		return summary, fmt.Errorf("%s already has a replay-summary.json; use a fresh results directory", cfg.resultsDir)
	} else if !errors.Is(err, os.ErrNotExist) {
		return summary, err
	}
	filing := ingest.Filing{Ticker: msTicker, CIK: msCIK, Year: 2026, Accession: msAccession, Form: model.Form10K}
	filed := time.Date(2026, 2, 19, 0, 0, 0, 0, time.UTC)
	legacy, err := parseVariant(ctx, "parser_v2_legacy", true, cfg.source, filing, filed, filepath.Join(cfg.indexDir, "ms-v2-legacy"))
	if err != nil {
		return summary, err
	}
	current, err := parseVariant(ctx, "parser_v3", false, cfg.source, filing, filed, filepath.Join(cfg.indexDir, "ms-v3"))
	if err != nil {
		return summary, err
	}
	// Removed-region anchor text drives a retrieval probe: words that exist only in the tail v3 no longer assigns to Item 7.
	diffs := map[string]sectionDiff{}
	for _, s := range []string{"Item 1A", "Item 7", "Item 8"} {
		if diffs[s], err = diffSection(s, legacy, current); err != nil {
			return summary, fmt.Errorf("%s diff: %w", s, err)
		}
	}
	probe := strings.Join(firstWords(diffs["Item 7"].RemovedFirstWords, 12), " ")
	probes := []probeSpec{
		{"item7_mda", "management discussion analysis results of operations net revenues", model.FilingFilter{Ticker: "MS", Year: 2026, Section: "Item 7"}},
		{"item7_removed_tail_text", probe, model.FilingFilter{Ticker: "MS", Year: 2026, Section: "Item 7"}},
		{"item1A_risk_factors", "risk factors may adversely affect our business", model.FilingFilter{Ticker: "MS", Year: 2026, Section: "Item 1A"}},
		{"item8_financial_statements", "consolidated balance sheet notes to consolidated financial statements", model.FilingFilter{Ticker: "MS", Year: 2026, Section: "Item 8"}},
		{"item7A_exact_section_absent", "value-at-risk market risk", model.FilingFilter{Ticker: "MS", Year: 2026, Section: "Item 7A"}},
		{"accession_scoped_no_section", "cybersecurity risk management and strategy", model.FilingFilter{Accession: msAccession}},
	}
	for _, run := range []*variantRun{legacy, current} {
		if err := reopen(ctx, run, probes); err != nil {
			return summary, fmt.Errorf("%s reopen: %w", run.Name, err)
		}
	}
	cc, err := corpusCompare(cfg.corpus, msAccession, legacy, current)
	if err != nil {
		return summary, err
	}
	originals, err := originalsRegression(ctx, cfg.originals)
	if err != nil {
		return summary, err
	}
	dump := func(r *variantRun) map[string]any {
		return map[string]any{"parser": r.Parser, "parse_report": r.Report, "chunk_counts": r.Counts, "passages_path": r.PassagesPath, "passages_sha256": r.PassagesSHA,
			"section_text_sha256": r.SectionSHA, "artifact_file_sha256": r.FileSHA256, "reopen_and_search": r.Reopen}
	}
	summary["parser_v2_legacy"], summary["parser_v3"] = dump(legacy), dump(current)
	summary["section_diff_v2_to_v3"] = diffs
	gap := func(r *variantRun) int {
		return r.Report.Sections["Item 8"].StartWord - (r.Report.Sections["Item 7"].StartWord + r.Report.Sections["Item 7"].Words)
	}
	summary["unemitted_words_between_item7_end_and_item8_heading"] = map[string]int{"v2": gap(legacy), "v3": gap(current)}
	summary["comparison_to_active_corpus"] = cc
	summary["original_four_filings"] = originals
	summary["finished_utc"] = time.Now().UTC().Format(time.RFC3339)
	summary["notes"] = []string{
		"Both variants parse identical source bytes. v2 legacy reproduces the historical parser-v2 titled-mode heading selection.",
		"comparison_to_active_corpus compares only the passages the parser emits (Items 1A, 7, 8). Equal indexed passages do not establish that unindexed visible text is identical; the original raw HTML is unavailable, so a full raw or text difference cannot be reconstructed.",
		"Chunk IDs are section-relative and 256-word windows start at the section heading: when a section ends earlier only its last chunk changes; no chunk stability is claimed beyond the identical_chunk_prefix reported per section.",
		"No performance metrics are measured or claimed. The active expanded corpus and indexes are read only.",
	}
	if err := os.MkdirAll(cfg.resultsDir, 0o755); err != nil {
		return summary, err
	}
	data, err := json.MarshalIndent(summary, "", "  ")
	if err != nil {
		return summary, err
	}
	out := filepath.Join(cfg.resultsDir, "replay-summary.json")
	f, err := os.OpenFile(out, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if err != nil {
		return summary, err
	}
	_, err = f.Write(append(data, '\n'))
	return summary, errors.Join(err, f.Close())
}
