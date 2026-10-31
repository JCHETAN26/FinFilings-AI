//go:build !amd64 || purego

package index

// simdKernels names the SIMD implementation compiled into this build.
const simdKernels = "none (scalar only)"

// simdGroup reports that no width has a SIMD kernel on this build.
func simdGroup(uint) int { return 0 }

// Scalar implementations keep these internal entry points functional on
// portable builds; simdGroup still reports zero so dispatch uses scalar directly.
func simdPack(dst []byte, vals []uint32, width uint) {
	_ = packBitsScalar(dst[:0], vals, width)
}

func simdUnpack(dst []uint32, src []byte, width uint) {
	_ = unpackBitsScalar(dst, src, width)
}
