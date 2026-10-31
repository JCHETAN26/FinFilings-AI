package index

import (
	"encoding/binary"
	"errors"
	"math"
	"math/rand/v2"
	"slices"
	"testing"
)

func TestDeltaRoundtrip(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	for trial := range 200 {
		n := r.IntN(300)
		src := make([]uint32, n)
		var v uint32
		for i := range src {
			v += uint32(r.IntN(1 << uint(r.IntN(20))))
			src[i] = v
		}
		gaps := make([]uint32, n)
		if err := DeltaEncode(gaps, src); err != nil {
			t.Fatal(err)
		}
		got := make([]uint32, n)
		if err := DeltaDecode(got, gaps); err != nil {
			t.Fatal(err)
		}
		if !slices.Equal(got, src) {
			t.Fatalf("trial %d: delta roundtrip mismatch", trial)
		}
		// In place.
		buf := slices.Clone(src)
		if DeltaEncode(buf, buf) != nil || DeltaDecode(buf, buf) != nil || !slices.Equal(buf, src) {
			t.Fatalf("trial %d: in-place delta roundtrip mismatch", trial)
		}
	}
	if err := DeltaEncode(make([]uint32, 2), []uint32{5, 4}); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("decreasing input: %v", err)
	}
	if err := DeltaDecode(make([]uint32, 2), []uint32{math.MaxUint32, 1}); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("overflow: %v", err)
	}
	if err := DeltaEncode(make([]uint32, 1), []uint32{1, 2}); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("short dst: %v", err)
	}
}

func TestStrictDeltaRoundtripAcrossBlocks(t *testing.T) {
	docs := []uint32{0, 1, 2, 3, 10, 11, 1000, 1001, 70000, math.MaxUint32}
	enc := make([]uint32, len(docs))
	next, err := DeltaEncodeStrict(enc[:4], docs[:4], 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = DeltaEncodeStrict(enc[4:], docs[4:], next); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(enc[:4], []uint32{0, 0, 0, 0}) {
		t.Fatalf("dense run should encode as zeros, got %v", enc[:4])
	}
	dec := make([]uint32, len(docs))
	next, err = DeltaDecodeStrict(dec[:4], enc[:4], 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = DeltaDecodeStrict(dec[4:], enc[4:], next); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(dec, docs) {
		t.Fatalf("got %v want %v", dec, docs)
	}
	if _, err := DeltaEncodeStrict(make([]uint32, 2), []uint32{3, 3}, 0); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("duplicate: %v", err)
	}
	if _, err := DeltaDecodeStrict(make([]uint32, 2), []uint32{math.MaxUint32, 0}, 0); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("overflow: %v", err)
	}
}

// TestStrictDeltaRejectsInvalidNext is a regression test for uint64 wrap of
// the exported next argument: next+gap and v-next must never be computed
// with next > 2^32.
func TestStrictDeltaRejectsInvalidNext(t *testing.T) {
	buf := make([]uint32, 2)
	for _, next := range []uint64{math.MaxUint64, math.MaxUint64 - 1, MaxStrictNext + 1, 1 << 40} {
		if _, err := DeltaEncodeStrict(buf, []uint32{1, 2}, next); !errors.Is(err, ErrInvalidArgument) {
			t.Errorf("encode next=%d: err %v, want ErrInvalidArgument", next, err)
		}
		if _, err := DeltaDecodeStrict(buf, []uint32{0, 0}, next); !errors.Is(err, ErrInvalidArgument) {
			t.Errorf("decode next=%d: err %v, want ErrInvalidArgument", next, err)
		}
		// Rejected even for empty input, before any arithmetic.
		if _, err := DeltaDecodeStrict(nil, nil, next); !errors.Is(err, ErrInvalidArgument) {
			t.Errorf("decode empty next=%d: err %v", next, err)
		}
	}
	// next == 2^32 is the valid "after MaxUint32" state: only empty input fits.
	if got, err := DeltaEncodeStrict(nil, nil, MaxStrictNext); err != nil || got != MaxStrictNext {
		t.Errorf("encode empty at 2^32: %d %v", got, err)
	}
	if _, err := DeltaEncodeStrict(buf, []uint32{math.MaxUint32}, MaxStrictNext); !errors.Is(err, ErrInvalidArgument) {
		t.Errorf("encode after MaxUint32: %v", err)
	}
	if _, err := DeltaDecodeStrict(buf, []uint32{0}, MaxStrictNext); !errors.Is(err, ErrCorrupt) {
		t.Errorf("decode after MaxUint32: %v", err)
	}
	// Largest valid sum: next = 2^32-1, gap 0 -> MaxUint32, returned next 2^32.
	got, err := DeltaDecodeStrict(buf, []uint32{0}, math.MaxUint32)
	if err != nil || buf[0] != math.MaxUint32 || got != MaxStrictNext {
		t.Errorf("decode at MaxUint32: %d %d %v", buf[0], got, err)
	}
	// Largest gap from 0 with the largest base that still fits.
	if _, err := DeltaDecodeStrict(buf, []uint32{math.MaxUint32}, 1); !errors.Is(err, ErrCorrupt) {
		t.Errorf("decode gap overflow: %v", err)
	}
}

func pforRoundtrip(t *testing.T, vals []uint32) []byte {
	t.Helper()
	enc, err := AppendPFOR([]byte{0xAA}, vals) // non-empty prefix: must append
	if err != nil {
		t.Fatal(err)
	}
	if enc[0] != 0xAA {
		t.Fatal("AppendPFOR clobbered prefix")
	}
	enc = enc[1:]
	dst := make([]uint32, BlockSize)
	n, used, err := DecodePFOR(dst, append(slices.Clone(enc), 0xFF, 0xFF)) // trailing bytes ignored
	if err != nil {
		t.Fatal(err)
	}
	if n != len(vals) || used != len(enc) || !slices.Equal(dst[:n], vals) {
		t.Fatalf("roundtrip mismatch: n=%d used=%d/%d\n got %v\nwant %v", n, used, len(enc), dst[:n], vals)
	}
	return enc
}

func TestPFORRoundtripWithExceptions(t *testing.T) {
	// 124 small values and 4 huge outliers: optimal width is small and the
	// outliers must be stored as patched exceptions.
	vals := make([]uint32, BlockSize)
	for i := range vals {
		vals[i] = uint32(i % 7)
	}
	vals[3], vals[50], vals[99], vals[127] = 1<<20, math.MaxUint32, 1<<31, 12345
	enc := pforRoundtrip(t, vals)
	width, exc := enc[1], enc[2]
	if width != 3 || exc != 4 {
		t.Fatalf("width=%d exceptions=%d, want 3 and 4", width, exc)
	}
	if bitpacked := packedLen(BlockSize, 32); len(enc) >= bitpacked/4 {
		t.Fatalf("encoded %d bytes; plain FOR at 32 bits would be %d", len(enc), bitpacked)
	}
}

func TestPFORRoundtripEdgeCases(t *testing.T) {
	cases := map[string][]uint32{
		"single-zero":   {0},
		"all-zero":      make([]uint32, BlockSize),
		"single-max":    {math.MaxUint32},
		"all-max":       slices.Repeat([]uint32{math.MaxUint32}, BlockSize),
		"odd-length":    {1, 2, 3, 4, 5, 6, 7},
		"powers-of-two": {1, 2, 4, 8, 16, 32, 64, 128, 256, 1 << 16, 1 << 24, 1 << 31},
	}
	for name, vals := range cases {
		t.Run(name, func(t *testing.T) { pforRoundtrip(t, vals) })
	}
	if enc := pforRoundtrip(t, make([]uint32, BlockSize)); len(enc) != pforHeaderSize {
		t.Fatalf("all-zero block should be header only, got %d bytes", len(enc))
	}
	for _, n := range []int{0, BlockSize + 1} {
		if _, err := AppendPFOR(nil, make([]uint32, n)); !errors.Is(err, ErrInvalidArgument) {
			t.Fatalf("n=%d: %v", n, err)
		}
	}
}

func TestPFORRandomRoundtrip(t *testing.T) {
	r := rand.New(rand.NewPCG(7, 7))
	for range 2000 {
		n := 1 + r.IntN(BlockSize)
		vals := make([]uint32, n)
		base := uint(r.IntN(33))
		for i := range vals {
			if base > 0 {
				vals[i] = uint32(r.Uint64() & (1<<base - 1))
			}
			if r.IntN(16) == 0 {
				vals[i] = r.Uint32() // outlier
			}
		}
		pforRoundtrip(t, vals)
	}
}

func TestPFORDecodeRejectsCorruptBlocks(t *testing.T) {
	// {1,2,3,1<<20}: width 2 (one packed byte) plus one exception at
	// position 3 with high = (1<<20)>>2.
	valid, _ := AppendPFOR(nil, []uint32{1, 2, 3, 1 << 20})
	width := uint(valid[1])
	excAt := pforHeaderSize + packedLen(4, width) // offset of first exception
	if width != 2 || valid[2] != 1 || valid[excAt] != 3 {
		t.Fatalf("unexpected fixture block layout %v", valid)
	}
	mut := func(f func(b []byte)) []byte { b := slices.Clone(valid); f(b); return b }
	withExc := func(e ...byte) []byte { return append(slices.Clone(valid[:excAt]), e...) }
	padded, _ := AppendPFOR(nil, []uint32{1, 2, 3}) // 6 bits used of 1 byte
	padded[pforHeaderSize] |= 0x80
	cases := map[string][]byte{
		"empty":             nil,
		"short-header":      valid[:2],
		"n-zero":            mut(func(b []byte) { b[0] = 0 }),
		"n-too-big":         mut(func(b []byte) { b[0] = BlockSize + 1 }),
		"width-33":          mut(func(b []byte) { b[1] = 33 }),
		"exceptions>n":      mut(func(b []byte) { b[2] = 5 }),
		"packed-truncated":  valid[:pforHeaderSize],
		"exc-missing":       valid[:excAt],
		"exc-truncated":     valid[:len(valid)-1],
		"exc-pos-range":     mut(func(b []byte) { b[excAt] = 4 }),
		"exc-zero-high":     withExc(3, 0),
		"exc-high-overflow": withExc(3, 0xff, 0xff, 0xff, 0xff, 0x0f), // 2^35-1 > 2^30-1
		"exc-bad-uvarint":   withExc(3, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x01),
		"padding-bits":      padded,
		"exc-width-32":      {1, 32, 1, 0, 0, 0, 0, 0, 1},
		"exc-dup-pos":       {4, 0, 2, 0, 5, 0, 5}, // width 0, two exceptions at position 0
		"exc-unsorted-pos":  {4, 0, 2, 2, 5, 1, 5}, // positions 2 then 1
	}
	dst := make([]uint32, BlockSize)
	for name, b := range cases {
		if _, _, err := DecodePFOR(dst, b); !errors.Is(err, ErrCorrupt) {
			t.Errorf("%s: err %v, want ErrCorrupt", name, err)
		}
	}
	if _, _, err := DecodePFOR(make([]uint32, 3), valid); !errors.Is(err, ErrInvalidArgument) {
		t.Errorf("short dst: %v", err)
	}
}

func TestPostingsRoundtripAndValidation(t *testing.T) {
	r := rand.New(rand.NewPCG(3, 4))
	var docs, tfs []uint32
	d := uint32(0)
	for range 1000 {
		d += 1 + uint32(r.IntN(50))
		docs = append(docs, d)
		tf := uint32(1 + r.IntN(3))
		if r.IntN(100) == 0 {
			tf = 5000
		}
		tfs = append(tfs, tf)
	}
	var scratch [BlockSize]uint32
	enc, err := appendPostings(nil, docs, tfs, &scratch)
	if err != nil {
		t.Fatal(err)
	}
	var it postingIter
	it.reset(enc, uint32(len(docs)), d+1)
	var gotDocs, gotTfs []uint32
	for {
		n, err := it.nextBlock()
		if err != nil {
			t.Fatal(err)
		}
		if n == 0 {
			break
		}
		gotDocs = append(gotDocs, it.docs[:n]...)
		gotTfs = append(gotTfs, it.tfs[:n]...)
	}
	if !slices.Equal(gotDocs, docs) || !slices.Equal(gotTfs, tfs) {
		t.Fatal("posting roundtrip mismatch")
	}
	// Wrong df, doc limit, and trailing bytes are all detected.
	for name, setup := range map[string]func(){
		"df-too-small": func() { it.reset(enc, uint32(len(docs)-1), d+1) },
		"df-too-large": func() { it.reset(enc, uint32(len(docs)+1), d+1) },
		"doc-limit":    func() { it.reset(enc, uint32(len(docs)), d) },
		"trailing":     func() { it.reset(append(slices.Clone(enc), 0), uint32(len(docs)), d+1) },
	} {
		setup()
		var err error
		for n := 1; n > 0 && err == nil; n, err = it.nextBlock() {
		}
		if !errors.Is(err, ErrCorrupt) {
			t.Errorf("%s: err %v, want ErrCorrupt", name, err)
		}
	}
	if _, err := appendPostings(nil, []uint32{2, 1}, []uint32{1, 1}, &scratch); !errors.Is(err, ErrInvalidArgument) {
		t.Errorf("unsorted docs: %v", err)
	}
	if _, err := appendPostings(nil, []uint32{1}, []uint32{0}, &scratch); !errors.Is(err, ErrInvalidArgument) {
		t.Errorf("zero tf: %v", err)
	}
}

// FuzzDecodePFOR checks that arbitrary bytes never panic the decoder and
// that anything it accepts re-encodes to the same values.
func FuzzDecodePFOR(f *testing.F) {
	seed, _ := AppendPFOR(nil, []uint32{1, 2, 3, 1 << 20, 0, 7})
	f.Add(seed)
	f.Add([]byte{1, 0, 0})
	f.Add([]byte{128, 32, 0})
	f.Fuzz(func(t *testing.T, b []byte) {
		dst := make([]uint32, BlockSize)
		n, used, err := DecodePFOR(dst, b)
		if err != nil {
			if !errors.Is(err, ErrCorrupt) {
				t.Fatalf("unexpected error class: %v", err)
			}
			return
		}
		if used > len(b) || n < 1 || n > BlockSize {
			t.Fatalf("n=%d used=%d len=%d", n, used, len(b))
		}
		vals := slices.Clone(dst[:n])
		enc, err := AppendPFOR(nil, vals)
		if err != nil {
			t.Fatal(err)
		}
		n2, _, err := DecodePFOR(dst, enc)
		if err != nil || n2 != n || !slices.Equal(dst[:n], vals) {
			t.Fatalf("re-encode mismatch: %v", err)
		}
	})
}

// FuzzPFORRoundtrip encodes arbitrary uint32 blocks.
func FuzzPFORRoundtrip(f *testing.F) {
	f.Add([]byte{0, 0, 0, 1, 255, 255, 255, 255})
	f.Fuzz(func(t *testing.T, b []byte) {
		n := min(len(b)/4, BlockSize)
		if n == 0 {
			return
		}
		vals := make([]uint32, n)
		for i := range vals {
			vals[i] = binary.LittleEndian.Uint32(b[4*i:])
		}
		pforRoundtrip(t, vals)
	})
}
