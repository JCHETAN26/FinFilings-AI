//go:build linux || darwin || freebsd || netbsd || openbsd || dragonfly

package index

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

// dirLock is an exclusive, advisory flock(2) on WriterLockName. The kernel
// releases it when the descriptor is closed or the process exits (including
// crashes), so a lock can never be left stale. flock locks belong to the
// open file description, so two Writers in the same process also exclude
// each other. The lock file itself is left in place; its presence alone
// means nothing.
type dirLock struct {
	f *os.File
}

func lockDir(dir string) (*dirLock, error) {
	f, err := os.OpenFile(filepath.Join(dir, WriterLockName), os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, fmt.Errorf("index: open writer lock: %w", err)
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = f.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, fmt.Errorf("%w: %s", ErrWriterLocked, dir)
		}
		return nil, fmt.Errorf("index: flock writer lock: %w", err)
	}
	return &dirLock{f: f}, nil
}

// unlock releases the lock. It is idempotent.
func (l *dirLock) unlock() error {
	if l == nil || l.f == nil {
		return nil
	}
	err := syscall.Flock(int(l.f.Fd()), syscall.LOCK_UN)
	err = errors.Join(err, l.f.Close())
	l.f = nil
	if err != nil {
		return fmt.Errorf("index: release writer lock: %w", err)
	}
	return nil
}
