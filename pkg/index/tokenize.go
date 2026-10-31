package index

import (
	"unicode"
	"unicode/utf8"
)

// tokenizer is the Phase 1 analyzer shared by ingestion and queries: it
// splits on any rune that is not a Unicode letter or digit, lower-cases
// letters, and drops tokens longer than maxTermLen bytes. There is no
// stemming or stop-word removal. A tokenizer reuses its buffer and is not
// safe for concurrent use.
type tokenizer struct {
	maxTermLen int
	buf        []byte
}

func newTokenizer(maxTermLen int) tokenizer {
	return tokenizer{maxTermLen: maxTermLen, buf: make([]byte, 0, maxTermLen+utf8.UTFMax)}
}

// next returns the next token in text at or after byte offset pos and the
// offset to resume from. tok is nil when text is exhausted. tok aliases the
// tokenizer's buffer and is valid only until the next call.
func (t *tokenizer) next(text string, pos int) (tok []byte, resume int) {
	for pos < len(text) {
		// Skip separators.
		r, size := decodeRune(text, pos)
		if !isTokenRune(r) {
			pos += size
			continue
		}
		t.buf = t.buf[:0]
		overflow := false
		for pos < len(text) {
			r, size = decodeRune(text, pos)
			if !isTokenRune(r) {
				break
			}
			pos += size
			if overflow {
				continue
			}
			if r < utf8.RuneSelf {
				c := byte(r)
				if 'A' <= c && c <= 'Z' {
					c += 'a' - 'A'
				}
				t.buf = append(t.buf, c)
			} else {
				t.buf = utf8.AppendRune(t.buf, unicode.ToLower(r))
			}
			if len(t.buf) > t.maxTermLen {
				overflow = true
			}
		}
		if !overflow {
			return t.buf, pos
		}
	}
	return nil, pos
}

func decodeRune(s string, pos int) (rune, int) {
	if c := s[pos]; c < utf8.RuneSelf {
		return rune(c), 1
	}
	return utf8.DecodeRuneInString(s[pos:])
}

func isTokenRune(r rune) bool {
	if r < utf8.RuneSelf {
		return ('a' <= r && r <= 'z') || ('A' <= r && r <= 'Z') || ('0' <= r && r <= '9')
	}
	return r != utf8.RuneError && (unicode.IsLetter(r) || unicode.IsDigit(r))
}
