//go:build !(linux || darwin || freebsd || netbsd || openbsd || dragonfly)

package index

import "fmt"

// mappedFile is unavailable on this platform; see mmap.go.
type mappedFile struct {
	data []byte
}

func mapFile(path string) (*mappedFile, error) {
	return nil, fmt.Errorf("%w: %s", ErrUnsupportedPlatform, path)
}

func (m *mappedFile) unmap() error { return nil }
