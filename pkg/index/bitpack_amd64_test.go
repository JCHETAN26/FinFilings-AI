//go:build amd64 && !purego

package index

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"
)

// TestSSE2KernelsDirect calls each assembly kernel directly on exact
// multiples of its group size and compares with the independent reference.
func TestSSE2KernelsDirect(t *testing.T) {
	if simdGroup(8) != 16 || simdGroup(16) != 8 {
		t.Fatalf("SIMD dispatch not active: groups %d/%d", simdGroup(8), simdGroup(16))
	}
	for _, width := range []uint{8, 16} {
		g := simdGroup(width)
		for n := g; n <= 512; n += g {
			vals := make([]uint32, n)
			for i := range vals {
				vals[i] = uint32(i)*2654435761 ^ 0xA5A5A5A5 // high bits set: kernels must mask
			}
			want := refPack(vals, width)
			got := make([]byte, len(want))
			simdPack(got, vals, width)
			if !bytes.Equal(got, want) {
				t.Fatalf("width %d n %d: pack kernel mismatch", width, n)
			}
			dec := make([]uint32, n)
			simdUnpack(dec, want, width)
			if !slices.Equal(dec, refUnpack(want, n, width)) {
				t.Fatalf("width %d n %d: unpack kernel mismatch", width, n)
			}
		}
	}
}

// TestSSE2KernelsDisassembly builds this package's test binary with
// `go test -c` (the binary `go test` runs is linked with -s and has no
// symbol table), disassembles it with `go tool objdump`, and requires the
// SSE2 instructions each kernel is written with, so a build that silently
// lost the kernels (or replaced them with scalar code) fails.
func TestSSE2KernelsDisassembly(t *testing.T) {
	if testing.Short() {
		t.Skip("disassembly check skipped in -short mode")
	}
	goTool, err := exec.LookPath("go")
	if err != nil {
		t.Skip("go tool not on PATH")
	}
	bin := filepath.Join(t.TempDir(), "index.test")
	build := exec.Command(goTool, "test", "-c", "-o", bin, ".")
	build.Env = append(os.Environ(), "GOFLAGS=")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("go test -c: %v\n%s", err, out)
	}
	out, err := exec.Command(goTool, "tool", "objdump", "-s", `index\.(un)?pack(8|16)SSE2`, bin).CombinedOutput()
	if err != nil {
		t.Fatalf("objdump: %v\n%s", err, out)
	}
	funcs := map[string]string{}
	var cur string
	header := regexp.MustCompile(`^TEXT \S+\.((?:un)?pack(?:8|16)SSE2)\.abi0\(SB\)`)
	for _, line := range strings.Split(string(out), "\n") {
		if m := header.FindStringSubmatch(line); m != nil {
			cur = m[1]
			continue
		}
		if cur != "" {
			funcs[cur] += line + "\n"
		}
	}
	want := map[string][]string{
		"pack8SSE2":    {"MOVDQU", "PCMPEQD", "PSRLD", "PAND", "PACKSSDW", "PACKUSWB"},
		"pack16SSE2":   {"MOVDQU", "PSLLD", "PSRAD", "PACKSSDW"},
		"unpack8SSE2":  {"MOVDQU", "PXOR", "PUNPCKLBW", "PUNPCKHBW", "PUNPCKLWD", "PUNPCKHWD"},
		"unpack16SSE2": {"MOVDQU", "PXOR", "PUNPCKLWD", "PUNPCKHWD"},
	}
	for fn, insns := range want {
		body, ok := funcs[fn]
		if !ok {
			t.Fatalf("%s not found in disassembly:\n%s", fn, out)
		}
		for _, insn := range insns {
			if !strings.Contains(body, insn+" ") {
				t.Errorf("%s: instruction %s not found:\n%s", fn, insn, body)
			}
		}
	}
}
