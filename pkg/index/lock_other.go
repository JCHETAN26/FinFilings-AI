//go:build !(linux || darwin || freebsd || netbsd || openbsd || dragonfly)

package index

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
)

// dirLock on platforms without flock: the lock is the exclusive creation
// (O_EXCL) of WriterLockName, removed on unlock. Unlike the flock version,
// a crash leaves the file behind and it must be removed manually after
// confirming no writer is running.
type dirLock struct {
	path string
}

func lockDir(dir string) (*dirLock, error) {
	path := filepath.Join(dir, WriterLockName)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if err != nil {
		if errors.Is(err, fs.ErrExist) {
			return nil, fmt.Errorf("%w: %s", ErrWriterLocked, dir)
		}
		return nil, fmt.Errorf("index: create writer lock: %w", err)
	}
	if err := f.Close(); err != nil {
		return nil, fmt.Errorf("index: create writer lock: %w", err)
	}
	return &dirLock{path: path}, nil
}

func (l *dirLock) unlock() error {
	if l == nil || l.path == "" {
		return nil
	}
	err := os.Remove(l.path)
	l.path = ""
	if err != nil {
		return fmt.Errorf("index: release writer lock: %w", err)
	}
	return nil
}
