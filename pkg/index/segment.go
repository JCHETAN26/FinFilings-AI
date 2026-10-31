package index

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"fmt"
	"hash/crc32"
	"io"
	"math"
	"os"

	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// Segment file format, version 1. All integers are little endian. A segment
// is immutable once published; it is written to a temporary file, fsynced,
// and renamed into place.
//
//	offset 0          "FFSEGv01"                      8-byte head magic
//	postings          concatenated posting lists       see postings.go
//	termBytes         concatenated term bytes, sorted
//	termEntries       termCount x 32-byte entries:
//	                    u32 termOff   offset into termBytes
//	                    u32 termLen   1..MaxTermLenLimit
//	                    u32 df        1..docCount
//	                    u32 reserved  must be 0
//	                    u64 postOff   offset into postings
//	                    u64 postLen   > 0
//	docLens           docCount x u32 token count per document
//	stored            concatenated stored-field records (see appendStored)
//	storedIdx         (docCount+1) x u64 offsets into stored; [0]=0, last=len
//	footer            segmentFooterSize bytes:
//	                    u32 version, u32 flags (0), u32 docCount,
//	                    u32 termCount, u64 totalTokens,
//	                    6 x (u64 off, u64 len) for the sections above in order,
//	                    u32 crc32c of every byte before this field,
//	                    u32 reserved (0), "FFSEGEND" tail magic
//
// Sections are contiguous and in the order listed. Terms are strictly
// increasing in bytewise order, term entries reference contiguous ascending
// posting ranges that exactly cover the postings section, and
// sum(docLens) == totalTokens. All of this is validated on open; posting
// payloads are validated lazily while decoding during search.
const (
	segmentHeadMagic  = "FFSEGv01"
	segmentTailMagic  = "FFSEGEND"
	segmentVersion    = 1
	segmentFooterSize = 4*4 + 8 + 6*16 + 4 + 4 + 8 // 136
	segmentCRCOffset  = segmentFooterSize - 16     // crc offset within footer
	termEntrySize     = 32
	numSections       = 6

	// MaxTermLenLimit is the largest permitted MaxTermLen.
	MaxTermLenLimit = 1024
	// MaxDocsPerSegmentLimit is the largest permitted MaxDocsPerSegment.
	// Doc IDs are uint32 and BM25 accumulators are sized per segment.
	MaxDocsPerSegmentLimit = 1 << 26
)

// segment is a read-only view of one memory-mapped segment file. Every byte
// slice field aliases the mapping and must never escape the package or be
// used after the owning Index has unmapped it.
type segment struct {
	name        string
	file        *mappedFile
	docCount    uint32
	termCount   uint32
	totalTokens uint64
	postings    []byte
	termBytes   []byte
	termEntries []byte
	docLens     []byte
	stored      []byte
	storedIdx   []byte
}

// parseSegment validates data as a complete segment and returns a view over
// it. data is not copied.
func parseSegment(name string, data []byte, tab *crc32.Table) (*segment, error) {
	corrupt := func(format string, args ...any) error {
		return fmt.Errorf("%w: segment %s: %s", ErrCorrupt, name, fmt.Sprintf(format, args...))
	}
	if len(data) < len(segmentHeadMagic)+segmentFooterSize {
		return nil, corrupt("truncated: %d bytes", len(data))
	}
	if string(data[:len(segmentHeadMagic)]) != segmentHeadMagic {
		return nil, corrupt("bad head magic")
	}
	footerStart := len(data) - segmentFooterSize
	f := data[footerStart:]
	if string(f[segmentFooterSize-8:]) != segmentTailMagic {
		return nil, corrupt("bad tail magic (truncated or not a segment)")
	}
	le := binary.LittleEndian
	wantCRC := le.Uint32(f[segmentCRCOffset:])
	if got := crc32.Checksum(data[:footerStart+segmentCRCOffset], tab); got != wantCRC {
		return nil, corrupt("checksum mismatch: stored %08x computed %08x", wantCRC, got)
	}
	if v := le.Uint32(f[0:]); v != segmentVersion {
		return nil, corrupt("unsupported version %d", v)
	}
	if flags, reserved := le.Uint32(f[4:]), le.Uint32(f[segmentCRCOffset+4:]); flags != 0 || reserved != 0 {
		return nil, corrupt("non-zero flags/reserved")
	}
	s := &segment{
		name:        name,
		docCount:    le.Uint32(f[8:]),
		termCount:   le.Uint32(f[12:]),
		totalTokens: le.Uint64(f[16:]),
	}
	if s.docCount == 0 || s.docCount > MaxDocsPerSegmentLimit {
		return nil, corrupt("doc count %d out of range", s.docCount)
	}
	var sections [numSections][]byte
	expectOff := uint64(len(segmentHeadMagic))
	for i := range numSections {
		off := le.Uint64(f[24+16*i:])
		n := le.Uint64(f[32+16*i:])
		if off != expectOff || n > uint64(footerStart)-off {
			return nil, corrupt("section %d range [%d,+%d) invalid", i, off, n)
		}
		sections[i] = data[off : off+n : off+n]
		expectOff = off + n
	}
	if expectOff != uint64(footerStart) {
		return nil, corrupt("sections end at %d, footer at %d", expectOff, footerStart)
	}
	s.postings, s.termBytes, s.termEntries = sections[0], sections[1], sections[2]
	s.docLens, s.stored, s.storedIdx = sections[3], sections[4], sections[5]

	if uint64(len(s.termEntries)) != uint64(s.termCount)*termEntrySize {
		return nil, corrupt("term entries size %d for %d terms", len(s.termEntries), s.termCount)
	}
	if uint64(len(s.docLens)) != uint64(s.docCount)*4 {
		return nil, corrupt("doc lengths size %d for %d docs", len(s.docLens), s.docCount)
	}
	if uint64(len(s.storedIdx)) != (uint64(s.docCount)+1)*8 {
		return nil, corrupt("stored index size %d for %d docs", len(s.storedIdx), s.docCount)
	}
	if err := s.validateTerms(); err != nil {
		return nil, corrupt("%v", err)
	}
	var sum uint64
	for d := range s.docCount {
		sum += uint64(le.Uint32(s.docLens[4*d:]))
	}
	if sum != s.totalTokens {
		return nil, corrupt("doc lengths sum %d != total tokens %d", sum, s.totalTokens)
	}
	prev := uint64(0)
	if le.Uint64(s.storedIdx) != 0 {
		return nil, corrupt("stored index does not start at 0")
	}
	for d := uint32(1); d <= s.docCount; d++ {
		off := le.Uint64(s.storedIdx[8*d:])
		if off <= prev || off > uint64(len(s.stored)) {
			return nil, corrupt("stored offset %d for doc %d invalid", off, d)
		}
		prev = off
	}
	if prev != uint64(len(s.stored)) {
		return nil, corrupt("stored index ends at %d, section is %d bytes", prev, len(s.stored))
	}
	return s, nil
}

func (s *segment) validateTerms() error {
	le := binary.LittleEndian
	var prevTerm []byte
	postEnd := uint64(0)
	for i := range s.termCount {
		e := s.termEntries[i*termEntrySize:]
		termOff, termLen := uint64(le.Uint32(e[0:])), uint64(le.Uint32(e[4:]))
		df, reserved := le.Uint32(e[8:]), le.Uint32(e[12:])
		postOff, postLen := le.Uint64(e[16:]), le.Uint64(e[24:])
		if termLen == 0 || termLen > MaxTermLenLimit || termOff+termLen > uint64(len(s.termBytes)) {
			return fmt.Errorf("term %d bytes [%d,+%d) invalid", i, termOff, termLen)
		}
		term := s.termBytes[termOff : termOff+termLen]
		if i > 0 && bytes.Compare(prevTerm, term) >= 0 {
			return fmt.Errorf("terms not strictly sorted at %d", i)
		}
		prevTerm = term
		if df == 0 || df > s.docCount || reserved != 0 {
			return fmt.Errorf("term %d df %d invalid", i, df)
		}
		if postOff != postEnd || postLen == 0 || postLen > uint64(len(s.postings))-postOff {
			return fmt.Errorf("term %d postings [%d,+%d) invalid", i, postOff, postLen)
		}
		postEnd = postOff + postLen
	}
	if postEnd != uint64(len(s.postings)) {
		return fmt.Errorf("postings cover %d of %d bytes", postEnd, len(s.postings))
	}
	return nil
}

// lookup binary-searches the term dictionary.
func (s *segment) lookup(term []byte) (int, bool) {
	lo, hi := 0, int(s.termCount)
	for lo < hi {
		mid := int(uint(lo+hi) >> 1)
		if bytes.Compare(s.term(mid), term) < 0 {
			lo = mid + 1
		} else {
			hi = mid
		}
	}
	return lo, lo < int(s.termCount) && bytes.Equal(s.term(lo), term)
}

func (s *segment) term(i int) []byte {
	e := s.termEntries[i*termEntrySize:]
	off := binary.LittleEndian.Uint32(e[0:])
	n := binary.LittleEndian.Uint32(e[4:])
	return s.termBytes[off : off+n]
}

func (s *segment) postingsOf(i int) (df uint32, list []byte) {
	e := s.termEntries[i*termEntrySize:]
	df = binary.LittleEndian.Uint32(e[8:])
	off := binary.LittleEndian.Uint64(e[16:])
	n := binary.LittleEndian.Uint64(e[24:])
	return df, s.postings[off : off+n]
}

func (s *segment) docLen(d uint32) uint32 {
	return binary.LittleEndian.Uint32(s.docLens[4*d:])
}

// meta decodes the stored fields of doc d, copying all strings out of the
// mapping.
func (s *segment) meta(d uint32) (model.ChunkMeta, error) {
	off := binary.LittleEndian.Uint64(s.storedIdx[8*d:])
	end := binary.LittleEndian.Uint64(s.storedIdx[8*(d+1):])
	m, err := decodeStored(s.stored[off:end])
	if err != nil {
		return m, fmt.Errorf("%w: segment %s doc %d: %v", ErrCorrupt, s.name, d, err)
	}
	return m, nil
}

// Stored record: five uvarint-length-prefixed strings (ID, Ticker, CIK,
// FormType, SectionItem) then uvarint FilingYear. The record must consume
// its byte range exactly.
func appendStored(dst []byte, c *model.SECChunk) []byte {
	for _, f := range [...]string{c.ID, c.Ticker, c.CIK, string(c.FormType), c.SectionItem} {
		dst = binary.AppendUvarint(dst, uint64(len(f)))
		dst = append(dst, f...)
	}
	return binary.AppendUvarint(dst, uint64(c.FilingYear))
}

func decodeStored(b []byte) (model.ChunkMeta, error) {
	var fields [5]string
	for i := range fields {
		n, m := binary.Uvarint(b)
		if m <= 0 || n > uint64(len(b)-m) {
			return model.ChunkMeta{}, fmt.Errorf("stored field %d malformed", i)
		}
		fields[i] = string(b[m : m+int(n)])
		b = b[m+int(n):]
	}
	year, m := binary.Uvarint(b)
	if m <= 0 || m != len(b) || year > math.MaxInt32 {
		return model.ChunkMeta{}, fmt.Errorf("stored filing year malformed")
	}
	return model.ChunkMeta{
		ID: fields[0], Ticker: fields[1], CIK: fields[2], FormType: model.FormType(fields[3]),
		SectionItem: fields[4], FilingYear: int(year),
	}, nil
}

// crcWriter forwards writes to w while maintaining a CRC-32C and byte count.
type crcWriter struct {
	w   io.Writer
	tab *crc32.Table
	crc uint32
	n   uint64
}

func (c *crcWriter) Write(p []byte) (int, error) {
	n, err := c.w.Write(p)
	c.crc = crc32.Update(c.crc, c.tab, p[:n])
	c.n += uint64(n)
	return n, err
}

// writeSegmentFile streams buf to path as a published segment: temp file,
// fsync, rename. scratch buffers are owned by the caller.
func writeSegmentFile(path string, buf *memSegment, tab *crc32.Table, sc *flushScratch) (err error) {
	tmp, err := os.CreateTemp(dirOf(path), ".seg-*.tmp")
	if err != nil {
		return fmt.Errorf("index: create segment temp file: %w", err)
	}
	tmpName := tmp.Name()
	defer func() {
		if err != nil {
			_ = tmp.Close()
			_ = os.Remove(tmpName)
		}
	}()
	bw := bufio.NewWriterSize(tmp, 1<<20)
	cw := &crcWriter{w: bw, tab: tab}
	le := binary.LittleEndian

	if _, err = io.WriteString(cw, segmentHeadMagic); err != nil {
		return err
	}
	terms := buf.sortedTerms(sc.terms[:0])
	sc.terms = terms
	var sections [numSections][2]uint64
	sections[0][0] = cw.n
	entries := sc.entries[:0]
	var termOff uint64
	for _, t := range terms {
		pb := buf.postings[t]
		start := cw.n
		sc.enc, err = appendPostings(sc.enc[:0], pb.docs, pb.tfs, &sc.block)
		if err != nil {
			return err
		}
		if _, err = cw.Write(sc.enc); err != nil {
			return err
		}
		if termOff+uint64(len(t)) > math.MaxUint32 {
			return fmt.Errorf("%w: term dictionary exceeds 4 GiB", ErrInvalidArgument)
		}
		var e [termEntrySize]byte
		le.PutUint32(e[0:], uint32(termOff))
		le.PutUint32(e[4:], uint32(len(t)))
		le.PutUint32(e[8:], uint32(len(pb.docs)))
		le.PutUint64(e[16:], start-sections[0][0])
		le.PutUint64(e[24:], cw.n-start)
		entries = append(entries, e)
		termOff += uint64(len(t))
	}
	sc.entries = entries
	sections[0][1] = cw.n - sections[0][0]

	sections[1][0] = cw.n
	for _, t := range terms {
		if _, err = io.WriteString(cw, t); err != nil {
			return err
		}
	}
	sections[1][1] = cw.n - sections[1][0]

	sections[2][0] = cw.n
	for i := range entries {
		if _, err = cw.Write(entries[i][:]); err != nil {
			return err
		}
	}
	sections[2][1] = cw.n - sections[2][0]

	var tmp8 [8]byte
	sections[3][0] = cw.n
	for _, l := range buf.docLens {
		le.PutUint32(tmp8[:4], l)
		if _, err = cw.Write(tmp8[:4]); err != nil {
			return err
		}
	}
	sections[3][1] = cw.n - sections[3][0]

	sections[4][0] = cw.n
	if _, err = cw.Write(buf.stored); err != nil {
		return err
	}
	sections[4][1] = cw.n - sections[4][0]

	sections[5][0] = cw.n
	for _, off := range buf.storedOffs {
		le.PutUint64(tmp8[:], off)
		if _, err = cw.Write(tmp8[:]); err != nil {
			return err
		}
	}
	sections[5][1] = cw.n - sections[5][0]

	var footer [segmentFooterSize]byte
	le.PutUint32(footer[0:], segmentVersion)
	le.PutUint32(footer[8:], uint32(len(buf.docLens)))
	le.PutUint32(footer[12:], uint32(len(terms)))
	le.PutUint64(footer[16:], buf.totalTokens)
	for i, sec := range sections {
		le.PutUint64(footer[24+16*i:], sec[0])
		le.PutUint64(footer[32+16*i:], sec[1])
	}
	if _, err = cw.Write(footer[:segmentCRCOffset]); err != nil {
		return err
	}
	le.PutUint32(footer[segmentCRCOffset:], cw.crc)
	copy(footer[segmentFooterSize-8:], segmentTailMagic)
	if _, err = bw.Write(footer[segmentCRCOffset:]); err != nil {
		return err
	}
	if err = bw.Flush(); err != nil {
		return fmt.Errorf("index: write segment: %w", err)
	}
	if err = tmp.Sync(); err != nil {
		return fmt.Errorf("index: sync segment: %w", err)
	}
	if err = tmp.Close(); err != nil {
		return fmt.Errorf("index: close segment: %w", err)
	}
	if err = os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("index: publish segment: %w", err)
	}
	return nil
}

// matches compares borrowed mmap bytes while Search holds the mapping lock.
// It avoids allocating five metadata strings for every candidate document.
func (s *segment) matches(d uint32, filter model.FilingFilter) (bool, error) {
	off := binary.LittleEndian.Uint64(s.storedIdx[8*d:])
	end := binary.LittleEndian.Uint64(s.storedIdx[8*(d+1):])
	b := s.stored[off:end]
	matched := true
	for field := 0; field < 5; field++ {
		n, consumed := binary.Uvarint(b)
		if consumed <= 0 || n > uint64(len(b)-consumed) {
			return false, fmt.Errorf("%w: malformed filter metadata", ErrCorrupt)
		}
		value := b[consumed : consumed+int(n)]
		switch field {
		case 0:
			if filter.Accession != "" {
				prefix := filter.Accession + "#"
				matched = matched && len(value) >= len(prefix) && string(value[:len(prefix)]) == prefix
			}
		case 1:
			matched = matched && (filter.Ticker == "" || string(value) == filter.Ticker)
		case 4:
			matched = matched && (filter.Section == "" || string(value) == filter.Section)
		}
		b = b[consumed+int(n):]
	}
	year, consumed := binary.Uvarint(b)
	if consumed <= 0 || consumed != len(b) || year > math.MaxInt32 {
		return false, fmt.Errorf("%w: malformed filter year", ErrCorrupt)
	}
	return matched && (filter.Year == 0 || uint64(filter.Year) == year), nil
}
