package ingest

import (
	"context"
	"os"
	"strings"
	"testing"

	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// Opt-in replay on the real Morgan Stanley FY2025 10-K (the only titled-heading filing in the
// expanded corpus). Set FINFILINGS_MS_HTML to the downloaded document, for example:
//
//	FINFILINGS_MS_HTML=data/parser-replay-20261101/ms-20251231.sec-current-NOT-CORPUS-BYTES.htm \
//	  go test ./pkg/ingest -run RealMorganStanley -v
//
// Obtain it with `parserreplay fetch` (SEC_USER_AGENT from the environment only). The
// expected numbers are the parser-v2 counts recorded in indexes/sec-scale-v2-20261031/report.json.
func TestRealMorganStanleyBoundaries(t *testing.T) {
	path := os.Getenv("FINFILINGS_MS_HTML")
	if path == "" {
		t.Skip("set FINFILINGS_MS_HTML to the real Morgan Stanley ms-20251231.htm")
	}
	parse := func(legacy bool) (map[string]string, ParseReport) {
		p, err := NewParser(ParserOptions{SpoolDir: t.TempDir(), LegacyTitledHeadings: legacy})
		if err != nil {
			t.Fatal(err)
		}
		f, err := os.Open(path)
		if err != nil {
			t.Fatal(err)
		}
		defer f.Close()
		text := map[string]string{}
		rep, err := p.Parse(context.Background(), f, FormatHTML, Filing{Ticker: "MS", CIK: "895421", Year: 2026, Accession: "000089542126000086", Form: model.Form10K},
			func(c model.SECChunk) error { text[c.SectionItem] += c.Text + " "; return nil })
		if err != nil {
			t.Fatal(err)
		}
		return text, rep
	}
	legacy, lrep := parse(true)
	current, crep := parse(false)

	// Baseline: the recorded parser-v2 result (the documented truncation).
	want := map[string]int{"Item 1A": 10672, "Item 7": 32278, "Item 8": 49101}
	for item, w := range want {
		if lrep.Sections[item].Words != w {
			t.Fatalf("v2 replay %s = %d words, recorded %d (source differs from the ingested document?)", item, lrep.Sections[item].Words, w)
		}
	}
	if lrep.HeadingMode != "titled" || crep.HeadingMode != "titled" || lrep.VisibleWords != crep.VisibleWords {
		t.Fatalf("%+v %+v", lrep, crep)
	}

	// Corrected: Item 1A and Item 8 are byte-identical; Item 7 ends where the risk part begins.
	for _, item := range []string{"Item 1A", "Item 8"} {
		if legacy[item] != current[item] || lrep.Sections[item] != crep.Sections[item] {
			t.Fatalf("%s changed", item)
		}
	}
	if crep.Sections["Item 7"].Words != 20497 || !strings.HasPrefix(legacy["Item 7"], current["Item 7"]) {
		t.Fatalf("Item 7 = %d words", crep.Sections["Item 7"].Words)
	}
	removed := legacy["Item 7"][len(current["Item 7"]):]
	if !strings.HasPrefix(strings.TrimSpace(removed), "Quantitative and Qualitative Disclosures about Risk") {
		t.Fatalf("removed text does not begin at the risk part title: %.80q", removed)
	}
	if strings.Contains(current["Item 7"], "Quantitative and Qualitative Disclosures about Risk Risk Management Overview") {
		t.Fatal("risk part leaked into Item 7")
	}
	if crep.IgnoredTitles == 0 {
		t.Fatal("no inferred titles were ignored")
	}
}
