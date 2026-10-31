package ingest

import (
	"errors"
	"fmt"
	"strings"
	"testing"
)

// SYNTHETIC fixtures for heading layouts observed in real bank 10-Ks
// (AXP/PNC/COF "(MD&A)" suffixes, BAC company-name heading line, COF
// self-linked headings, Morgan Stanley's untitled annual-report layout). The
// words are generated filler.

// variantDoc builds a body with one block per (heading HTML, item) pair,
// followed by n filler words carrying that item's marker.
func variantDoc(company string, n int, sections ...[2]string) string {
	var b strings.Builder
	b.WriteString(`<html><body><table>`)
	for _, s := range sections {
		fmt.Fprintf(&b, `<tr><td><a href="#toc%s">%s</a></td></tr>`, s[1], fixtureTitles[s[1]])
	}
	b.WriteString(`</table>`)
	for _, s := range sections {
		b.WriteString(s[0])
		b.WriteString(`<p>` + fixtureWords(company, s[1], n) + `</p>`)
	}
	b.WriteString(`</body></html>`)
	return b.String()
}

func sectionsOf(t *testing.T, doc string) (map[string]int, ParseReport) {
	t.Helper()
	chunks, report, err := parseFixture(t, doc, FormatHTML, ParserOptions{})
	if err != nil {
		t.Fatal(err)
	}
	words := map[string]int{}
	for _, c := range chunks {
		words[c.SectionItem] += len(strings.Fields(c.Text))
	}
	return words, report
}

func TestParserHeadingVariants(t *testing.T) {
	plain := func(item string) [2]string {
		return [2]string{fmt.Sprintf(`<p>Item %s. %s</p>`, item, fixtureTitles[item]), item}
	}
	doc := variantDoc("v", 200,
		plain("1"),
		[2]string{`<p>ITEM 1A – RISK FACTORS</p>`, "1A"},
		// Company-name heading line; the title stands alone on the next line.
		[2]string{`<div>Item 7. Acme Bancorp and Subsidiaries</div><div>Management&#8217;s Discussion and Analysis of Financial Condition and Results of Operations</div>`, "7"},
		// Self-linked heading: the anchor precedes a link back to itself.
		[2]string{`<div id="fs"></div><div><a href="#fs">Item 8. Financial Statements and Supplementary Data</a></div>`, "8"},
		[2]string{`<div id="i9"></div><div><a href="#i9">Item 9. Changes in and Disagreements with Accountants on Accounting and Financial Disclosure</a></div>`, "9"},
	)
	words, report := sectionsOf(t, doc)
	if report.HeadingMode != "item" || words["Item 1A"] < 200 || words["Item 7"] < 200 || words["Item 8"] < 200 {
		t.Fatalf("mode=%s words=%v", report.HeadingMode, words)
	}
	// The self-linked Item 9 heading ends Item 8: no Item 9 text leaks in.
	chunks, _, _ := parseFixture(t, doc, FormatHTML, ParserOptions{})
	for _, c := range chunks {
		if strings.Contains(c.Text, fixtureMarker("v", "9")) || strings.Contains(c.Text, "Changes in and Disagreements") {
			t.Fatalf("Item 9 text tagged %s", c.SectionItem)
		}
	}

	// "(MD&A)" / ("MD&A") abbreviation suffixes.
	for _, suffix := range []string{" (MD&amp;A)", " (&#8220;MD&amp;A&#8221;)"} {
		doc := variantDoc("p", 200, plain("1A"),
			[2]string{`<p>ITEM 7. MANAGEMENT’S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS OF OPERATIONS` + suffix + `</p>`, "7"},
			plain("8"), plain("9"))
		if words, _ := sectionsOf(t, doc); words["Item 7"] < 200 {
			t.Fatalf("suffix %q: %v", suffix, words)
		}
	}
}

// A forward link is still navigation: a cross-reference that links ahead to
// the Item 8 anchor must not start a section, even with the back-link rule.
func TestParserForwardLinkStillIgnored(t *testing.T) {
	doc := variantDoc("f", 200,
		[2]string{`<p>Item 1A. Risk Factors</p>`, "1A"},
		[2]string{`<p>Item 7. ` + fixtureTitles["7"] + `</p><p><a href="#later">Item 8. Financial Statements and Supplementary Data</a></p>`, "7"},
		[2]string{`<p id="later">Item 8. Financial Statements and Supplementary Data</p>`, "8"},
		[2]string{`<p>Item 9. ` + fixtureTitles["9"] + `</p>`, "9"},
	)
	words, _ := sectionsOf(t, doc)
	if words["Item 7"] < 200 || words["Item 8"] < 200 || words["Item 8"] > 220 {
		t.Fatal(words)
	}
}

func TestParserTitledFallback(t *testing.T) {
	titled := func(item string) [2]string {
		return [2]string{`<p>` + strings.ReplaceAll(fixtureTitles[item], "&#8217;", "’") + `</p>`, item}
	}
	// Annual-report order without Item labels, with repeated running headers.
	doc := variantDoc("ms", 300, titled("1"), titled("1A"), titled("7"), titled("8"), titled("9"))
	doc = strings.Replace(doc, `</p><p>`+fixtureWords("ms", "7", 300), `</p><p>`+fixtureWords("ms", "7", 300)+`</p><p>Management’s Discussion and Analysis</p><p>more discussion text`, 1)
	words, report := sectionsOf(t, doc)
	if report.HeadingMode != "titled" || words["Item 1A"] < 300 || words["Item 7"] < 300 || words["Item 8"] < 300 {
		t.Fatalf("mode=%s words=%v", report.HeadingMode, words)
	}
	chunks, _, _ := parseFixture(t, doc, FormatHTML, ParserOptions{})
	for _, c := range chunks {
		for _, foreign := range []string{"1", "9"} {
			if strings.Contains(c.Text, fixtureMarker("ms", foreign)) {
				t.Fatalf("Item %s text tagged %s", foreign, c.SectionItem)
			}
		}
	}

	// Titled Item 8 may not run to the end of the document.
	open := variantDoc("x", 300, titled("1A"), titled("7"), titled("8"))
	if _, _, err := parseFixture(t, open, FormatHTML, ParserOptions{}); !errors.Is(err, ErrSections) || !strings.Contains(err.Error(), "end of the document") {
		t.Fatal("unbounded titled Item 8 accepted", err)
	}

	// The fallback never overrides a verified Item-heading parse.
	_, report = sectionsOf(t, fixture10K(fixtureSpec{Company: "aapl"}))
	if report.HeadingMode != "item" {
		t.Fatal(report.HeadingMode)
	}
}
