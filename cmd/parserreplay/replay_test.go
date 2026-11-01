package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/ingest"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// SYNTHETIC filing (generated words) shaped like the real titled Morgan Stanley layout:
// MD&A, then a risk part titled "Quantitative and Qualitative Disclosures about Risk" with an
// interior "Cybersecurity" subsection, then Financial Statements.
func words(prefix string, n int) string {
	var b strings.Builder
	for i := range n {
		fmt.Fprintf(&b, "%s%d ", prefix, i)
	}
	return b.String()
}

func syntheticMS(riskWords int) string {
	return "<html><body><p>Business</p><p>" + words("biz", 200) + "</p><p>Risk Factors</p><p>" + words("rf", 400) + "</p>" +
		"<p>Management’s Discussion and Analysis of Financial Condition and Results of Operations</p><p>" + words("mda", 700) + "</p>" +
		"<p>Quantitative and Qualitative Disclosures about Risk</p><p>" + words("risk", riskWords) + "</p>" +
		"<p>Cybersecurity</p><p>" + words("cyber", 600) + "</p>" +
		"<p>Financial Statements and Supplementary Data</p><p>" + words("fs", 900) + "</p>" +
		"<p>Changes in and Disagreements with Accountants on Accounting and Financial Disclosure</p><p>" + words("tail", 200) + "</p></body></html>"
}

type fixtureRuns struct {
	legacy, current *variantRun
	root            string
}

func parseBoth(t *testing.T, riskWords int) fixtureRuns {
	t.Helper()
	root := t.TempDir()
	raw := filepath.Join(root, "ms.htm")
	if err := os.WriteFile(raw, []byte(syntheticMS(riskWords)), 0o644); err != nil {
		t.Fatal(err)
	}
	filing := ingest.Filing{Ticker: msTicker, CIK: msCIK, Year: 2026, Accession: msAccession, Form: model.Form10K}
	filed := time.Date(2026, 2, 19, 0, 0, 0, 0, time.UTC)
	legacy, err := parseVariant(context.Background(), "parser_v2_legacy", true, raw, filing, filed, filepath.Join(root, "out", "legacy"))
	if err != nil {
		t.Fatal(err)
	}
	current, err := parseVariant(context.Background(), "parser_v3", false, raw, filing, filed, filepath.Join(root, "out", "current"))
	if err != nil {
		t.Fatal(err)
	}
	return fixtureRuns{legacy, current, root}
}

func TestChunkMetadataHoldsNoPassageText(t *testing.T) {
	typ := reflect.TypeOf(chunkMeta{})
	for i := range typ.NumField() {
		if strings.EqualFold(typ.Field(i).Name, "text") {
			t.Fatalf("chunkMeta has a %s field: passage text must stay on disk", typ.Field(i).Name)
		}
	}
	r := parseBoth(t, 3000)
	for _, c := range r.current.Chunks {
		if len(c.ID)+len(c.Section)+len(c.TextSHA256) > 200 {
			t.Fatalf("retained metadata too large for %s", c.ID)
		}
	}
}

// The diff reads back a bounded number of passages no matter how large the section is.
func TestDiffReadsBoundedPassagesRegardlessOfSectionSize(t *testing.T) {
	for _, riskWords := range []int{2000, 60000} {
		r := parseBoth(t, riskWords)
		reads := 0
		count := func(run *variantRun) {
			inner := run.text
			run.text = func(m chunkMeta) (string, error) {
				reads++
				if m.Length > maxPassageLine {
					t.Fatalf("unbounded read of %d bytes", m.Length)
				}
				return inner(m)
			}
		}
		count(r.legacy)
		count(r.current)
		d, err := diffSection("Item 7", r.legacy, r.current)
		if err != nil {
			t.Fatal(err)
		}
		if !d.CurrentWordsAreLegacyPrefix || d.RemovedWords != r.legacy.Report.Sections["Item 7"].Words-r.current.Report.Sections["Item 7"].Words || d.RemovedWords < riskWords {
			t.Fatalf("risk=%d: %+v", riskWords, d)
		}
		if !strings.HasPrefix(d.RemovedFirstWords, "Quantitative and Qualitative Disclosures about Risk risk0 risk1") {
			t.Fatalf("anchor = %q", d.RemovedFirstWords)
		}
		if !strings.HasSuffix(d.RemovedLast, fmt.Sprintf("risk%d", riskWords-1)) || !strings.HasSuffix(d.CurrentTailBeforeBoundary, "mda699") {
			t.Fatalf("anchors: %q / %q", d.RemovedLast, d.CurrentTailBeforeBoundary)
		}
		if got := len(r.legacy.BySection["Item 7"]); reads > 8 || (riskWords > 30000 && got < 100) {
			t.Fatalf("risk=%d: %d passage reads for a %d-passage section", riskWords, reads, got)
		}
	}
}

func TestUnchangedSectionsNeedNoTextReadsAndMatchByHash(t *testing.T) {
	r := parseBoth(t, 3000)
	for _, run := range []*variantRun{r.legacy, r.current} {
		run.text = func(chunkMeta) (string, error) { t.Fatal("text read for an unchanged section"); return "", nil }
	}
	for _, section := range []string{"Item 1A", "Item 8"} {
		d, err := diffSection(section, r.legacy, r.current)
		if err != nil || !d.Unchanged || d.IdenticalChunkPrefix != d.LegacyChunks || len(d.ChangedIDs) != 0 {
			t.Fatalf("%s: %+v %v", section, d, err)
		}
		if r.legacy.SectionSHA[section] != r.current.SectionSHA[section] {
			t.Fatalf("%s section hashes differ", section)
		}
	}
	if r.legacy.SectionSHA["Item 7"] == r.current.SectionSHA["Item 7"] {
		t.Fatal("changed Item 7 has identical hashes")
	}
}

func TestReadPassageDetectsWrongOffsetsAndOversizeLengths(t *testing.T) {
	r := parseBoth(t, 3000)
	m := r.current.Chunks[3]
	if text, err := readPassage(r.current.PassagesPath, m); err != nil || text == "" {
		t.Fatal(err)
	}
	wrong := m
	wrong.Offset = r.current.Chunks[4].Offset
	if _, err := readPassage(r.current.PassagesPath, wrong); err == nil {
		t.Fatal("passage at the wrong offset accepted")
	}
	huge := m
	huge.Length = maxPassageLine + 1
	if _, err := readPassage(r.current.PassagesPath, huge); err == nil {
		t.Fatal("oversize read accepted")
	}
}

func TestVariantArtifactsAreFreshAndRecorded(t *testing.T) {
	r := parseBoth(t, 3000)
	raw := filepath.Join(r.root, "ms.htm")
	filing := ingest.Filing{Ticker: msTicker, CIK: msCIK, Year: 2026, Accession: msAccession, Form: model.Form10K}
	if _, err := parseVariant(context.Background(), "again", false, raw, filing, time.Date(2026, 2, 19, 0, 0, 0, 0, time.UTC), filepath.Join(r.root, "out", "current")); err == nil {
		t.Fatal("an existing output directory was reused")
	}
	for _, rel := range []string{"passages.jsonl", "parse-report.json", "segments/CORPUS_SHA256", "segments/shards.json"} {
		if r.current.FileSHA256[rel] == "" {
			t.Fatalf("artifact %s not hashed", rel)
		}
	}
	if sum, _, err := fileSHA(r.current.PassagesPath); err != nil || sum != r.current.PassagesSHA {
		t.Fatal("recorded passages hash differs from the file")
	}
	if _, err := os.Stat(filepath.Join(r.root, "out", "current", ".work")); !os.IsNotExist(err) {
		t.Fatal("parser work directory left behind")
	}
	var report ingest.ParseReport
	data, err := os.ReadFile(filepath.Join(r.root, "out", "current", "parse-report.json"))
	if err != nil || json.Unmarshal(data, &report) != nil || report.Parser != ingest.ParserVersion || report.Sections["Item 7"].Words != r.current.Report.Sections["Item 7"].Words {
		t.Fatal("parse report not exported faithfully", err)
	}
	if r.legacy.Parser != ingest.ParserVersionV2 {
		t.Fatalf("legacy parser identity %q", r.legacy.Parser)
	}
}

func TestReopenSearchesAreSectionScopedAndCleanUp(t *testing.T) {
	r := parseBoth(t, 3000)
	probes := []probeSpec{
		{"mda", "mda12 mda13", model.FilingFilter{Ticker: msTicker, Year: 2026, Section: "Item 7"}},
		{"risk_text_in_item7", "risk40 risk41", model.FilingFilter{Ticker: msTicker, Year: 2026, Section: "Item 7"}},
		{"absent_7A", "risk40", model.FilingFilter{Ticker: msTicker, Year: 2026, Section: "Item 7A"}},
	}
	for _, run := range []*variantRun{r.legacy, r.current} {
		if err := reopen(context.Background(), run, probes); err != nil {
			t.Fatal(run.Name, err)
		}
		p := run.Reopen["probes"].([]searchProbe)
		if len(p[0].Hits) == 0 || !p[0].Allowed || len(p[2].Hits) != 0 {
			t.Fatalf("%s: %+v", run.Name, p)
		}
	}
	// v2 indexed the risk text under Item 7; v3 must not.
	legacyHits := r.legacy.Reopen["probes"].([]searchProbe)[1].Hits
	currentHits := r.current.Reopen["probes"].([]searchProbe)[1].Hits
	if len(legacyHits) == 0 {
		t.Fatal("legacy baseline does not retrieve the risk text under Item 7")
	}
	for _, id := range currentHits {
		if indexOfID(r.current, id) < 0 {
			t.Fatalf("v3 search returned %s, which is not one of its passages", id)
		}
	}
	// The first legacy hit is a passage that only exists because Item 7 ran into the risk part.
	if indexOfID(r.current, legacyHits[0]) >= 0 && len(currentHits) > 0 && currentHits[0] == legacyHits[0] {
		t.Fatal("v3 still ranks the removed risk-part passage first")
	}
}

func indexOfID(r *variantRun, id string) int {
	for i, c := range r.Chunks {
		if c.ID == id {
			return i
		}
	}
	return -1
}

func TestCorpusCompareIsStreamingAndCountsRemovedPassages(t *testing.T) {
	r := parseBoth(t, 3000)
	// A corpus file built from the legacy export plus an unrelated accession line.
	data, err := os.ReadFile(r.legacy.PassagesPath)
	if err != nil {
		t.Fatal(err)
	}
	corpus := filepath.Join(r.root, "corpus.jsonl")
	other := `{"ID":"000000000000000001#passage-000000","Ticker":"AAA","CIK":"1","FormType":"10-K","FilingYear":2024,"SectionItem":"Full filing (item not classified)","Text":"x"}` + "\n"
	if err := os.WriteFile(corpus, append([]byte(other), data...), 0o644); err != nil {
		t.Fatal(err)
	}
	out, err := corpusCompare(corpus, msAccession, r.legacy, r.current)
	if err != nil {
		t.Fatal(err)
	}
	legacy := out["parser_v2_legacy"].(map[string]int)
	current := out["parser_v3"].(map[string]int)
	removed := len(r.legacy.Chunks) - len(r.current.Chunks)
	if legacy["identical"] != len(r.legacy.Chunks) || legacy["only_in_corpus"] != 0 {
		t.Fatalf("legacy: %v", legacy)
	}
	if current["only_in_corpus"] != removed || current["identical"]+current["text_differs"] != len(r.current.Chunks) {
		t.Fatalf("current: %v removed=%d", current, removed)
	}
	if _, err := corpusCompare(filepath.Join(r.root, "missing.jsonl"), msAccession); err == nil {
		t.Fatal("missing corpus accepted")
	}
}

func TestReplayRefusesAnExistingSummary(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "ms.htm")
	if err := os.WriteFile(source, []byte(syntheticMS(2000)), 0o644); err != nil {
		t.Fatal(err)
	}
	results := filepath.Join(root, "results")
	if err := os.MkdirAll(results, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(results, "replay-summary.json"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := runReplay(context.Background(), replayConfig{source: source, corpus: filepath.Join(root, "none"), originals: root, indexDir: filepath.Join(root, "idx"), resultsDir: results})
	if err == nil || !strings.Contains(err.Error(), "fresh results directory") {
		t.Fatalf("%v", err)
	}
	if _, serr := os.Stat(filepath.Join(root, "idx")); !os.IsNotExist(serr) {
		t.Fatal("artifacts were written before the overwrite check")
	}
	if data, _ := os.ReadFile(filepath.Join(results, "replay-summary.json")); string(data) != "{}" {
		t.Fatal("existing summary modified")
	}
}
