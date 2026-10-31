package ingest

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

var fixtureFiling = Filing{Ticker: "AAPL", CIK: "320193", Year: 2024, Accession: "000032019324000123", Form: model.Form10K}

func parseFixture(t *testing.T, doc string, format Format, opts ParserOptions) ([]model.SECChunk, ParseReport, error) {
	t.Helper()
	if opts.SpoolDir == "" {
		opts.SpoolDir = t.TempDir()
	}
	p, err := NewParser(opts)
	if err != nil {
		t.Fatal(err)
	}
	var chunks []model.SECChunk
	report, err := p.Parse(context.Background(), strings.NewReader(doc), format, fixtureFiling, func(c model.SECChunk) error {
		chunks = append(chunks, c)
		return nil
	})
	if entries, _ := os.ReadDir(opts.SpoolDir); len(entries) != 0 {
		t.Fatal("parser spool not removed", entries)
	}
	return chunks, report, err
}

func TestParserTagsOnlyMajorSections(t *testing.T) {
	doc := fixture10K(fixtureSpec{Company: "aapl"})
	chunks, report, err := parseFixture(t, doc, FormatHTML, ParserOptions{WordsPerChunk: 64})
	if err != nil {
		t.Fatal(err)
	}
	if len(chunks) == 0 || report.Chunks != len(chunks) || report.RawBytes != int64(len(doc)) {
		t.Fatal(report)
	}
	want := map[string]int{"Item 1A": 0, "Item 7": 0, "Item 8": 0}
	order := []string{}
	for _, c := range chunks {
		if _, ok := want[c.SectionItem]; !ok {
			t.Fatalf("unexpected section tag %q", c.SectionItem)
		}
		if len(order) == 0 || order[len(order)-1] != c.SectionItem {
			order = append(order, c.SectionItem)
		}
		want[c.SectionItem]++
		item := strings.TrimPrefix(c.SectionItem, "Item ")
		if !strings.HasPrefix(c.ID, fixtureFiling.Accession+"#item"+item+"#passage-") || c.Ticker != "AAPL" || c.FilingYear != 2024 {
			t.Fatalf("bad identity %+v", c)
		}
		if err := c.Validate(); err != nil {
			t.Fatal(err)
		}
		for _, foreign := range []string{"1", "1b", "2", "7a", "9"} {
			if strings.Contains(c.Text, fixtureMarker("aapl", foreign)) {
				t.Fatalf("%s chunk contains Item %s text", c.SectionItem, foreign)
			}
		}
		for _, hidden := range []string{"hiddenxbrlword", "hiddenstyleword", "stylewordhidden", "commentword", "scriptword", "signatureword"} {
			if strings.Contains(c.Text, hidden) {
				t.Fatalf("hidden/out-of-section text %q emitted", hidden)
			}
		}
		if !strings.Contains(c.Text, fixtureMarker("aapl", item)) && !strings.Contains(c.Text, "Item") {
			t.Fatalf("%s chunk lacks its own marker: %q", c.SectionItem, c.Text[:40])
		}
	}
	if strings.Join(order, ",") != "Item 1A,Item 7,Item 8" {
		t.Fatal("section order", order)
	}
	for item, n := range want {
		if n == 0 || report.Sections[item].Chunks != n {
			t.Fatalf("%s: %d chunks, report %+v", item, n, report.Sections[item])
		}
	}
	// Entities decoded, NBSP normalized, heading line kept as section text.
	joined := ""
	for _, c := range chunks {
		joined += c.Text + "\n"
	}
	for _, s := range []string{"ITEM 7. Management’s Discussion", "Item 1A & elsewhere <unaudited>."} {
		if !strings.Contains(joined, s) {
			t.Fatalf("missing %q", s)
		}
	}
	if report.CandidateHeadings != 8 {
		t.Fatalf("candidate headings %d, want 8 body headings (TOC ignored)", report.CandidateHeadings)
	}
}

func TestParserRejectsUnverifiableFilings(t *testing.T) {
	cases := map[string]string{
		"toc only":       fixture10K(fixtureSpec{Company: "x", Words: map[string]int{"1A": 5, "7": 5, "8": 5}}),
		"missing item 7": fixture10K(fixtureSpec{Company: "x", Items: []string{"1", "1A", "8", "9"}}),
		"out of order":   fixture10K(fixtureSpec{Company: "x", Items: []string{"1", "7", "1A", "8", "9"}}),
		"duplicate":      fixture10K(fixtureSpec{Company: "x", Items: []string{"1", "1A", "7", "8", "9", "1A"}}),
	}
	for name, doc := range cases {
		emitted := 0
		p, _ := NewParser(ParserOptions{SpoolDir: t.TempDir()})
		_, err := p.Parse(context.Background(), strings.NewReader(doc), FormatHTML, fixtureFiling, func(model.SECChunk) error { emitted++; return nil })
		if !errors.Is(err, ErrSections) || emitted != 0 {
			t.Errorf("%s: err=%v emitted=%d", name, err, emitted)
		}
	}
	tenQ := fixtureFiling
	tenQ.Form = model.Form10Q
	p, _ := NewParser(ParserOptions{})
	if _, err := p.Parse(context.Background(), strings.NewReader("x"), FormatHTML, tenQ, nil); !errors.Is(err, ErrUnsupportedForm) {
		t.Error("10-Q accepted", err)
	}
	if _, _, err := parseFixture(t, fixture10K(fixtureSpec{Company: "x"}), FormatHTML, ParserOptions{MaxInputBytes: 1024}); err == nil {
		t.Error("input byte limit ignored")
	}
	for name, doc := range map[string]string{"unterminated tag": "<div class='x", "huge run": strings.Repeat("a", 2<<20), "bad utf8": "<p>\xff\xfe</p>", "deep": strings.Repeat("<span>", 300)} {
		if _, _, err := parseFixture(t, doc, FormatHTML, ParserOptions{}); err == nil || errors.Is(err, ErrSections) {
			t.Errorf("%s: %v", name, err)
		}
	}
	if _, err := NewParser(ParserOptions{WordsPerChunk: 5000}); err == nil {
		t.Error("chunk limit ignored")
	}
}

func TestParserItem8ByReferenceAndPlainText(t *testing.T) {
	chunks, report, err := parseFixture(t, fixture10K(fixtureSpec{Company: "x", Item8ByReference: true}), FormatHTML, ParserOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if report.Sections["Item 8"].Chunks != 1 || !strings.Contains(chunks[len(chunks)-1].Text, "incorporated by reference") {
		t.Fatal(report.Sections)
	}
	var text strings.Builder
	for _, item := range []string{"1", "1A", "7", "8", "9"} {
		text.WriteString("Item " + item + ". " + strings.ReplaceAll(fixtureTitles[item], "&#8217;", "'") + "\n")
		text.WriteString(fixtureWords("t", item, 200) + "\n\n")
	}
	chunks, _, err = parseFixture(t, text.String(), FormatText, ParserOptions{})
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range chunks {
		if strings.Contains(c.Text, fixtureMarker("t", "9")) || strings.Contains(c.Text, fixtureMarker("t", "1")) {
			t.Fatal("plain-text section boundary leaked", c.SectionItem)
		}
	}
}

// Cross-implementation check against the existing Python parser on the same
// synthetic fixture. Skipped when python3 is unavailable.
func TestParserMatchesPythonReference(t *testing.T) {
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 not found")
	}
	pyRoot, err := filepath.Abs("../../py")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(pyRoot, "ingest", "sections.py")); err != nil {
		t.Skip("py/ingest/sections.py not present")
	}
	dir := t.TempDir()
	doc := fixture10K(fixtureSpec{Company: "aapl"})
	raw := filepath.Join(dir, "filing.htm")
	if err := os.WriteFile(raw, []byte(doc), 0o644); err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(dir, "py.jsonl")
	script := `import sys; from pathlib import Path; from ingest.sections import Filing, SectionParser
SectionParser(256).parse(Path(sys.argv[1]), Path(sys.argv[2]), Filing('AAPL','320193',2024,'000032019324000123'), 'html', Path(sys.argv[3]))`
	cmd := exec.Command(python, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[4]); "+script, raw, out, dir, pyRoot)
	cmd.Dir = dir
	if msg, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("python reference failed: %v\n%s", err, msg)
	}
	pyOut, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	chunks, _, err := parseFixture(t, doc, FormatHTML, ParserOptions{})
	if err != nil {
		t.Fatal(err)
	}
	var goOut bytes.Buffer
	for _, c := range chunks {
		line, _ := json.Marshal(map[string]any{"ID": c.ID, "Ticker": c.Ticker, "CIK": c.CIK, "FormType": c.FormType, "FilingYear": c.FilingYear, "SectionItem": c.SectionItem, "Text": c.Text})
		goOut.Write(line)
		goOut.WriteByte('\n')
	}
	var pyLines []map[string]any
	for _, l := range bytes.Split(bytes.TrimSpace(pyOut), []byte("\n")) {
		var m map[string]any
		if err := json.Unmarshal(l, &m); err != nil {
			t.Fatal(err)
		}
		pyLines = append(pyLines, m)
	}
	if len(pyLines) != len(chunks) {
		t.Fatalf("python emitted %d chunks, go %d", len(pyLines), len(chunks))
	}
	for i, c := range chunks {
		if pyLines[i]["ID"] != c.ID || pyLines[i]["SectionItem"] != c.SectionItem || pyLines[i]["Text"] != c.Text {
			t.Fatalf("chunk %d differs:\npy %v\ngo %+v", i, pyLines[i], c)
		}
	}
}
