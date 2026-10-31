package index

import (
	"context"
	"encoding/binary"
	"errors"
	"hash/crc32"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// fixtureSegment builds a one-segment index of n fixtures and returns its
// directory and the raw bytes of seg-000000.
func fixtureSegment(t *testing.T, n int) (dir string, data []byte) {
	t.Helper()
	dir = filepath.Join(t.TempDir(), "idx")
	buildFixtureIndex(t, dir, WriterOptions{}, n)
	data, err := os.ReadFile(filepath.Join(dir, segmentFileName(0)))
	if err != nil {
		t.Fatal(err)
	}
	return dir, data
}

// reseal recomputes the footer CRC so tests can reach structural
// validation behind the checksum.
func reseal(data []byte) []byte {
	foot := len(data) - segmentFooterSize
	crc := crc32.Checksum(data[:foot+segmentCRCOffset], crc32.MakeTable(crc32.Castagnoli))
	binary.LittleEndian.PutUint32(data[foot+segmentCRCOffset:], crc)
	return data
}

func footerU64(data []byte, off int) uint64 {
	return binary.LittleEndian.Uint64(data[len(data)-segmentFooterSize+off:])
}

func TestSegmentTruncationAtEveryLength(t *testing.T) {
	_, data := fixtureSegment(t, 40)
	tab := crc32.MakeTable(crc32.Castagnoli)
	if _, err := parseSegment("ok", data, tab); err != nil {
		t.Fatalf("intact segment rejected: %v", err)
	}
	for n := range len(data) {
		if _, err := parseSegment("trunc", data[:n], tab); !errors.Is(err, ErrCorrupt) {
			t.Fatalf("truncated to %d/%d bytes: err %v, want ErrCorrupt", n, len(data), err)
		}
	}
}

func TestSegmentBitFlipsFailChecksum(t *testing.T) {
	_, data := fixtureSegment(t, 40)
	tab := crc32.MakeTable(crc32.Castagnoli)
	for pos := 0; pos < len(data); pos += 13 {
		b := slices.Clone(data)
		b[pos] ^= 1 << (pos % 8)
		if _, err := parseSegment("flip", b, tab); !errors.Is(err, ErrCorrupt) {
			t.Fatalf("bit flip at %d: err %v, want ErrCorrupt", pos, err)
		}
	}
}

// TestSegmentStructuralValidation corrupts fields and re-seals the CRC, so
// only the structural checks can catch the damage.
func TestSegmentStructuralValidation(t *testing.T) {
	_, data := fixtureSegment(t, 40)
	tab := crc32.MakeTable(crc32.Castagnoli)
	le := binary.LittleEndian
	foot := len(data) - segmentFooterSize
	entries := int(footerU64(data, 24+16*2))
	docLens := int(footerU64(data, 24+16*3))
	storedIdx := int(footerU64(data, 24+16*5))
	cases := map[string]func(b []byte){
		"version":         func(b []byte) { le.PutUint32(b[foot:], 2) },
		"flags":           func(b []byte) { le.PutUint32(b[foot+4:], 1) },
		"doc-count-zero":  func(b []byte) { le.PutUint32(b[foot+8:], 0) },
		"doc-count-more":  func(b []byte) { le.PutUint32(b[foot+8:], 41) },
		"term-count":      func(b []byte) { le.PutUint32(b[foot+12:], le.Uint32(b[foot+12:])+1) },
		"total-tokens":    func(b []byte) { le.PutUint64(b[foot+16:], 1) },
		"section-offset":  func(b []byte) { le.PutUint64(b[foot+24:], 9) },
		"section-overlap": func(b []byte) { le.PutUint64(b[foot+32:], footerU64(b, 32)+1) },
		"section-huge":    func(b []byte) { le.PutUint64(b[foot+32:], 1<<62) },
		"terms-unsorted": func(b []byte) {
			e0, e1 := slices.Clone(b[entries:entries+termEntrySize]), b[entries+termEntrySize:entries+2*termEntrySize]
			copy(b[entries:], e1)
			copy(b[entries+termEntrySize:], e0)
		},
		"term-len-zero":    func(b []byte) { le.PutUint32(b[entries+4:], 0) },
		"term-out-of-blob": func(b []byte) { le.PutUint32(b[entries:], 1<<30) },
		"df-zero":          func(b []byte) { le.PutUint32(b[entries+8:], 0) },
		"df-gt-docs":       func(b []byte) { le.PutUint32(b[entries+8:], 41) },
		"entry-reserved":   func(b []byte) { le.PutUint32(b[entries+12:], 7) },
		"postings-gap":     func(b []byte) { le.PutUint64(b[entries+16:], 1) },
		"postings-len":     func(b []byte) { le.PutUint64(b[entries+24:], le.Uint64(b[entries+24:])+1) },
		"doc-len":          func(b []byte) { le.PutUint32(b[docLens:], le.Uint32(b[docLens:])+1) },
		"stored-idx-start": func(b []byte) { le.PutUint64(b[storedIdx:], 1) },
		"stored-idx-order": func(b []byte) { le.PutUint64(b[storedIdx+8:], 0) },
		"stored-idx-end":   func(b []byte) { le.PutUint64(b[storedIdx+8*40:], le.Uint64(b[storedIdx+8*40:])-1) },
		"tail-magic":       func(b []byte) { b[len(b)-1] = 'X' },
		"head-magic":       func(b []byte) { b[0] = 'X' },
	}
	for name, corrupt := range cases {
		b := slices.Clone(data)
		corrupt(b)
		if _, err := parseSegment(name, reseal(b), tab); !errors.Is(err, ErrCorrupt) {
			t.Errorf("%s: err %v, want ErrCorrupt", name, err)
		}
	}
}

// TestCorruptPostingsDetectedAtSearch damages a posting payload behind a
// valid CRC and structure; the search touching it must fail with
// ErrCorrupt rather than panic or return garbage.
func TestCorruptPostingsDetectedAtSearch(t *testing.T) {
	dir, data := fixtureSegment(t, 40)
	tab := crc32.MakeTable(crc32.Castagnoli)
	seg, err := parseSegment("orig", data, tab)
	if err != nil {
		t.Fatal(err)
	}
	firstTerm := string(seg.term(0))
	b := slices.Clone(data)
	b[len(segmentHeadMagic)] = 0 // first PFOR block of term 0: n = 0
	if err := os.WriteFile(filepath.Join(dir, segmentFileName(0)), reseal(b), 0o644); err != nil {
		t.Fatal(err)
	}
	ix, err := Open(dir, Options{})
	if err != nil {
		t.Fatalf("structure is intact, Open should succeed: %v", err)
	}
	defer ix.Close()
	if _, err := ix.Search(context.Background(), firstTerm, 10); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("search %q: err %v, want ErrCorrupt", firstTerm, err)
	}
	// Other terms remain searchable, and the scratch accumulator was reset.
	hits, err := ix.Search(context.Background(), "marker0001", 10)
	if err != nil || len(hits) != 1 {
		t.Fatalf("unaffected term: %v %v", hits, err)
	}
}

func TestCorruptStoredFieldsDetected(t *testing.T) {
	_, data := fixtureSegment(t, 5)
	tab := crc32.MakeTable(crc32.Castagnoli)
	storedOff := int(footerU64(data, 24+16*4))
	b := slices.Clone(data)
	b[storedOff] = 0x7f // ID length larger than the record
	seg, err := parseSegment("stored", reseal(b), tab)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := seg.meta(0); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("err %v, want ErrCorrupt", err)
	}
	if _, err := seg.meta(1); err != nil {
		t.Fatalf("doc 1 should decode: %v", err)
	}
}

func TestOpenRejectsCorruptFiles(t *testing.T) {
	cases := map[string]func(t *testing.T, dir string){
		"segment-truncated": func(t *testing.T, dir string) {
			p := filepath.Join(dir, segmentFileName(0))
			data, _ := os.ReadFile(p)
			must(t, os.WriteFile(p, data[:len(data)/2], 0o644))
		},
		"segment-empty": func(t *testing.T, dir string) {
			must(t, os.WriteFile(filepath.Join(dir, segmentFileName(0)), nil, 0o644))
		},
		"manifest-bad-crc": func(t *testing.T, dir string) {
			p := filepath.Join(dir, ManifestName)
			data, _ := os.ReadFile(p)
			data[0] ^= 1
			must(t, os.WriteFile(p, data, 0o644))
		},
		"manifest-truncated": func(t *testing.T, dir string) {
			p := filepath.Join(dir, ManifestName)
			data, _ := os.ReadFile(p)
			must(t, os.WriteFile(p, data[:len(data)-3], 0o644))
		},
		"manifest-path-traversal": func(t *testing.T, dir string) {
			m := &manifest{maxTermLen: 64, segments: []string{"../seg-000000.ffseg"}}
			must(t, os.WriteFile(filepath.Join(dir, ManifestName), encodeManifest(m, crc32.MakeTable(crc32.Castagnoli)), 0o644))
		},
		"manifest-bad-count": func(t *testing.T, dir string) {
			tab := crc32.MakeTable(crc32.Castagnoli)
			body := strings.Replace(string(encodeManifest(&manifest{maxTermLen: 64, segments: []string{segmentFileName(0)}}, tab)),
				"segments 1", "segments 2", 1)
			body = body[:strings.LastIndex(body[:len(body)-1], "\n")+1]
			body += "crc32c " + hex8(crc32.Checksum([]byte(body), tab)) + "\n"
			must(t, os.WriteFile(filepath.Join(dir, ManifestName), []byte(body), 0o644))
		},
	}
	for name, corrupt := range cases {
		t.Run(name, func(t *testing.T) {
			dir, _ := fixtureSegment(t, 20)
			corrupt(t, dir)
			if ix, err := Open(dir, Options{}); !errors.Is(err, ErrCorrupt) {
				if ix != nil {
					ix.Close()
				}
				t.Fatalf("err %v, want ErrCorrupt", err)
			}
		})
	}
	t.Run("segment-missing", func(t *testing.T) {
		dir, _ := fixtureSegment(t, 20)
		must(t, os.Remove(filepath.Join(dir, segmentFileName(0))))
		if _, err := Open(dir, Options{}); !errors.Is(err, fs.ErrNotExist) {
			t.Fatalf("err %v, want fs.ErrNotExist", err)
		}
	})
	t.Run("manifest-missing", func(t *testing.T) {
		if _, err := Open(t.TempDir(), Options{}); !errors.Is(err, fs.ErrNotExist) {
			t.Fatalf("err %v, want fs.ErrNotExist", err)
		}
	})
}

// FuzzParseSegment mutates a real segment (optionally re-sealing the CRC)
// and requires that parsing and full decoding of any accepted segment never
// panic and only fail with ErrCorrupt.
func FuzzParseSegment(f *testing.F) {
	dir := f.TempDir()
	buildFixtureIndex(f, dir, WriterOptions{}, 8)
	seed, err := os.ReadFile(filepath.Join(dir, segmentFileName(0)))
	if err != nil {
		f.Fatal(err)
	}
	f.Add(uint32(0), byte(0), false)
	f.Add(uint32(len(segmentHeadMagic)), byte(0xff), true)
	f.Add(uint32(len(seed)-segmentFooterSize-3), byte(1), true)
	tab := crc32.MakeTable(crc32.Castagnoli)
	f.Fuzz(func(t *testing.T, pos uint32, x byte, resealCRC bool) {
		b := slices.Clone(seed)
		b[int(pos)%len(b)] ^= x
		if resealCRC {
			reseal(b)
		}
		s, err := parseSegment("fuzz", b, tab)
		if err != nil {
			if !errors.Is(err, ErrCorrupt) {
				t.Fatalf("unexpected error class: %v", err)
			}
			return
		}
		var it postingIter
		for i := range int(s.termCount) {
			df, list := s.postingsOf(i)
			it.reset(list, df, s.docCount)
			for {
				n, err := it.nextBlock()
				if err != nil && !errors.Is(err, ErrCorrupt) {
					t.Fatalf("unexpected error class: %v", err)
				}
				if err != nil || n == 0 {
					break
				}
			}
		}
		for d := range s.docCount {
			if _, err := s.meta(d); err != nil && !errors.Is(err, ErrCorrupt) {
				t.Fatalf("unexpected error class: %v", err)
			}
		}
	})
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func hex8(v uint32) string {
	const digits = "0123456789abcdef"
	var b [8]byte
	for i := 7; i >= 0; i-- {
		b[i] = digits[v&0xf]
		v >>= 4
	}
	return string(b[:])
}
