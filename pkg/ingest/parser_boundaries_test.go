package ingest

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// SYNTHETIC fixtures (generated filler words) for the titled-mode boundary rules
// added in parser v3. They model the structure found in the real Morgan Stanley
// FY2025 10-K: standalone item titles without "Item N" labels, a risk part titled
// "Quantitative and Qualitative Disclosures about Risk", an interior "Cybersecurity"
// subsection, and repeated page running heads. The real-source replay lives in
// replay_real_test.go and cmd/parserreplay.

const (
	titleMDA  = "Management’s Discussion and Analysis of Financial Condition and Results of Operations"
	titleRisk = "Quantitative and Qualitative Disclosures about Risk"
	titleFS   = "Financial Statements and Supplementary Data"
)

// block is a standalone title paragraph followed by n marker-bearing words.
func block(company, item, title string, n int) string {
	return `<p>` + title + `</p><p>` + fixtureWords(company, item, n) + `</p>`
}

func words(t *testing.T, doc string, opts ParserOptions) (map[string]string, ParseReport) {
	t.Helper()
	chunks, report, err := parseFixture(t, doc, FormatHTML, opts)
	if err != nil {
		t.Fatal(err)
	}
	text := map[string]string{}
	for _, c := range chunks {
		text[c.SectionItem] += " " + c.Text
	}
	return text, report
}

func count(s string) int { return len(strings.Fields(s)) }

// titledBase is the common prefix. The caller supplies the Item 7 body.
func titledBase(company, mda string) string {
	return `<html><body>` + block(company, "1", "Business", 200) + block(company, "1A", "Risk Factors", 300) +
		`<p>` + titleMDA + `</p>` + mda + block(company, "8", titleFS, 300) +
		block(company, "9", "Changes in and Disagreements with Accountants on Accounting and Financial Disclosure", 150) + `</body></html>`
}

// A "Cybersecurity" subheading inside titled MD&A must not end Item 7. The v2
// parser treats it as Item 1C, ends Item 7 there and drops the rest.
func TestTitledInteriorCybersecuritySubheadingDoesNotEndItem7(t *testing.T) {
	mda := `<p>` + fixtureWords("ms", "7", 300) + `</p><p>Cybersecurity</p><p>` + fixtureWords("ms", "7tail", 300) + `</p>`
	doc := titledBase("ms", mda)

	got, report := words(t, doc, ParserOptions{})
	if report.HeadingMode != "titled" || report.Parser != ParserVersion || report.IgnoredTitles != 1 {
		t.Fatalf("%+v", report)
	}
	if !strings.Contains(got["Item 7"], fixtureMarker("ms", "7tail")) || count(got["Item 7"]) < 600 {
		t.Fatalf("Item 7 stopped at the interior subheading: %d words", count(got["Item 7"]))
	}
	if strings.Contains(got["Item 7"], fixtureMarker("ms", "8")) || strings.Contains(got["Item 8"], fixtureMarker("ms", "7tail")) {
		t.Fatal("Item 7 and Item 8 text crossed")
	}

	legacy, lreport := words(t, doc, ParserOptions{LegacyTitledHeadings: true})
	if lreport.Parser != ParserVersionV2 || strings.Contains(legacy["Item 7"], fixtureMarker("ms", "7tail")) {
		t.Fatalf("legacy replay must reproduce the v2 truncation: %+v", lreport)
	}
	if legacy["Item 8"] != got["Item 8"] || legacy["Item 1A"] != got["Item 1A"] {
		t.Fatal("unrelated sections changed")
	}
}

// Morgan Stanley's shape: the risk part is its own section (Item 7A) and contains the
// Cybersecurity subsection. v3 must close Item 7 at the risk part's title, leave the
// risk text out of Item 7 (and out of every emitted section), and still reach Item 8.
func TestTitledRiskPartClosesItem7AndNothingLeaks(t *testing.T) {
	mda := `<p>` + fixtureWords("ms", "7", 400) + `</p>` +
		`<p>` + titleRisk + `</p><p>` + fixtureWords("ms", "7A", 300) + `</p>` +
		`<p>Risk Disclosures</p><p>Cybersecurity</p><p>` + fixtureWords("ms", "7Acyber", 300) + `</p>` +
		`<p>Risk Disclosures</p><p>` + fixtureWords("ms", "7Atail", 100) + `</p>`
	doc := titledBase("ms", mda)

	got, report := words(t, doc, ParserOptions{})
	for _, marker := range []string{"7A", "7Acyber", "7Atail"} {
		for item, text := range got {
			if strings.Contains(text, fixtureMarker("ms", marker)) {
				t.Fatalf("risk-part text (%s) emitted under %s", marker, item)
			}
		}
	}
	if !strings.Contains(got["Item 7"], fixtureMarker("ms", "7")) || count(got["Item 7"]) < 400 || count(got["Item 7"]) > 420 {
		t.Fatalf("Item 7 = %d words", count(got["Item 7"]))
	}
	if report.IgnoredTitles != 1 || !strings.Contains(got["Item 8"], fixtureMarker("ms", "8")) {
		t.Fatalf("%+v", report)
	}

	// Documented v2 behaviour: Item 7 ran through the risk part until "Cybersecurity".
	legacy, _ := words(t, doc, ParserOptions{LegacyTitledHeadings: true})
	if !strings.Contains(legacy["Item 7"], fixtureMarker("ms", "7A")) || strings.Contains(legacy["Item 7"], fixtureMarker("ms", "7Atail")) {
		t.Fatal("legacy replay does not reproduce v2")
	}
}

// Repeated running heads (page headers that repeat an item title) are neither new
// sections nor duplicate-heading errors, whether they repeat the same item or an
// earlier one.
func TestTitledRepeatedRunningHeadingsAreNotBoundaries(t *testing.T) {
	mda := ""
	for page := range 4 {
		mda += `<p>` + fixtureWords("ms", "7", 120) + `</p>`
		mda += `<p>Business</p><p>` + titleMDA + `</p><p>Risk Factors</p><p>Cybersecurity</p>`
		mda += fmt.Sprintf(`<p>page %d Table of Contents</p>`, page)
	}
	doc := titledBase("ms", mda)
	got, report := words(t, doc, ParserOptions{})
	if count(got["Item 7"]) < 480 || report.IgnoredTitles == 0 {
		t.Fatalf("Item 7 = %d words, %+v", count(got["Item 7"]), report)
	}
	// Item 1A is likewise unaffected by its own repeated head.
	doc = strings.Replace(doc, `<p>Risk Factors</p><p>`+fixtureWords("ms", "1A", 300), `<p>Risk Factors</p><p>`+fixtureWords("ms", "1A", 150)+`</p><p>Risk Factors</p><p>`+fixtureWords("ms", "1A", 150), 1)
	if got, _ := words(t, doc, ParserOptions{}); count(got["Item 1A"]) < 300 {
		t.Fatalf("Item 1A = %d", count(got["Item 1A"]))
	}
}

// A standalone title that moves FORWARD is still a real boundary. This is the shape of
// Morgan Stanley's Item 1C pointer (a short "Cybersecurity" stub) between Items 1A and 7.
func TestTitledForwardBoundariesStillEndSections(t *testing.T) {
	doc := `<html><body>` + block("ms", "1", "Business", 200) + block("ms", "1A", "Risk Factors", 300) +
		`<p>Cybersecurity</p><p>For a discussion of cybersecurity, see the risk disclosures. ` + fixtureWords("ms", "1C", 40) + `</p>` +
		`<p>` + titleMDA + `</p><p>` + fixtureWords("ms", "7", 300) + `</p>` + block("ms", "8", titleFS, 300) +
		block("ms", "9", "Changes in and Disagreements with Accountants on Accounting and Financial Disclosure", 150) + `</body></html>`
	got, report := words(t, doc, ParserOptions{})
	if report.IgnoredTitles != 0 {
		t.Fatalf("a forward boundary was ignored: %+v", report)
	}
	for item, text := range got {
		if item != "Item 1A" && item != "Item 7" && item != "Item 8" {
			t.Fatalf("unexpected section %s", item)
		}
		if strings.Contains(text, fixtureMarker("ms", "1C")) {
			t.Fatalf("Item 1C stub leaked into %s", item)
		}
	}
	if count(got["Item 1A"]) > 330 {
		t.Fatalf("Item 1A swallowed the stub: %d words", count(got["Item 1A"]))
	}
	// Item 8 ends at the forward Item 9 title.
	if strings.Contains(got["Item 8"], fixtureMarker("ms", "9")) {
		t.Fatal("Item 9 leaked into Item 8")
	}
}

// Explicit "Item N" headings are never filtered by the order rule, even when they go
// backwards: an explicit Item 1C heading inside a titled-mode document ends Item 7
// exactly as before, and its legacy and v3 results are identical.
func TestExplicitHeadingsKeepTheirMeaningInTitledMode(t *testing.T) {
	mda := `<p>` + fixtureWords("ms", "7", 300) + `</p><p>Item 1C. Cybersecurity</p><p>` + fixtureWords("ms", "1C", 200) + `</p>`
	doc := titledBase("ms", mda)
	got, report := words(t, doc, ParserOptions{})
	if report.HeadingMode != "titled" || report.IgnoredTitles != 0 {
		t.Fatalf("%+v", report)
	}
	if strings.Contains(got["Item 7"], fixtureMarker("ms", "1C")) || count(got["Item 7"]) > 320 {
		t.Fatalf("explicit backward heading ignored: %d words", count(got["Item 7"]))
	}
	legacy, _ := words(t, doc, ParserOptions{LegacyTitledHeadings: true})
	for _, item := range []string{"Item 1A", "Item 7", "Item 8"} {
		if legacy[item] != got[item] {
			t.Fatalf("%s differs between v2 and v3 for explicit headings", item)
		}
	}
}

// Ambiguity: the same word can be an interior subheading (inferred) or a real explicit
// heading. Only the inferred one is ignored.
func TestInferredAndExplicitCybersecurityAreDistinguished(t *testing.T) {
	mda := `<p>` + fixtureWords("ms", "7", 250) + `</p><p>Cybersecurity</p><p>` + fixtureWords("ms", "7mid", 250) + `</p>` +
		`<p>Item 1C. Cybersecurity</p><p>` + fixtureWords("ms", "1C", 200) + `</p>`
	got, report := words(t, titledBase("ms", mda), ParserOptions{})
	if report.IgnoredTitles != 1 {
		t.Fatalf("%+v", report)
	}
	if !strings.Contains(got["Item 7"], fixtureMarker("ms", "7mid")) || strings.Contains(got["Item 7"], fixtureMarker("ms", "1C")) {
		t.Fatal("inferred subheading ignored but explicit heading not honoured")
	}
}

// Item-heading parses (all accepted layouts) are byte-identical under v2 and v3, and the
// explicit-mode Item 7A title is unchanged: "about Risk" is recognised only as an
// inferred standalone title, so an explicit "Item 7A. ... about Risk" still does not end Item 7.
func TestItemModeIsUnchanged(t *testing.T) {
	for _, spec := range []fixtureSpec{{Company: "aapl"}, {Company: "jpm", Item8ByReference: true}} {
		doc := fixture10K(spec)
		a, ra, ea := parseFixture(t, doc, FormatHTML, ParserOptions{})
		b, rb, eb := parseFixture(t, doc, FormatHTML, ParserOptions{LegacyTitledHeadings: true})
		if fmt.Sprint(ea) != fmt.Sprint(eb) || fmt.Sprint(a) != fmt.Sprint(b) || ra.Sections["Item 7"] != rb.Sections["Item 7"] || ra.HeadingMode != "item" {
			t.Fatalf("item mode changed for %+v", spec)
		}
	}
	doc := variantDoc("e", 200,
		[2]string{`<p>Item 1A. Risk Factors</p>`, "1A"},
		[2]string{`<p>Item 7. ` + fixtureTitles["7"] + `</p>`, "7"},
		[2]string{`<p>Item 7A. ` + titleRisk + `</p>`, "7A"},
		[2]string{`<p>Item 8. Financial Statements and Supplementary Data</p>`, "8"},
		[2]string{`<p>Item 9. ` + fixtureTitles["9"] + `</p>`, "9"})
	got, _ := words(t, doc, ParserOptions{})
	if !strings.Contains(got["Item 7"], fixtureMarker("e", "7A")) {
		t.Fatal("explicit Item 7A 'about Risk' heading behaviour changed; that would alter accepted layouts")
	}
}

// A filing that the new rules cannot validate emits nothing, and the parser identity is
// recorded as v3 (or v2 only for the explicit legacy replay).
func TestTitledValidationFailureEmitsNothing(t *testing.T) {
	// Item 8 present only as an interior backward title after Item 7 -> no Item 8 section.
	doc := `<html><body>` + block("x", "1A", "Risk Factors", 300) + `<p>` + titleMDA + `</p><p>` + fixtureWords("x", "7", 300) + `</p><p>Business</p><p>` + fixtureWords("x", "1", 300) + `</p></body></html>`
	p, err := NewParser(ParserOptions{SpoolDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	emitted := 0
	_, err = p.Parse(context.Background(), strings.NewReader(doc), FormatHTML, fixtureFiling, func(model.SECChunk) error { emitted++; return nil })
	if !errors.Is(err, ErrSections) || emitted != 0 {
		t.Fatalf("emitted=%d err=%v", emitted, err)
	}
}

// Disabling the order filter or the 7A variant must fail these tests (rule coverage).
func TestRulesAreIndividuallyLoadBearing(t *testing.T) {
	mda := `<p>` + fixtureWords("ms", "7", 300) + `</p><p>Cybersecurity</p><p>` + fixtureWords("ms", "7tail", 300) + `</p>`
	doc := titledBase("ms", mda)
	p, _ := NewParser(ParserOptions{SpoolDir: t.TempDir()})
	if p.standalone["7A"].MatchString(compact(titleRisk)) != true {
		t.Fatal("7A about-Risk variant missing")
	}
	legacy, _ := NewParser(ParserOptions{SpoolDir: t.TempDir(), LegacyTitledHeadings: true})
	if legacy.standalone["7A"].MatchString(compact(titleRisk)) {
		t.Fatal("legacy parser must not know the variant")
	}
	_, report := words(t, doc, ParserOptions{})
	if report.IgnoredTitles == 0 {
		t.Fatal("order rule inactive")
	}
}
