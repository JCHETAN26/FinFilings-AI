package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/ingest"
)

// The only filing this tool can fetch: Morgan Stanley's FY2025 10-K, the sole
// titled-heading filing in the expanded corpus. Identity and the recorded bytes come
// from indexes/sec-scale-v2-20261031/report.json. There is deliberately no flag to
// choose another filing: this is a single-document replay fixture, not an ingester.
const (
	msCIK       = "895421"
	msTicker    = "MS"
	msAccession = "000089542126000086"
	msDocument  = "ms-20251231.htm"
	// Recorded in the ingestion report. NOTE: SEC currently serves different bytes
	// (see bench_kit/results/parser-boundaries-20261101/RESULTS.md), so these are the
	// expectation to compare against, never a claim about any local file.
	msBytes  = 9714832
	msSHA256 = "57698077e7a8e943c9b7dbb7521fdb583101cc23579a6f271b9b06d5d61112df"
	// A defensive ceiling (bytes) about 1.6x the expected document.
	fetchMaxBytes = 16 << 20
	// Name under which bytes that do not match the recorded filing are kept.
	driftName = "ms-20251231.sec-current-NOT-CORPUS-BYTES.htm"
)

// msFiling returns the filing reference by value (no mutable package state).
func msFiling() ingest.FilingRef {
	return ingest.FilingRef{CIK: msCIK, Ticker: msTicker, Accession: msAccession, PrimaryDocument: msDocument}
}

// fetchConfig is the injectable part of a fetch; production uses defaultFetchConfig.
type fetchConfig struct {
	dir        string
	rate       float64
	allowDrift bool
	// archivesBaseURL is the SEC archive host; tests point it at a local httptest server.
	archivesBaseURL string
	// expectBytes/expectSHA256 identify the recorded filing.
	expectBytes  int64
	expectSHA256 string
}

func defaultFetchConfig(dir string, rate float64, allowDrift bool) fetchConfig {
	return fetchConfig{dir: dir, rate: rate, allowDrift: allowDrift, expectBytes: msBytes, expectSHA256: msSHA256}
}

// publishExclusive copies src to a NEW file at dst. It fails if dst exists (O_EXCL), never
// replaces anything, re-hashes what it wrote, and removes its own partial file on failure.
func publishExclusive(src *os.File, dst string, wantBytes int64, wantSHA string) (err error) {
	if _, err := src.Seek(0, io.SeekStart); err != nil {
		return err
	}
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o444)
	if err != nil {
		return fmt.Errorf("refusing to publish %s: %w", dst, err)
	}
	defer func() {
		if err != nil {
			err = errors.Join(err, out.Close(), os.Remove(dst))
		}
	}()
	h := sha256.New()
	n, err := io.Copy(io.MultiWriter(out, h), src)
	if err != nil {
		return err
	}
	if n != wantBytes || hex.EncodeToString(h.Sum(nil)) != wantSHA {
		return fmt.Errorf("published copy verification failed (%d bytes)", n)
	}
	if err := out.Sync(); err != nil {
		return err
	}
	return out.Close()
}

// fetchFixture downloads the Morgan Stanley filing once, with the existing paced EDGAR
// client, and publishes it only if the byte count and SHA-256 match the recorded filing --
// or, with allowDrift, under the NOT-CORPUS-BYTES name. Neither name may already exist, so
// no earlier fixture is ever replaced. The contact header is read from SEC_USER_AGENT only;
// it is never accepted as a flag (so it cannot appear in argv) and never printed or logged.
func fetchFixture(ctx context.Context, cfg fetchConfig) (map[string]any, error) {
	if cfg.rate <= 0 || cfg.rate > 1 {
		return nil, errors.New("fetch rate must be in (0, 1] request per second")
	}
	agent := os.Getenv("SEC_USER_AGENT")
	if agent == "" {
		return nil, errors.New("SEC_USER_AGENT is not set (environment only; never pass it as an argument)")
	}
	// Both possible outputs are checked BEFORE any request, so a drift fixture from an earlier
	// run cannot be silently overwritten and no SEC request is wasted.
	original, drift := filepath.Join(cfg.dir, msDocument), filepath.Join(cfg.dir, driftName)
	for _, p := range []string{original, drift} {
		if _, err := os.Lstat(p); err == nil {
			return nil, fmt.Errorf("%s already exists; refusing to re-download or overwrite", p)
		} else if !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
	}
	if err := os.MkdirAll(cfg.dir, 0o755); err != nil {
		return nil, err
	}
	opts := ingest.ClientOptions{UserAgent: agent, RequestsPerSecond: cfg.rate, Timeout: 120 * time.Second, MaxDocumentBytes: fetchMaxBytes, MaxRetries: 2}
	if cfg.archivesBaseURL != "" {
		opts.ArchivesBaseURL = cfg.archivesBaseURL
	}
	client, err := ingest.NewClient(opts)
	if err != nil {
		return nil, redact(err, agent)
	}
	tmp, err := os.CreateTemp(cfg.dir, msDocument+".partial-*")
	if err != nil {
		return nil, err
	}
	defer func() {
		if cerr := errors.Join(tmp.Close(), os.Remove(tmp.Name())); cerr != nil {
			fmt.Fprintln(os.Stderr, "parserreplay: temporary file cleanup:", cerr)
		}
	}()
	started := time.Now().UTC()
	dl, err := client.Download(ctx, msFiling(), tmp)
	if err != nil {
		return nil, redact(err, agent)
	}
	matches := dl.Bytes == cfg.expectBytes && dl.SHA256 == cfg.expectSHA256
	final := original
	if !matches {
		if !cfg.allowDrift {
			return nil, fmt.Errorf("downloaded document does not match the recorded filing: %d bytes sha256 %s (expected %d, %s); nothing published (re-run with -allow-drift to keep it as a clearly labelled UNVERIFIED current copy)", dl.Bytes, dl.SHA256, cfg.expectBytes, cfg.expectSHA256)
		}
		// Not the bytes the corpus was built from: keep it only under a name that says so.
		final = drift
	}
	if err := tmp.Sync(); err != nil {
		return nil, err
	}
	if err := publishExclusive(tmp, final, dl.Bytes, dl.SHA256); err != nil {
		return nil, err
	}
	return map[string]any{
		"url": dl.URL, "bytes": dl.Bytes, "sha256": dl.SHA256, "path": final, "requests_per_second": cfg.rate,
		"matches_recorded_filing": matches, "recorded_bytes": cfg.expectBytes, "recorded_sha256": cfg.expectSHA256,
		"started_utc": started.Format(time.RFC3339), "finished_utc": time.Now().UTC().Format(time.RFC3339),
		"contact_header": "withheld (read from SEC_USER_AGENT; not recorded)",
	}, nil
}

// redact removes the contact header from an error before it can be printed.
func redact(err error, agent string) error {
	if err == nil || agent == "" {
		return err
	}
	return errors.New(strings.ReplaceAll(err.Error(), agent, "<redacted>"))
}
