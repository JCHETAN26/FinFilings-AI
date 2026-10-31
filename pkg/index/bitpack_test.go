package index

import (
	"bytes"
	"math/rand/v2"
	"slices"
	"testing"
)

// refPack is an independent bit-at-a-time reference for the PFOR packed
// layout (LSB-first bit stream, low width bits of each value). It shares no
// code with packBitsScalar or the SIMD kernels.
func refPack(vals []uint32, width uint) []byte {
	out := make([]byte, (len(vals)*int(width)+7)/8)
	bit := 0
	for _, v := range vals {
		for j := range width {
			if v>>j&1 == 1 {
				out[bit/8] |= 1 << (bit % 8)
			}
			bit++
		}
	}
	return out
}

func refUnpack(src []byte, n int, width uint) []uint32 {
	out := make([]uint32, n)
	bit := 0
	for i := range out {
		for j := range width {
			if src[bit/8]>>(bit%8)&1 == 1 {
				out[i] |= 1 << j
			}
			bit++
		}
	}
	return out
}

// TestBitPackDispatchMatchesReference checks the dispatching packBits and
// unpackBits (SIMD for widths 8/16 on amd64, scalar otherwise) against the
// reference for every width 0..32 and every length 0..300, which covers
// SIMD-only, SIMD+tail, and tail-only cases. Inputs carry bits above width,
// unaligned source offsets, and a non-empty dst prefix.
func TestBitPackDispatchMatchesReference(t *testing.T) {
	t.Logf("SIMD kernels in this build: %s", simdKernels)
	r := rand.New(rand.NewPCG(21, 22))
	backing := make([]uint32, 300+3)
	for width := uint(0); width <= 32; width++ {
		for n := 0; n <= 300; n++ {
			off := n % 4 // misalign the source relative to 16-byte boundaries
			vals := backing[off : off+n]
			for i := range vals {
				vals[i] = r.Uint32()
			}
			want := refPack(vals, width)
			prefix := []byte{0xDE, 0xAD, 0xBE}
			got := packBits(slices.Clone(prefix), vals, width)
			if !bytes.Equal(got[:3], prefix) || !bytes.Equal(got[3:], want) {
				t.Fatalf("pack width=%d n=%d (simd group %d): mismatch", width, n, simdGroup(width))
			}
			if scalar := packBitsScalar(nil, vals, width); !bytes.Equal(scalar, want) {
				t.Fatalf("scalar pack width=%d n=%d: mismatch", width, n)
			}
			dst := make([]uint32, n+1)[1:] // misaligned destination
			if !unpackBits(dst, want, width) {
				t.Fatalf("unpack width=%d n=%d: padding rejected", width, n)
			}
			if wantVals := refUnpack(want, n, width); !slices.Equal(dst, wantVals) {
				t.Fatalf("unpack width=%d n=%d (simd group %d): mismatch", width, n, simdGroup(width))
			}
		}
	}
}

// TestPFORUsesByteAlignedWidths confirms that typical blocks choose widths
// 8 and 16, so the SIMD kernels are actually exercised by AppendPFOR and
// DecodePFOR, and that those blocks roundtrip.
func TestPFORUsesByteAlignedWidths(t *testing.T) {
	r := rand.New(rand.NewPCG(9, 9))
	for _, w := range []uint{8, 16} {
		for _, n := range []int{BlockSize, BlockSize - 1, 17, 8, 7} {
			vals := make([]uint32, n)
			for i := range vals {
				vals[i] = uint32(r.IntN(1<<w-1<<(w-1))) + 1<<(w-1) // exactly w bits
			}
			enc := pforRoundtrip(t, vals)
			if uint(enc[1]) != w || enc[2] != 0 {
				t.Fatalf("n=%d: width %d exceptions %d, want width %d and none", n, enc[1], enc[2], w)
			}
			// Exceptions above a SIMD width: low bits go through the kernel
			// (which must mask them), high bits are patched.
			vals[0], vals[n-1] = 0xFFFFFFFF, 1<<31|vals[n-1]
			enc = pforRoundtrip(t, vals)
			if uint(enc[1]) != w || enc[2] != 2 {
				t.Fatalf("n=%d with outliers: width %d exceptions %d", n, enc[1], enc[2])
			}
		}
	}
}
