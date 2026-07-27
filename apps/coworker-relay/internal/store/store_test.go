package store

import (
	"path/filepath"
	"testing"
	"time"
)

func openTestStore(t *testing.T) *Store {
	t.Helper()
	value, err := Open(filepath.Join(t.TempDir(), "relay.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = value.Close() })
	return value
}

func TestEnrollmentIsSingleUse(t *testing.T) {
	database := openTestStore(t)
	created, code, err := database.CreateInstance("home")
	if err != nil {
		t.Fatal(err)
	}
	enrolled, credential, err := database.Enroll(code, "$argon2id$test")
	if err != nil {
		t.Fatal(err)
	}
	if enrolled.ID != created.ID || credential == "" {
		t.Fatalf("unexpected enrollment: %#v credential=%q", enrolled, credential)
	}
	if _, _, err := database.Enroll(code, "$argon2id$test"); err == nil {
		t.Fatal("pairing code was accepted twice")
	}
	if _, err := database.AuthenticateInstance(created.ID, credential); err != nil {
		t.Fatalf("credential was not accepted: %v", err)
	}
}

func TestFailureBanPersistsAndCanBeRemoved(t *testing.T) {
	database := openTestStore(t)
	now := time.Now().UTC()
	for attempt := 1; attempt <= 5; attempt++ {
		banned, _, err := database.RecordFailure("cw_test", "203.0.113.5", now)
		if err != nil {
			t.Fatal(err)
		}
		if banned != (attempt == 5) {
			t.Fatalf("attempt %d banned=%v", attempt, banned)
		}
	}
	if _, active, err := database.ActiveBan("cw_test", "203.0.113.5", now); err != nil || !active {
		t.Fatalf("expected active ban: active=%v err=%v", active, err)
	}
	if err := database.RemoveBan("cw_test", "203.0.113.5"); err != nil {
		t.Fatal(err)
	}
	if _, active, _ := database.ActiveBan("cw_test", "203.0.113.5", now); active {
		t.Fatal("ban remained active after removal")
	}
}

func TestUpdateStatisticsSeparateAnonymousAndAuthenticatedChecks(t *testing.T) {
	database := openTestStore(t)
	instance, _, err := database.CreateInstance("home")
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	if err := database.RecordUpdateCheck(instance.ID, "0.3.4", false, now); err != nil {
		t.Fatal(err)
	}
	if err := database.RecordUpdateCheck(instance.ID, "0.3.5", true, now); err != nil {
		t.Fatal(err)
	}
	stats, err := database.UpdateStatistics(instance.ID, now)
	if err != nil {
		t.Fatal(err)
	}
	window := stats["last_7_days"].(map[string]any)
	if window["anonymous"] != uint64(1) || window["authenticated"] != uint64(1) {
		t.Fatalf("unexpected update statistics: %#v", window)
	}
	versions := window["versions"].(map[string]uint64)
	if versions["0.3.4"] != 1 || versions["0.3.5"] != 1 {
		t.Fatalf("unexpected version counts: %#v", versions)
	}
}
