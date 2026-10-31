package ingest

import (
	"fmt"
	"strings"
)

// SYNTHETIC TEST FIXTURE: a generated 10-K-shaped HTML document. Its layout
// imitates EDGAR primary documents (table of contents with links, inline XBRL
// hidden header, split headings, entities, cross-references, script) but
// every word is generated filler. Nothing measured on it is SEC retrieval
// quality, parser accuracy on real filings, or production performance.
type fixtureSpec struct {
	Company string // vocabulary prefix, e.g. "aapl"
	// Items lists body items in document order; defaults to a full 10-K.
	Items []string
	// Words per body item; default 300 (items 1B and 2 default 40).
	Words map[string]int
	// Item8ByReference replaces Item 8's body with a short pointer.
	Item8ByReference bool
}

var fixtureTitles = map[string]string{
	"1":  "Business",
	"1A": "Risk Factors",
	"1B": "Unresolved Staff Comments",
	"2":  "Properties",
	"7":  "Management&#8217;s Discussion and Analysis of Financial Condition and Results of Operations",
	"7A": "Quantitative and Qualitative Disclosures About Market Risk",
	"8":  "Financial Statements and Supplementary Data",
	"9":  "Changes in and Disagreements with Accountants on Accounting and Financial Disclosure",
}

// fixtureMarker is a word that occurs only in one item of one company.
func fixtureMarker(company, item string) string {
	return company + "item" + strings.ToLower(item) + "marker"
}

var fixtureFiller = strings.Fields(`revenue liquidity risk capital operations customers supply demand
	margin segment cash flow interest rate competition regulatory inventory growth fiscal quarter
	results financial condition products services markets costs expenses income tax`)

func fixtureWords(company, item string, n int) string {
	var b strings.Builder
	marker := fixtureMarker(company, item)
	for i := range n {
		if i > 0 {
			b.WriteByte(' ')
		}
		if i%50 == 7 {
			b.WriteString(marker)
			continue
		}
		b.WriteString(fixtureFiller[(i*7+len(item))%len(fixtureFiller)])
	}
	return b.String()
}

func fixture10K(spec fixtureSpec) string {
	items := spec.Items
	if items == nil {
		items = []string{"1", "1A", "1B", "2", "7", "7A", "8", "9"}
	}
	var b strings.Builder
	b.WriteString(`<!DOCTYPE html><html><head><title>Item 7. Management's Discussion and Analysis</title>`)
	b.WriteString(`<style>p { margin: 0 } /* Item 1A. Risk Factors stylewordhidden */</style></head><body>`)
	b.WriteString(`<ix:header><ix:hidden><div>Item 1A. Risk Factors hiddenxbrlword</div></ix:hidden></ix:header>`)
	b.WriteString(`<div style="display: none">Item 8. Financial Statements hiddenstyleword</div>`)
	b.WriteString(`<!-- Item 7. Management's Discussion commentword -->`)
	// Table of contents: every heading is linked, so none may start a section.
	b.WriteString(`<table>`)
	for _, item := range items {
		fmt.Fprintf(&b, `<tr><td><a href="#item%s">Item %s.</a></td><td><a href="#item%s">%s</a></td><td>3</td></tr>`, item, item, item, fixtureTitles[item])
	}
	b.WriteString(`</table><div>PART I</div>`)
	for _, item := range items {
		n := 300
		if item == "1B" || item == "2" {
			n = 40
		}
		if w, ok := spec.Words[item]; ok {
			n = w
		}
		switch item {
		case "1":
			// Split heading: number and title in separate blocks, with an
			// entity-encoded non-breaking space.
			fmt.Fprintf(&b, `<div id="item1"><span style="font-weight:bold">Item&#160;1.</span></div><div><b>Business</b></div>`)
		case "7":
			fmt.Fprintf(&b, `<p id="item7"><b>ITEM 7. %s</b></p>`, fixtureTitles[item])
		default:
			fmt.Fprintf(&b, `<p id="item%s">Item %s. %s</p>`, item, item, fixtureTitles[item])
		}
		if item == "8" && spec.Item8ByReference {
			b.WriteString(`<p>The information required by this item is incorporated by reference to the Annual Report to shareholders filed as Exhibit 13, pages 40 through 90 of this report.</p>`)
			continue
		}
		words := strings.Fields(fixtureWords(spec.Company, item, n))
		for start := 0; start < len(words); start += 60 {
			end := min(start+60, len(words))
			b.WriteString(`<p>` + strings.Join(words[start:end], " ") + `</p>`)
		}
		if item == "7" {
			// A linked cross-reference line must not start a new section.
			b.WriteString(`<p><a href="#item8">Item 8. Financial Statements and Supplementary Data</a></p>`)
			b.WriteString(`<p>See also the discussion in Item 1A &amp; elsewhere &lt;unaudited&gt;.</p>`)
		}
		if item == "8" {
			b.WriteString(`<script>var s = "<div>Item 9. scriptword</div>"; if (a > b) {}</script>`)
		}
	}
	b.WriteString(`<div>SIGNATURES signatureword</div></body></html>`)
	return b.String()
}
