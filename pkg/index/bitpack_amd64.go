//go:build amd64 && !purego

package index

// SSE2 is part of the amd64 baseline (GOAMD64=v1), so these kernels need no
// runtime CPU feature detection. Only widths 8 and 16 have SIMD kernels.

// simdKernels names the SIMD implementation compiled into this build.
const simdKernels = "amd64-sse2 (widths 8, 16)"

// simdGroup returns the number of values each SIMD loop iteration handles
// for width, or 0 if width has no SIMD kernel.
func simdGroup(width uint) int {
	switch width {
	case 8:
		return 16 // 4 input vectors -> 1 output vector
	case 16:
		return 8 // 2 input vectors -> 1 output vector
	default:
		return 0
	}
}

// simdPack packs vals (len a positive multiple of simdGroup(width)) into dst
// (len exactly packedLen(len(vals), width)).
func simdPack(dst []byte, vals []uint32, width uint) {
	n := len(vals)
	_ = dst[packedLen(n, width)-1] // bounds proof for the kernel's stores
	if width == 8 {
		pack8SSE2(&dst[0], &vals[0], n)
	} else {
		pack16SSE2(&dst[0], &vals[0], n)
	}
}

// simdUnpack unpacks src (len exactly packedLen(len(dst), width)) into dst
// (len a positive multiple of simdGroup(width)).
func simdUnpack(dst []uint32, src []byte, width uint) {
	n := len(dst)
	_ = src[packedLen(n, width)-1] // bounds proof for the kernel's loads
	if width == 8 {
		unpack8SSE2(&dst[0], &src[0], n)
	} else {
		unpack16SSE2(&dst[0], &src[0], n)
	}
}

// pack8SSE2 stores the low 8 bits of src[0:n] to dst[0:n]. n % 16 == 0, n > 0.
//
//go:noescape
func pack8SSE2(dst *byte, src *uint32, n int)

// pack16SSE2 stores the low 16 bits of src[0:n] little endian to
// dst[0:2n]. n % 8 == 0, n > 0.
//
//go:noescape
func pack16SSE2(dst *byte, src *uint32, n int)

// unpack8SSE2 zero-extends src[0:n] bytes into dst[0:n]. n % 16 == 0, n > 0.
//
//go:noescape
func unpack8SSE2(dst *uint32, src *byte, n int)

// unpack16SSE2 zero-extends n little-endian uint16 values from src into
// dst[0:n]. n % 8 == 0, n > 0.
//
//go:noescape
func unpack16SSE2(dst *uint32, src *byte, n int)
