//go:build !amd64 || purego

package index

import (
	"bytes"
	"slices"
	"testing"
)

func TestPortableKernelEntryPoints(t *testing.T) {
	vals := []uint32{0, 255, 256, 65535, 0xffffffff, 17, 1024}
	for width := uint(0); width <= 32; width++ {
		want := refPack(vals, width)
		got := make([]byte, len(want))
		simdPack(got, vals, width)
		if !bytes.Equal(got, want) {
			t.Fatalf("pack width %d", width)
		}
		dec := make([]uint32, len(vals))
		simdUnpack(dec, got, width)
		if !slices.Equal(dec, refUnpack(want, len(vals), width)) {
			t.Fatalf("unpack width %d", width)
		}
	}
}
