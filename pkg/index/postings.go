package index

import (
	"fmt"
	"math"
)

// Posting list wire format. A list with df postings is a sequence of
// ceil(df/BlockSize) block pairs. Pair i covers postings
// [i*BlockSize, min(df, (i+1)*BlockSize)) and is
//
//	PFOR(doc gaps)   DeltaEncodeStrict of segment-local doc IDs, continuing
//	                 the running base across blocks (first base is 0)
//	PFOR(tf - 1)     term frequencies minus one (tf >= 1)
//
// Both blocks of a pair must declare the same n, equal to the expected
// number of postings for that pair. The list must consume its byte range
// exactly.

// appendPostings encodes a posting list. docs must be strictly increasing,
// tfs must be >= 1, and len(docs) == len(tfs). scratch is caller-owned.
func appendPostings(dst []byte, docs, tfs []uint32, scratch *[BlockSize]uint32) ([]byte, error) {
	if len(docs) != len(tfs) || len(docs) == 0 {
		return dst, fmt.Errorf("%w: posting docs/tfs lengths %d/%d", ErrInvalidArgument, len(docs), len(tfs))
	}
	var next uint64
	var err error
	for start := 0; start < len(docs); start += BlockSize {
		end := min(start+BlockSize, len(docs))
		blk := scratch[:end-start]
		if next, err = DeltaEncodeStrict(blk, docs[start:end], next); err != nil {
			return dst, err
		}
		if dst, err = AppendPFOR(dst, blk); err != nil {
			return dst, err
		}
		for i, tf := range tfs[start:end] {
			if tf == 0 {
				return dst, fmt.Errorf("%w: zero term frequency", ErrInvalidArgument)
			}
			blk[i] = tf - 1
		}
		if dst, err = AppendPFOR(dst, blk); err != nil {
			return dst, err
		}
	}
	return dst, nil
}

// postingIter decodes a posting list one block at a time into fixed-size
// arrays, so iteration cost in memory is constant regardless of df.
type postingIter struct {
	src       []byte
	remaining int    // postings not yet decoded
	next      uint64 // strict-delta base for the next block
	docLimit  uint32 // doc IDs must be < docLimit
	docs      [BlockSize]uint32
	tfs       [BlockSize]uint32
}

func (it *postingIter) reset(src []byte, df uint32, docLimit uint32) {
	it.src = src
	it.remaining = int(df)
	it.next = 0
	it.docLimit = docLimit
}

// nextBlock decodes the next block into it.docs/it.tfs and returns its
// length. It returns 0, nil when the list is exhausted.
func (it *postingIter) nextBlock() (int, error) {
	if it.remaining == 0 {
		if len(it.src) != 0 {
			return 0, fmt.Errorf("%w: %d trailing bytes after posting list", ErrCorrupt, len(it.src))
		}
		return 0, nil
	}
	want := min(it.remaining, BlockSize)
	n, used, err := DecodePFOR(it.docs[:], it.src)
	if err != nil {
		return 0, err
	}
	if n != want {
		return 0, fmt.Errorf("%w: posting doc block has %d values, want %d", ErrCorrupt, n, want)
	}
	it.src = it.src[used:]
	if it.next, err = DeltaDecodeStrict(it.docs[:n], it.docs[:n], it.next); err != nil {
		return 0, err
	}
	if it.docs[n-1] >= it.docLimit {
		return 0, fmt.Errorf("%w: posting doc ID %d >= doc count %d", ErrCorrupt, it.docs[n-1], it.docLimit)
	}
	m, used, err := DecodePFOR(it.tfs[:], it.src)
	if err != nil {
		return 0, err
	}
	if m != n {
		return 0, fmt.Errorf("%w: posting tf block has %d values, want %d", ErrCorrupt, m, n)
	}
	it.src = it.src[used:]
	for i := range n {
		if it.tfs[i] == math.MaxUint32 {
			return 0, fmt.Errorf("%w: term frequency overflow", ErrCorrupt)
		}
		it.tfs[i]++
	}
	it.remaining -= n
	return n, nil
}
