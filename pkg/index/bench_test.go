package index

import (
	"context"
	"fmt"
	"math/rand/v2"
	"path/filepath"
	"strconv"
	"testing"
)

// Benchmarks use synthetic data and the 1,000-chunk test fixtures. Their
// results describe this implementation on fixture-sized inputs only; they
// are not SEC corpus performance numbers.

func benchBlock(kind string) []uint32 {
	r := rand.New(rand.NewPCG(11, 13))
	vals := make([]uint32, BlockSize)
	for i := range vals {
		switch kind {
		case "dense": // consecutive doc IDs after strict delta
			vals[i] = 0
		case "small": // typical small gaps
			vals[i] = uint32(r.IntN(64))
		case "exceptions": // small gaps with ~5% large outliers
			vals[i] = uint32(r.IntN(16))
			if r.IntN(20) == 0 {
				vals[i] = r.Uint32() >> 4
			}
		}
	}
	return vals
}

func BenchmarkDeltaRoundtrip(b *testing.B) {
	src := make([]uint32, BlockSize)
	for i := range src {
		src[i] = uint32(i * 37)
	}
	buf := make([]uint32, BlockSize)
	b.SetBytes(4 * BlockSize)
	b.ReportAllocs()
	for b.Loop() {
		if err := DeltaEncode(buf, src); err != nil {
			b.Fatal(err)
		}
		if err := DeltaDecode(buf, buf); err != nil {
			b.Fatal(err)
		}
	}
}

// BenchmarkPFOREncode / BenchmarkPFORDecode use block kinds whose chosen
// widths are 0 (dense), 6 (small) and 4 (exceptions); none has a SIMD
// kernel, so these measure the scalar path end to end through the
// dispatcher. BenchmarkPFORDecodeWidth covers the SIMD widths.
func BenchmarkPFOREncode(b *testing.B) {
	for _, kind := range []string{"dense", "small", "exceptions"} {
		b.Run(kind, func(b *testing.B) {
			vals := benchBlock(kind)
			dst := make([]byte, 0, 1024)
			b.SetBytes(4 * BlockSize)
			b.ReportAllocs()
			for b.Loop() {
				var err error
				if dst, err = AppendPFOR(dst[:0], vals); err != nil {
					b.Fatal(err)
				}
			}
			b.ReportMetric(float64(len(dst))*8/BlockSize, "bits/int")
		})
	}
}

func BenchmarkPFORDecode(b *testing.B) {
	for _, kind := range []string{"dense", "small", "exceptions"} {
		b.Run(kind, func(b *testing.B) {
			enc, err := AppendPFOR(nil, benchBlock(kind))
			if err != nil {
				b.Fatal(err)
			}
			if simdGroup(uint(enc[1])) > 0 {
				b.Fatalf("kind %s chose SIMD width %d; this benchmark is the scalar path", kind, enc[1])
			}
			dst := make([]uint32, BlockSize)
			b.SetBytes(4 * BlockSize)
			b.ReportAllocs()
			for b.Loop() {
				if _, _, err := DecodePFOR(dst, enc); err != nil {
					b.Fatal(err)
				}
			}
			b.ReportMetric(float64(enc[1]), "width")
		})
	}
}

// BenchmarkBitPackMatched compares the dispatching packBits/unpackBits
// (SSE2 kernels for widths 8 and 16 on amd64 without purego) against
// packBitsScalar/unpackBitsScalar on the identical 128-value input. Width 12
// has no SIMD kernel and is included as a control: both variants run the
// scalar code there.
func BenchmarkBitPackMatched(b *testing.B) {
	r := rand.New(rand.NewPCG(17, 19))
	for _, width := range []uint{8, 12, 16} {
		vals := make([]uint32, BlockSize)
		for i := range vals {
			vals[i] = r.Uint32() // bits above width exercise masking
		}
		packed := packBitsScalar(nil, vals, width)
		out := make([]uint32, BlockSize)
		buf := make([]byte, 0, len(packed))
		variants := []struct {
			name   string
			pack   func()
			unpack func()
		}{
			{"dispatch", func() { buf = packBits(buf[:0], vals, width) }, func() { unpackBits(out, packed, width) }},
			{"scalar", func() { buf = packBitsScalar(buf[:0], vals, width) }, func() { unpackBitsScalar(out, packed, width) }},
		}
		for _, v := range variants {
			b.Run(fmt.Sprintf("pack/w%d/%s", width, v.name), func(b *testing.B) {
				b.SetBytes(4 * BlockSize)
				b.ReportAllocs()
				for b.Loop() {
					v.pack()
				}
			})
			b.Run(fmt.Sprintf("unpack/w%d/%s", width, v.name), func(b *testing.B) {
				b.SetBytes(4 * BlockSize)
				b.ReportAllocs()
				for b.Loop() {
					v.unpack()
				}
			})
		}
	}
}

// BenchmarkPFORDecodeWidth decodes full PFOR blocks whose chosen width is 8
// or 16 (SIMD path) or 12 (scalar path), end to end through DecodePFOR.
func BenchmarkPFORDecodeWidth(b *testing.B) {
	r := rand.New(rand.NewPCG(23, 29))
	for _, width := range []uint{8, 12, 16} {
		vals := make([]uint32, BlockSize)
		for i := range vals {
			vals[i] = 1<<(width-1) | uint32(r.IntN(1<<(width-1)))
		}
		enc, err := AppendPFOR(nil, vals)
		if err != nil || uint(enc[1]) != width {
			b.Fatalf("setup: width %d err %v", enc[1], err)
		}
		dst := make([]uint32, BlockSize)
		b.Run(fmt.Sprintf("w%d", width), func(b *testing.B) {
			b.SetBytes(4 * BlockSize)
			b.ReportAllocs()
			for b.Loop() {
				if _, _, err := DecodePFOR(dst, enc); err != nil {
					b.Fatal(err)
				}
			}
		})
	}
}

// BenchmarkPostingIter decodes a 100,000-posting list block by block.
func BenchmarkPostingIter(b *testing.B) {
	r := rand.New(rand.NewPCG(5, 5))
	const n = 100_000
	docs, tfs := make([]uint32, n), make([]uint32, n)
	d := uint32(0)
	for i := range docs {
		d += 1 + uint32(r.IntN(8))
		docs[i], tfs[i] = d, uint32(1+r.IntN(4))
	}
	var scratch [BlockSize]uint32
	enc, err := appendPostings(nil, docs, tfs, &scratch)
	if err != nil {
		b.Fatal(err)
	}
	var it postingIter
	b.SetBytes(8 * n) // raw doc+tf bytes produced per iteration
	b.ReportAllocs()
	for b.Loop() {
		it.reset(enc, n, d+1)
		for {
			m, err := it.nextBlock()
			if err != nil {
				b.Fatal(err)
			}
			if m == 0 {
				break
			}
		}
	}
	b.ReportMetric(float64(len(enc))*8/n, "bits/posting")
}

// BenchmarkIngestFixtures builds a complete index of the 1,000 fixture
// chunks per iteration, including segment fsync and manifest publish.
func BenchmarkIngestFixtures(b *testing.B) {
	g := newFixtureGen()
	var textBytes int64
	for i := range fixtureCount {
		c := g.chunk(i)
		textBytes += int64(len(c.Text))
		for _, t := range c.Tokens {
			textBytes += int64(len(t)) + 1
		}
	}
	root := b.TempDir()
	b.SetBytes(textBytes)
	b.ReportAllocs()
	i := 0
	for b.Loop() {
		i++
		buildFixtureIndex(b, filepath.Join(root, strconv.Itoa(i)), WriterOptions{}, fixtureCount)
	}
}

func BenchmarkSearchFixtures(b *testing.B) {
	ix, _ := openFixtureIndex(b, WriterOptions{}, fixtureCount)
	queries := map[string]string{
		"rare-1term":     "marker0042",
		"topic-1term":    "cybersecurity",
		"mixed-3term":    "goodwill impairment charge",
		"common-3term":   "the and of",
		"mixed-6term":    "interest rate risk hedging foreign currency",
		"unknown-1term":  "zzzzunknownterm",
		"multi-unique-2": "fin017 fin200",
	}
	for name, q := range queries {
		b.Run(name, func(b *testing.B) {
			ctx := context.Background()
			b.ReportAllocs()
			for b.Loop() {
				if _, err := ix.Search(ctx, q, 10); err != nil {
					b.Fatal(err)
				}
			}
		})
	}
}

func BenchmarkSearchFixturesParallel(b *testing.B) {
	ix, _ := openFixtureIndex(b, WriterOptions{MaxDocsPerSegment: 250}, fixtureCount)
	ctx := context.Background()
	b.ReportAllocs()
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			if _, err := ix.Search(ctx, "interest rate risk hedging", 10); err != nil {
				b.Fatal(err)
			}
		}
	})
}
