package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

// All servers are local httptest fakes: no request leaves the machine. The "agent" is a
// throwaway fixture identity, not a real contact.
const fixtureAgent = "FixtureTest fixture@example.org"

type fakeSEC struct {
	srv  *httptest.Server
	hits atomic.Int64
	body []byte
}

func newFakeSEC(t *testing.T, body string) *fakeSEC {
	t.Helper()
	f := &fakeSEC{body: []byte(body)}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.hits.Add(1)
		if r.URL.Path != "/Archives/edgar/data/"+msCIK+"/"+msAccession+"/"+msDocument {
			http.NotFound(w, r)
			return
		}
		w.Write(f.body)
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func configFor(t *testing.T, f *fakeSEC, allowDrift bool, recorded string) fetchConfig {
	t.Helper()
	t.Setenv("SEC_USER_AGENT", fixtureAgent)
	sum := sha256.Sum256([]byte(recorded))
	cfg := defaultFetchConfig(filepath.Join(t.TempDir(), "fixture"), 1, allowDrift)
	cfg.archivesBaseURL, cfg.expectBytes, cfg.expectSHA256 = f.srv.URL, int64(len(recorded)), hex.EncodeToString(sum[:])
	return cfg
}

func entries(t *testing.T, dir string) []string {
	t.Helper()
	list, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, e := range list {
		names = append(names, e.Name())
	}
	return names
}

func TestFetchPublishesRecordedDocumentOnceAndNeverOverwrites(t *testing.T) {
	f := newFakeSEC(t, "recorded filing bytes")
	cfg := configFor(t, f, false, "recorded filing bytes")
	out, err := fetchFixture(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	if out["matches_recorded_filing"] != true || strings.Contains(out["contact_header"].(string), "example.org") {
		t.Fatalf("%v", out)
	}
	if got := entries(t, cfg.dir); len(got) != 1 || got[0] != msDocument {
		t.Fatalf("directory = %v (partial file left behind?)", got)
	}
	data, err := os.ReadFile(filepath.Join(cfg.dir, msDocument))
	if err != nil || string(data) != "recorded filing bytes" {
		t.Fatal(err, string(data))
	}
	hits := f.hits.Load()
	if _, err := fetchFixture(context.Background(), cfg); err == nil || !strings.Contains(err.Error(), "already exists") {
		t.Fatalf("second fetch was not refused: %v", err)
	}
	if f.hits.Load() != hits {
		t.Fatal("a refused fetch still contacted the server")
	}
}

func TestFetchMismatchPublishesNothingWithoutAllowDrift(t *testing.T) {
	f := newFakeSEC(t, "different bytes")
	cfg := configFor(t, f, false, "recorded filing bytes")
	if _, err := fetchFixture(context.Background(), cfg); err == nil || !strings.Contains(err.Error(), "does not match the recorded filing") {
		t.Fatalf("%v", err)
	}
	if got := entries(t, cfg.dir); len(got) != 0 {
		t.Fatalf("files left behind: %v", got)
	}
}

func TestDriftFixtureIsLabelledAndCannotBeOverwritten(t *testing.T) {
	f := newFakeSEC(t, "different bytes")
	cfg := configFor(t, f, true, "recorded filing bytes")
	out, err := fetchFixture(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	if out["matches_recorded_filing"] != false || filepath.Base(out["path"].(string)) != driftName {
		t.Fatalf("%v", out)
	}
	if got := entries(t, cfg.dir); len(got) != 1 || got[0] != driftName {
		t.Fatalf("directory = %v", got)
	}
	// A second allow-drift fetch -- even of different bytes -- must not replace the earlier drift fixture.
	hits := f.hits.Load()
	f.body = []byte("yet other bytes")
	if _, err := fetchFixture(context.Background(), cfg); err == nil || !strings.Contains(err.Error(), "already exists") {
		t.Fatalf("existing drift fixture was not protected: %v", err)
	}
	data, _ := os.ReadFile(filepath.Join(cfg.dir, driftName))
	if string(data) != "different bytes" || f.hits.Load() != hits {
		t.Fatalf("drift fixture changed or server contacted: %q hits %d->%d", data, hits, f.hits.Load())
	}
}

func TestPreexistingDriftFileBlocksEvenAMatchingFetch(t *testing.T) {
	f := newFakeSEC(t, "recorded filing bytes")
	cfg := configFor(t, f, false, "recorded filing bytes")
	if err := os.MkdirAll(cfg.dir, 0o755); err != nil {
		t.Fatal(err)
	}
	drift := filepath.Join(cfg.dir, driftName)
	if err := os.WriteFile(drift, []byte("earlier developer evidence"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := fetchFixture(context.Background(), cfg); err == nil || !strings.Contains(err.Error(), "already exists") {
		t.Fatalf("%v", err)
	}
	if f.hits.Load() != 0 {
		t.Fatal("request made although an output name was taken")
	}
	if data, _ := os.ReadFile(drift); string(data) != "earlier developer evidence" {
		t.Fatal("evidence overwritten")
	}
}

func TestPublishExclusiveRefusesOverwriteAndRemovesPartialOnFailure(t *testing.T) {
	dir := t.TempDir()
	src, err := os.CreateTemp(dir, "src")
	if err != nil {
		t.Fatal(err)
	}
	defer src.Close()
	if _, err := src.WriteString("payload"); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte("payload"))
	want := hex.EncodeToString(sum[:])
	existing := filepath.Join(dir, "existing")
	if err := os.WriteFile(existing, []byte("keep"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := publishExclusive(src, existing, 7, want); err == nil {
		t.Fatal("overwrote an existing file")
	}
	if data, _ := os.ReadFile(existing); string(data) != "keep" {
		t.Fatal("existing file modified")
	}
	bad := filepath.Join(dir, "bad")
	if err := publishExclusive(src, bad, 7, strings.Repeat("0", 64)); err == nil {
		t.Fatal("hash mismatch accepted")
	}
	if _, err := os.Lstat(bad); !os.IsNotExist(err) {
		t.Fatal("partial published file was not removed")
	}
	good := filepath.Join(dir, "good")
	if err := publishExclusive(src, good, 7, want); err != nil {
		t.Fatal(err)
	}
}

func TestFetchRefusesWithoutIdentityOrWithExcessiveRateAndRedactsErrors(t *testing.T) {
	f := newFakeSEC(t, "x")
	cfg := configFor(t, f, false, "x")
	t.Setenv("SEC_USER_AGENT", "")
	if _, err := fetchFixture(context.Background(), cfg); err == nil || f.hits.Load() != 0 {
		t.Fatal("fetched without SEC_USER_AGENT")
	}
	t.Setenv("SEC_USER_AGENT", fixtureAgent)
	cfg.rate = 5
	if _, err := fetchFixture(context.Background(), cfg); err == nil || f.hits.Load() != 0 {
		t.Fatal("rate above 1 request/second accepted")
	}
	cfg.rate, cfg.archivesBaseURL = 1, "http://127.0.0.1:1" // closed local port: a transport error
	_, err := fetchFixture(context.Background(), cfg)
	if err == nil || strings.Contains(err.Error(), fixtureAgent) || strings.Contains(err.Error(), "example.org") {
		t.Fatalf("error missing or leaks the contact: %v", err)
	}
	if got := entries(t, cfg.dir); len(got) != 0 {
		t.Fatalf("files left behind after failure: %v", got)
	}
}

func TestOnlyTheRecordedFilingCanBeRequested(t *testing.T) {
	ref := msFiling()
	ref.CIK = "1" // msFiling returns a value: callers cannot mutate package state
	if msFiling().CIK != msCIK {
		t.Fatal("msFiling is backed by mutable package state")
	}
}
