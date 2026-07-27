package cache

import (
	"os"
	"testing"
)

func TestCacheCommitReadAndPurge(t *testing.T) {
	value, err := New(t.TempDir(), 1024)
	if err != nil {
		t.Fatal(err)
	}
	key := Key("cw_test", "/asset")
	writer, err := value.Begin(key, 200, []Header{{"Content-Type", "application/octet-stream"}})
	if err != nil {
		t.Fatal(err)
	}
	if err := writer.Write([]byte("signed-update")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Commit(); err != nil {
		t.Fatal(err)
	}
	entry, found := value.Get(key)
	if !found || entry.Metadata.Size != int64(len("signed-update")) {
		t.Fatalf("unexpected cache entry: found=%v entry=%#v", found, entry)
	}
	body, err := os.ReadFile(entry.Path)
	if err != nil || string(body) != "signed-update" {
		t.Fatalf("unexpected cache body: %q err=%v", body, err)
	}
	if err := value.Purge(); err != nil {
		t.Fatal(err)
	}
	if _, found := value.Get(key); found {
		t.Fatal("cache entry survived purge")
	}
}

func TestCacheRejectsSameSizeCorruption(t *testing.T) {
	value, err := New(t.TempDir(), 1024)
	if err != nil {
		t.Fatal(err)
	}
	key := Key("cw_test", "/asset")
	writer, err := value.Begin(key, 200, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := writer.Write([]byte("signed-update")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Commit(); err != nil {
		t.Fatal(err)
	}
	entry, found := value.Get(key)
	if !found {
		t.Fatal("committed cache entry missing")
	}
	if err := os.WriteFile(entry.Path, []byte("tampered-data"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, found := value.Get(key); found {
		t.Fatal("cache accepted a same-size corrupted body")
	}
}
