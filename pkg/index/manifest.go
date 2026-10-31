package index

import (
	"bytes"
	"fmt"
	"hash/crc32"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Manifest file format, version 1. The manifest is the single publish point
// of an index directory: segments not listed in it are ignored, and an index
// without a manifest cannot be opened. It is written to a temporary file,
// fsynced, renamed to ManifestName, and the directory is fsynced.
//
//	ffindex-manifest 1
//	max_term_len <int>
//	segment <name>          one line per segment, in doc-order
//	segments <count>
//	crc32c <8 lowercase hex digits over all preceding bytes>
//
// Lines end with '\n'. Segment names must match segmentFileName.
const (
	ManifestName      = "MANIFEST"
	manifestHeader    = "ffindex-manifest 1"
	maxManifestSize   = 16 << 20
	segmentNameFormat = "seg-%06d.ffseg"
)

type manifest struct {
	maxTermLen int
	segments   []string
}

func segmentFileName(seq int) string { return fmt.Sprintf(segmentNameFormat, seq) }

func validSegmentName(name string) bool {
	digits, ok := strings.CutPrefix(name, "seg-")
	if !ok {
		return false
	}
	if digits, ok = strings.CutSuffix(digits, ".ffseg"); !ok || len(digits) > 9 {
		return false
	}
	seq, err := strconv.Atoi(digits)
	return err == nil && seq >= 0 && segmentFileName(seq) == name
}

func encodeManifest(m *manifest, tab *crc32.Table) []byte {
	var b bytes.Buffer
	fmt.Fprintf(&b, "%s\nmax_term_len %d\n", manifestHeader, m.maxTermLen)
	for _, s := range m.segments {
		fmt.Fprintf(&b, "segment %s\n", s)
	}
	fmt.Fprintf(&b, "segments %d\n", len(m.segments))
	fmt.Fprintf(&b, "crc32c %08x\n", crc32.Checksum(b.Bytes(), tab))
	return b.Bytes()
}

func decodeManifest(data []byte, tab *crc32.Table) (*manifest, error) {
	corrupt := func(format string, args ...any) error {
		return fmt.Errorf("%w: manifest: %s", ErrCorrupt, fmt.Sprintf(format, args...))
	}
	if len(data) == 0 || data[len(data)-1] != '\n' {
		return nil, corrupt("truncated")
	}
	body := data[:len(data)-1]
	i := bytes.LastIndexByte(body, '\n')
	if i < 0 {
		return nil, corrupt("missing checksum line")
	}
	crcLine := string(body[i+1:])
	hexCRC, ok := strings.CutPrefix(crcLine, "crc32c ")
	if !ok || len(hexCRC) != 8 {
		return nil, corrupt("malformed checksum line")
	}
	want, err := strconv.ParseUint(hexCRC, 16, 32)
	if err != nil {
		return nil, corrupt("malformed checksum: %v", err)
	}
	if got := crc32.Checksum(data[:i+1], tab); uint32(want) != got {
		return nil, corrupt("checksum mismatch")
	}
	lines := strings.Split(string(data[:i]), "\n")
	if len(lines) < 3 || lines[0] != manifestHeader {
		return nil, corrupt("bad header")
	}
	m := &manifest{}
	v, ok := strings.CutPrefix(lines[1], "max_term_len ")
	if !ok {
		return nil, corrupt("missing max_term_len")
	}
	if m.maxTermLen, err = strconv.Atoi(v); err != nil || m.maxTermLen < 1 || m.maxTermLen > MaxTermLenLimit {
		return nil, corrupt("invalid max_term_len %q", v)
	}
	for _, l := range lines[2 : len(lines)-1] {
		name, ok := strings.CutPrefix(l, "segment ")
		if !ok || !validSegmentName(name) {
			return nil, corrupt("invalid segment line %q", l)
		}
		m.segments = append(m.segments, name)
	}
	v, ok = strings.CutPrefix(lines[len(lines)-1], "segments ")
	if !ok {
		return nil, corrupt("missing segment count")
	}
	if n, err := strconv.Atoi(v); err != nil || n != len(m.segments) {
		return nil, corrupt("segment count %q does not match %d lines", v, len(m.segments))
	}
	return m, nil
}

func readManifest(dir string, tab *crc32.Table) (*manifest, error) {
	path := filepath.Join(dir, ManifestName)
	st, err := os.Stat(path)
	if err != nil {
		return nil, fmt.Errorf("index: stat manifest: %w", err)
	}
	if st.Size() > maxManifestSize {
		return nil, fmt.Errorf("%w: manifest is %d bytes", ErrCorrupt, st.Size())
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("index: read manifest: %w", err)
	}
	return decodeManifest(data, tab)
}

func writeManifest(dir string, m *manifest, tab *crc32.Table) (err error) {
	tmp, err := os.CreateTemp(dir, ".manifest-*.tmp")
	if err != nil {
		return fmt.Errorf("index: create manifest: %w", err)
	}
	defer func() {
		if err != nil {
			_ = tmp.Close()
			_ = os.Remove(tmp.Name())
		}
	}()
	if _, err = tmp.Write(encodeManifest(m, tab)); err != nil {
		return fmt.Errorf("index: write manifest: %w", err)
	}
	if err = tmp.Sync(); err != nil {
		return fmt.Errorf("index: sync manifest: %w", err)
	}
	if err = tmp.Close(); err != nil {
		return fmt.Errorf("index: close manifest: %w", err)
	}
	if err = os.Rename(tmp.Name(), filepath.Join(dir, ManifestName)); err != nil {
		return fmt.Errorf("index: publish manifest: %w", err)
	}
	return syncDir(dir)
}

func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return fmt.Errorf("index: open dir for sync: %w", err)
	}
	defer d.Close()
	if err := d.Sync(); err != nil {
		return fmt.Errorf("index: sync dir: %w", err)
	}
	return nil
}
