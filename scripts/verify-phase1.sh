#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export GOCACHE="${GOCACHE:-${TMPDIR:-/tmp}/finfilings-go-cache}"
export CCACHE_DIR="${TMPDIR:-/tmp}/finfilings-ccache"
export CCACHE_TEMPDIR="${TMPDIR:-/tmp}/finfilings-ccache-tmp"
mkdir -p "$CCACHE_DIR" "$CCACHE_TEMPDIR"
report_dir=docs/build/codex-verification
mkdir -p "$report_dir"
{
 TZ=America/Phoenix date --iso-8601=seconds
 go version
 uname -sm
 go env GOOS GOARCH GOAMD64
 python3 - <<'PY'
from pathlib import Path
p=Path('/proc/cpuinfo')
if p.exists():
 for line in p.read_text().splitlines():
  if line.startswith('model name'):
   print(line)
   break
PY
} > "$report_dir/environment.txt"
go test -count=1 ./... > "$report_dir/unit-tests.txt" 2>&1
printf 'Unit tests passed\n'
go test -race -count=1 ./... > "$report_dir/race-tests.txt" 2>&1
printf 'Race tests passed\n'
go test -tags=purego -count=1 ./... > "$report_dir/purego-tests.txt" 2>&1
printf 'Portable scalar tests passed\n'
go vet ./... > "$report_dir/vet.txt" 2>&1
printf 'go vet passed\n'
FINFILINGS_MEMORY_PROFILE=1 go test -count=1 -run '^TestFixtureMemoryFootprint$' -v ./pkg/index > "$report_dir/fixture-memory.txt" 2>&1
printf 'Fixture memory diagnostic completed\n'
go test -c -o "${TMPDIR:-/tmp}/finfilings-phase1-verify.test" ./pkg/index
go tool objdump -s 'index\.(un)?pack(8|16)SSE2' "${TMPDIR:-/tmp}/finfilings-phase1-verify.test" > "$report_dir/simd-disassembly.txt"
printf 'SIMD disassembly saved; results in %s\n' "$report_dir"
python3 - <<'PY'
import hashlib
import json
from pathlib import Path
paths = sorted([Path('go.mod'), *Path('pkg').rglob('*.go'), *Path('pkg').rglob('*.s')])
manifest = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
Path('docs/build/codex-verification/source-sha256.json').write_text(json.dumps(manifest, indent=2) + '\n')
PY
