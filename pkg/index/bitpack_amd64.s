//go:build amd64 && !purego

#include "textflag.h"

// SSE2 bit-packing kernels for byte-aligned PFOR widths. Go assembler
// operand order is source, destination. Mnemonic mapping to Intel names:
// PACKSSLW=PACKSSDW, PCMPEQL=PCMPEQD, PUNPCKLWL=PUNPCKLWD,
// PUNPCKHWL=PUNPCKHWD, MOVOU=MOVDQU, MOVO=MOVDQA.

// func pack8SSE2(dst *byte, src *uint32, n int)
// 16 values per iteration: mask each dword to 0..255, then the signed
// 32->16 and unsigned 16->8 saturating packs are exact.
TEXT ·pack8SSE2(SB), NOSPLIT, $0-24
	MOVQ dst+0(FP), DI
	MOVQ src+8(FP), SI
	MOVQ n+16(FP), CX
	PCMPEQL X7, X7
	PSRLL   $24, X7          // X7 = 0x000000FF per dword

loop:
	MOVOU    0(SI), X0
	MOVOU    16(SI), X1
	MOVOU    32(SI), X2
	MOVOU    48(SI), X3
	PAND     X7, X0
	PAND     X7, X1
	PAND     X7, X2
	PAND     X7, X3
	PACKSSLW X1, X0          // X0 = words of values 0..7
	PACKSSLW X3, X2          // X2 = words of values 8..15
	PACKUSWB X2, X0          // X0 = bytes of values 0..15
	MOVOU    X0, 0(DI)
	ADDQ     $64, SI
	ADDQ     $16, DI
	SUBQ     $16, CX
	JNZ      loop
	RET

// func pack16SSE2(dst *byte, src *uint32, n int)
// 8 values per iteration: sign-extend the low 16 bits of each dword
// (shift left 16, arithmetic shift right 16) so the signed saturating
// 32->16 pack reproduces the low 16 bits exactly.
TEXT ·pack16SSE2(SB), NOSPLIT, $0-24
	MOVQ dst+0(FP), DI
	MOVQ src+8(FP), SI
	MOVQ n+16(FP), CX

loop:
	MOVOU    0(SI), X0
	MOVOU    16(SI), X1
	PSLLL    $16, X0
	PSRAL    $16, X0
	PSLLL    $16, X1
	PSRAL    $16, X1
	PACKSSLW X1, X0          // X0 = low 16 bits of values 0..7
	MOVOU    X0, 0(DI)
	ADDQ     $32, SI
	ADDQ     $16, DI
	SUBQ     $8, CX
	JNZ      loop
	RET

// func unpack8SSE2(dst *uint32, src *byte, n int)
// 16 values per iteration: zero-extend bytes to words, words to dwords.
TEXT ·unpack8SSE2(SB), NOSPLIT, $0-24
	MOVQ dst+0(FP), DI
	MOVQ src+8(FP), SI
	MOVQ n+16(FP), CX
	PXOR X7, X7

loop:
	MOVOU     0(SI), X0
	MOVO      X0, X1
	PUNPCKLBW X7, X0         // words of bytes 0..7
	PUNPCKHBW X7, X1         // words of bytes 8..15
	MOVO      X0, X2
	MOVO      X1, X3
	PUNPCKLWL X7, X0         // dwords 0..3
	PUNPCKHWL X7, X2         // dwords 4..7
	PUNPCKLWL X7, X1         // dwords 8..11
	PUNPCKHWL X7, X3         // dwords 12..15
	MOVOU     X0, 0(DI)
	MOVOU     X2, 16(DI)
	MOVOU     X1, 32(DI)
	MOVOU     X3, 48(DI)
	ADDQ      $16, SI
	ADDQ      $64, DI
	SUBQ      $16, CX
	JNZ       loop
	RET

// func unpack16SSE2(dst *uint32, src *byte, n int)
// 8 values per iteration: zero-extend words to dwords.
TEXT ·unpack16SSE2(SB), NOSPLIT, $0-24
	MOVQ dst+0(FP), DI
	MOVQ src+8(FP), SI
	MOVQ n+16(FP), CX
	PXOR X7, X7

loop:
	MOVOU     0(SI), X0
	MOVO      X0, X1
	PUNPCKLWL X7, X0         // dwords 0..3
	PUNPCKHWL X7, X1         // dwords 4..7
	MOVOU     X0, 0(DI)
	MOVOU     X1, 16(DI)
	ADDQ      $16, SI
	ADDQ      $32, DI
	SUBQ      $8, CX
	JNZ       loop
	RET
