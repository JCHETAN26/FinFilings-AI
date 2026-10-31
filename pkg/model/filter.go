package model

import (
	"fmt"
	"strings"
	"unicode"
)

// FilingFilter uses AND semantics and exact metadata matches. Empty fields are unconstrained.
// Accession is the 18-digit EDGAR accession prefix in verified SEC chunk IDs.
type FilingFilter struct {
	Ticker    string `json:"ticker,omitempty"`
	Year      int    `json:"year,omitempty"`
	Section   string `json:"section,omitempty"`
	Accession string `json:"accession,omitempty"`
}

func (f FilingFilter) Validate() error {
	if len(f.Ticker) > 16 || (f.Year != 0 && (f.Year < 1993 || f.Year > 2100)) || len(f.Section) > 128 {
		return fmt.Errorf("invalid filing filter")
	}
	for _, r := range f.Ticker {
		if !(r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '.' || r == '-') {
			return fmt.Errorf("invalid ticker filter")
		}
	}
	for _, r := range f.Section {
		if unicode.IsControl(r) {
			return fmt.Errorf("invalid section filter")
		}
	}
	if f.Accession != "" {
		if len(f.Accession) != 18 {
			return fmt.Errorf("invalid accession filter")
		}
		for _, r := range f.Accession {
			if r < '0' || r > '9' {
				return fmt.Errorf("invalid accession filter")
			}
		}
	}
	return nil
}

func (f FilingFilter) Matches(m ChunkMeta) bool {
	return (f.Ticker == "" || f.Ticker == m.Ticker) && (f.Year == 0 || f.Year == m.FilingYear) && (f.Section == "" || f.Section == m.SectionItem) && (f.Accession == "" || strings.HasPrefix(m.ID, f.Accession+"#passage-"))
}
