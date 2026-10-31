//go:build linux || darwin || freebsd || netbsd || openbsd || dragonfly

package index

import (
	"fmt"
	"math"
	"os"
	"syscall"
)

// mappedFile owns a read-only, shared memory mapping of a whole file.
//
// Ownership and lifecycle:
//   - mapFile opens the file, maps it with PROT_READ/MAP_SHARED, and closes
//     the descriptor immediately; the mapping stays valid without it.
//   - Exactly one owner (a segment held by an Index) calls unmap, once, after
//     it has guaranteed that no reader still references data. Index enforces
//     this with its RWMutex: searches hold the read lock for their full
//     duration, and Close takes the write lock before unmapping.
//   - data is zero-copy: slices into it must never escape to callers. Search
//     results copy every string out of the mapping.
//   - Segment files are immutable after publish. If another process truncates
//     a mapped file, the kernel raises SIGBUS on access; that is outside the
//     supported contract and is not recoverable in-process.
type mappedFile struct {
	data []byte
}

func mapFile(path string) (*mappedFile, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("index: open segment: %w", err)
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return nil, fmt.Errorf("index: stat segment: %w", err)
	}
	size := st.Size()
	if size <= 0 {
		return nil, fmt.Errorf("%w: segment %s is empty", ErrCorrupt, path)
	}
	if size > math.MaxInt {
		return nil, fmt.Errorf("%w: segment %s too large to map (%d bytes)", ErrInvalidArgument, path, size)
	}
	data, err := syscall.Mmap(int(f.Fd()), 0, int(size), syscall.PROT_READ, syscall.MAP_SHARED)
	if err != nil {
		return nil, fmt.Errorf("index: mmap segment %s: %w", path, err)
	}
	return &mappedFile{data: data}, nil
}

// unmap releases the mapping. It is idempotent.
func (m *mappedFile) unmap() error {
	if m == nil || m.data == nil {
		return nil
	}
	err := syscall.Munmap(m.data)
	m.data = nil
	if err != nil {
		return fmt.Errorf("index: munmap: %w", err)
	}
	return nil
}
