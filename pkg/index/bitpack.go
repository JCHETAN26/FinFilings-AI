package index

import "slices"

// packBits appends the low width bits of every value as an LSB-first bit
// stream (the PFOR packed-data layout). When simdGroup(width) > 0, the
// largest prefix of vals that is a multiple of the group size is packed by
// a SIMD kernel and the remainder by packBitsScalar; because SIMD widths are
// byte multiples, the tail always starts on a byte boundary, so both paths
// produce byte-identical output to packBitsScalar alone.
func packBits(dst []byte, vals []uint32, width uint) []byte {
	if g := simdGroup(width); g > 0 {
		if n := len(vals) / g * g; n > 0 {
			off := len(dst)
			dst = slices.Grow(dst, packedLen(len(vals), width))
			dst = dst[:off+packedLen(n, width)]
			simdPack(dst[off:], vals[:n], width)
			vals = vals[n:]
		}
	}
	return packBitsScalar(dst, vals, width)
}

// unpackBits reverses packBits. src must be exactly packedLen(len(dst),
// width) bytes. It reports false if padding bits of the final byte are
// non-zero (impossible for the byte-aligned SIMD widths).
func unpackBits(dst []uint32, src []byte, width uint) bool {
	if g := simdGroup(width); g > 0 {
		if n := len(dst) / g * g; n > 0 {
			nb := packedLen(n, width)
			simdUnpack(dst[:n], src[:nb], width)
			dst, src = dst[n:], src[nb:]
		}
	}
	return unpackBitsScalar(dst, src, width)
}
