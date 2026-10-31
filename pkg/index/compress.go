package index

import (
	"encoding/binary"
	"fmt"
	"math"
	"math/bits"
)

// Integer compression for posting lists: delta (gap) encoding followed by
// Patched Frame-of-Reference (PFOR / PForDelta) bit packing.
//
// Bit packing dispatches through packBits/unpackBits (bitpack.go). On amd64
// (without the purego build tag), widths 8 and 16 use SSE2 assembly kernels
// for the largest prefix of a block that fills whole 128-bit vectors; every
// other width, every tail, and every other platform use the portable scalar
// code in this file. See docs/build/phase1.md.

// MaxStrictNext is the largest valid next argument for the strict delta
// functions: the successor of math.MaxUint32.
const MaxStrictNext = uint64(math.MaxUint32) + 1

// BlockSize is the maximum number of integers in one PFOR block.
const BlockSize = 128

// PFOR block wire format (all fields little endian where multi-byte):
//
//	byte 0      n           number of values, 1..BlockSize
//	byte 1      width       bit width b of the packed low bits, 0..32
//	byte 2      exceptions  number of exception entries e, 0..n
//	ceil(n*b/8) packed      low b bits of each value, LSB-first bit stream;
//	                        unused high bits of the final byte must be zero
//	e entries   exception   1 byte position (strictly increasing, < n)
//	                        followed by uvarint high = value >> b (high > 0)
//
// The encoder picks b to minimise the encoded size of the block, so values
// that do not fit in b bits are stored as exceptions ("patches").
const pforHeaderSize = 3

// DeltaEncode writes the gaps of non-decreasing src into dst:
// dst[0] = src[0], dst[i] = src[i] - src[i-1]. dst may alias src.
func DeltaEncode(dst, src []uint32) error {
	if len(dst) < len(src) {
		return fmt.Errorf("%w: delta dst len %d < src len %d", ErrInvalidArgument, len(dst), len(src))
	}
	var prev uint32
	for i, v := range src {
		if v < prev {
			return fmt.Errorf("%w: delta input not non-decreasing at %d (%d < %d)", ErrInvalidArgument, i, v, prev)
		}
		dst[i] = v - prev
		prev = v
	}
	return nil
}

// DeltaDecode reverses DeltaEncode. It returns ErrCorrupt if the running sum
// overflows uint32. dst may alias src.
func DeltaDecode(dst, src []uint32) error {
	if len(dst) < len(src) {
		return fmt.Errorf("%w: delta dst len %d < src len %d", ErrInvalidArgument, len(dst), len(src))
	}
	var acc uint64
	for i, g := range src {
		acc += uint64(g)
		if acc > math.MaxUint32 {
			return fmt.Errorf("%w: delta sum overflows uint32 at %d", ErrCorrupt, i)
		}
		dst[i] = uint32(acc)
	}
	return nil
}

// DeltaEncodeStrict encodes strictly increasing src as gaps minus one, so
// consecutive document IDs encode as 0. next is the smallest value the first
// element may take (0 for a fresh list, previous value + 1 when continuing a
// list across blocks). Valid next values are 0..MaxStrictNext; next equal to
// MaxStrictNext (the successor of math.MaxUint32) only admits an empty src.
// Larger next values are rejected with ErrInvalidArgument before any
// arithmetic, so the uint64 running value cannot wrap. It returns the next
// value for a following block. dst may alias src.
func DeltaEncodeStrict(dst, src []uint32, next uint64) (uint64, error) {
	if len(dst) < len(src) {
		return next, fmt.Errorf("%w: delta dst len %d < src len %d", ErrInvalidArgument, len(dst), len(src))
	}
	if next > MaxStrictNext {
		return next, fmt.Errorf("%w: strict delta next %d > 2^32", ErrInvalidArgument, next)
	}
	for i, v := range src {
		if uint64(v) < next {
			return next, fmt.Errorf("%w: strict delta input not strictly increasing at %d", ErrInvalidArgument, i)
		}
		dst[i] = uint32(uint64(v) - next)
		next = uint64(v) + 1
	}
	return next, nil
}

// DeltaDecodeStrict reverses DeltaEncodeStrict. next must be at most
// MaxStrictNext (ErrInvalidArgument otherwise, checked before arithmetic).
// It returns ErrCorrupt if a decoded value exceeds math.MaxUint32. Because
// next <= 2^32 and each gap < 2^32, next+gap cannot overflow uint64.
// dst may alias src.
func DeltaDecodeStrict(dst, src []uint32, next uint64) (uint64, error) {
	if len(dst) < len(src) {
		return next, fmt.Errorf("%w: delta dst len %d < src len %d", ErrInvalidArgument, len(dst), len(src))
	}
	if next > MaxStrictNext {
		return next, fmt.Errorf("%w: strict delta next %d > 2^32", ErrInvalidArgument, next)
	}
	for i, g := range src {
		v := next + uint64(g)
		if v > math.MaxUint32 {
			return next, fmt.Errorf("%w: strict delta value overflows uint32 at %d", ErrCorrupt, i)
		}
		dst[i] = uint32(v)
		next = v + 1
	}
	return next, nil
}

// packedLen is the number of bytes needed to pack n values of width bits.
func packedLen(n int, width uint) int {
	return (n*int(width) + 7) / 8
}

// pforWidth chooses the bit width that minimises the encoded block size and
// returns it with the resulting exception count.
func pforWidth(vals []uint32) (width uint, exceptions int) {
	var hist [33]int // hist[k] = number of values whose bit length is k
	maxBits := 0
	for _, v := range vals {
		l := bits.Len32(v)
		hist[l]++
		if l > maxBits {
			maxBits = l
		}
	}
	n := len(vals)
	bestW, bestExc := maxBits, 0
	bestCost := packedLen(n, uint(maxBits))
	for w := 0; w < maxBits; w++ {
		cost := packedLen(n, uint(w))
		exc := 0
		for k := w + 1; k <= maxBits; k++ {
			if hist[k] == 0 {
				continue
			}
			highBytes := (k - w + 6) / 7 // uvarint length of a (k-w)-bit value
			cost += hist[k] * (1 + highBytes)
			exc += hist[k]
		}
		if cost < bestCost {
			bestW, bestExc, bestCost = w, exc, cost
		}
	}
	return uint(bestW), bestExc
}

// AppendPFOR appends the PFOR encoding of vals (1..BlockSize values) to dst.
func AppendPFOR(dst []byte, vals []uint32) ([]byte, error) {
	n := len(vals)
	if n == 0 || n > BlockSize {
		return dst, fmt.Errorf("%w: PFOR block size %d outside [1, %d]", ErrInvalidArgument, n, BlockSize)
	}
	width, exc := pforWidth(vals)
	dst = append(dst, byte(n), byte(width), byte(exc))
	dst = packBits(dst, vals, width)
	if exc > 0 {
		for i, v := range vals {
			if high := v >> width; high != 0 {
				dst = append(dst, byte(i))
				dst = binary.AppendUvarint(dst, uint64(high))
			}
		}
	}
	return dst, nil
}

// DecodePFOR decodes one PFOR block from the front of src into dst, which
// must have length >= BlockSize. It returns the number of values decoded and
// the number of bytes consumed. Any malformed input yields ErrCorrupt; the
// decoder never reads outside src.
func DecodePFOR(dst []uint32, src []byte) (n, consumed int, err error) {
	if len(dst) < BlockSize {
		return 0, 0, fmt.Errorf("%w: PFOR dst len %d < %d", ErrInvalidArgument, len(dst), BlockSize)
	}
	if len(src) < pforHeaderSize {
		return 0, 0, fmt.Errorf("%w: PFOR header truncated (%d bytes)", ErrCorrupt, len(src))
	}
	n = int(src[0])
	width := uint(src[1])
	exc := int(src[2])
	if n == 0 || n > BlockSize {
		return 0, 0, fmt.Errorf("%w: PFOR block size %d", ErrCorrupt, n)
	}
	if width > 32 {
		return 0, 0, fmt.Errorf("%w: PFOR bit width %d", ErrCorrupt, width)
	}
	if exc > n || (exc > 0 && width == 32) {
		return 0, 0, fmt.Errorf("%w: PFOR exception count %d (n=%d, width=%d)", ErrCorrupt, exc, n, width)
	}
	pos := pforHeaderSize
	plen := packedLen(n, width)
	if len(src)-pos < plen {
		return 0, 0, fmt.Errorf("%w: PFOR packed data truncated", ErrCorrupt)
	}
	if !unpackBits(dst[:n], src[pos:pos+plen], width) {
		return 0, 0, fmt.Errorf("%w: PFOR non-zero padding bits", ErrCorrupt)
	}
	pos += plen
	maxHigh := uint64(math.MaxUint32) >> width
	last := -1
	for i := 0; i < exc; i++ {
		if pos >= len(src) {
			return 0, 0, fmt.Errorf("%w: PFOR exception %d truncated", ErrCorrupt, i)
		}
		p := int(src[pos])
		pos++
		if p >= n || p <= last {
			return 0, 0, fmt.Errorf("%w: PFOR exception position %d invalid", ErrCorrupt, p)
		}
		last = p
		high, m := binary.Uvarint(src[pos:])
		if m <= 0 {
			return 0, 0, fmt.Errorf("%w: PFOR exception %d value malformed", ErrCorrupt, i)
		}
		pos += m
		if high == 0 || high > maxHigh {
			return 0, 0, fmt.Errorf("%w: PFOR exception value %d out of range", ErrCorrupt, high)
		}
		dst[p] |= uint32(high) << width
	}
	return n, pos, nil
}

// packBitsScalar appends the low width bits of every value as an LSB-first
// bit stream. Portable scalar implementation (no SIMD).
func packBitsScalar(dst []byte, vals []uint32, width uint) []byte {
	if width == 0 {
		return dst
	}
	mask := uint64(1)<<width - 1
	var acc uint64
	var nbits uint
	for _, v := range vals {
		acc |= (uint64(v) & mask) << nbits
		nbits += width
		for nbits >= 8 {
			dst = append(dst, byte(acc))
			acc >>= 8
			nbits -= 8
		}
	}
	if nbits > 0 {
		dst = append(dst, byte(acc))
	}
	return dst
}

// unpackBitsScalar reverses packBitsScalar. src must be exactly
// packedLen(len(dst), width) bytes. It reports false if the padding bits of
// the final byte are non-zero. Portable scalar implementation (no SIMD).
func unpackBitsScalar(dst []uint32, src []byte, width uint) bool {
	if width == 0 {
		clear(dst)
		return true
	}
	mask := uint64(1)<<width - 1
	var acc uint64
	var nbits uint
	pos := 0
	for i := range dst {
		for nbits < width {
			acc |= uint64(src[pos]) << nbits
			pos++
			nbits += 8
		}
		dst[i] = uint32(acc & mask)
		acc >>= width
		nbits -= width
	}
	return acc == 0
}
