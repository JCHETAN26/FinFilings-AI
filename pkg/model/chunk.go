// Package model defines the data types shared across finfilings-ai packages.
package model

import (
	"fmt"
	"unicode/utf8"
)

// FormType is an SEC periodic report form type.
type FormType string

// Supported SEC form types. Amendments are indexed like their base form.
const (
	Form10K  FormType = "10-K"
	Form10KA FormType = "10-K/A"
	Form10Q  FormType = "10-Q"
	Form10QA FormType = "10-Q/A"
)

// Valid reports whether f is one of the supported form types.
func (f FormType) Valid() bool {
	switch f {
	case Form10K, Form10KA, Form10Q, Form10QA:
		return true
	default:
		return false
	}
}

// Field limits enforced by Validate. They bound the size of stored fields in
// index segments and keep malformed upstream data from inflating segments.
const (
	MaxIDLen          = 256
	MaxTickerLen      = 16
	MaxCIKLen         = 10
	MaxSectionItemLen = 128
	MinFilingYear     = 1993 // first year of EDGAR electronic filings
	MaxFilingYear     = 2100
)

// ValidationError reports an invalid SECChunk field.
type ValidationError struct {
	ChunkID string
	Field   string
	Reason  string
}

func (e *ValidationError) Error() string {
	return fmt.Sprintf("invalid chunk %q: field %s: %s", e.ChunkID, e.Field, e.Reason)
}

// SECChunk is one passage of a 10-K or 10-Q filing.
//
// Tokens vs Text: when Tokens is non-empty it replaces Text as the input to
// the index analyzer, and Text is not indexed. Tokens are not indexed
// verbatim: each element is normalized by the same analyzer that processes
// queries (split on non-letter/digit runes, lower-cased, over-long pieces
// dropped), so one element may yield zero, one, or several terms (e.g.
// "10-K" yields "10" and "k"). This keeps every indexed term reachable by a
// query. Use Tokens to control segmentation upstream (e.g. pre-cleaned
// words), not to inject exact terms.
//
// Storage: Text and Tokens are never stored in index segments; only the
// metadata returned by Meta is. Retrieval returns ChunkMeta, so a RAG
// pipeline needs a separate passage store keyed by ID to recover text.
type SECChunk struct {
	ID          string   // globally unique chunk identifier, e.g. "0000320193-23-000106#item7#0004"
	Ticker      string   // exchange ticker, e.g. "AAPL"
	CIK         string   // SEC Central Index Key, decimal digits, up to 10 characters
	FormType    FormType // 10-K, 10-K/A, 10-Q, or 10-Q/A
	FilingYear  int      // calendar year of the filing date
	SectionItem string   // filing item, e.g. "Item 1A" or "Item 7"
	Text        string   // passage text
	Tokens      []string // optional pre-split words; analyzer-normalized, replaces Text when non-empty
}

// ChunkMeta is the stored, retrievable metadata of an indexed chunk.
type ChunkMeta struct {
	ID          string
	Ticker      string
	CIK         string
	FormType    FormType
	FilingYear  int
	SectionItem string
}

// Meta returns the stored metadata subset of c.
func (c *SECChunk) Meta() ChunkMeta {
	return ChunkMeta{
		ID:          c.ID,
		Ticker:      c.Ticker,
		CIK:         c.CIK,
		FormType:    c.FormType,
		FilingYear:  c.FilingYear,
		SectionItem: c.SectionItem,
	}
}

// Validate checks field presence, lengths, and formats. It returns a
// *ValidationError describing the first invalid field.
func (c *SECChunk) Validate() error {
	fail := func(field, reason string) error {
		return &ValidationError{ChunkID: c.ID, Field: field, Reason: reason}
	}
	if err := checkString(c.ID, MaxIDLen); err != "" {
		return fail("ID", err)
	}
	if err := checkString(c.Ticker, MaxTickerLen); err != "" {
		return fail("Ticker", err)
	}
	if err := checkString(c.CIK, MaxCIKLen); err != "" {
		return fail("CIK", err)
	}
	for i := 0; i < len(c.CIK); i++ {
		if c.CIK[i] < '0' || c.CIK[i] > '9' {
			return fail("CIK", "must contain only decimal digits")
		}
	}
	if !c.FormType.Valid() {
		return fail("FormType", fmt.Sprintf("unsupported form type %q", string(c.FormType)))
	}
	if c.FilingYear < MinFilingYear || c.FilingYear > MaxFilingYear {
		return fail("FilingYear", fmt.Sprintf("%d outside [%d, %d]", c.FilingYear, MinFilingYear, MaxFilingYear))
	}
	if err := checkString(c.SectionItem, MaxSectionItemLen); err != "" {
		return fail("SectionItem", err)
	}
	if len(c.Tokens) == 0 && c.Text == "" {
		return fail("Text", "Text and Tokens are both empty")
	}
	return nil
}

func checkString(s string, maxLen int) string {
	switch {
	case s == "":
		return "must not be empty"
	case len(s) > maxLen:
		return fmt.Sprintf("length %d exceeds %d bytes", len(s), maxLen)
	case !utf8.ValidString(s):
		return "must be valid UTF-8"
	default:
		return ""
	}
}
