package store

import (
	"bytes"
	"path/filepath"
	"testing"
	"time"

	bolt "go.etcd.io/bbolt"
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

func TestPreparedCredentialPromotesOnlyAfterSuccessfulUse(t *testing.T) {
	database := openTestStore(t)
	instance, code, err := database.CreateInstance("home")
	if err != nil {
		t.Fatal(err)
	}
	_, previous, err := database.Enroll(code, "$argon2id$test")
	if err != nil {
		t.Fatal(err)
	}
	next, err := database.PrepareCredential(instance.ID)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := database.AuthenticateInstance(instance.ID, previous); err != nil {
		t.Fatalf("current credential was invalidated before promotion: %v", err)
	}
	if _, err := database.AuthenticateInstance(instance.ID, next); err != nil {
		t.Fatalf("pending credential could not promote: %v", err)
	}
	if _, err := database.AuthenticateInstance(instance.ID, previous); err == nil {
		t.Fatal("previous credential remained valid after promotion")
	}
}

func TestDeleteInstanceRemovesRelatedSecurityState(t *testing.T) {
	database := openTestStore(t)
	instance, _, err := database.CreateInstance("home")
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	for range 5 {
		if _, _, err := database.RecordFailure(instance.ID, "203.0.113.5", now); err != nil {
			t.Fatal(err)
		}
	}
	if err := database.RecordUpdateCheck(instance.ID, "0.3.5", false, now); err != nil {
		t.Fatal(err)
	}
	if err := database.DeleteInstance(instance.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := database.GetInstance(instance.ID); err == nil {
		t.Fatal("instance survived deletion")
	}
	if bans, err := database.ListBans(instance.ID); err != nil || len(bans) != 0 {
		t.Fatalf("instance bans survived deletion: %#v %v", bans, err)
	}
	if err := database.db.View(func(tx *bolt.Tx) error {
		for _, bucketName := range [][]byte{pairingsBucket, failuresBucket, updateStatsBucket} {
			if count := tx.Bucket(bucketName).Stats().KeyN; count != 0 {
				t.Fatalf("%s retained %d keys", bucketName, count)
			}
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
}

func TestGarbageCollectionAndBackup(t *testing.T) {
	database := openTestStore(t)
	if _, _, err := database.CreateInstance("home"); err != nil {
		t.Fatal(err)
	}
	removed, err := database.GarbageCollect(time.Now().UTC().Add(11 * time.Minute))
	if err != nil || removed["pairings"] != 1 {
		t.Fatalf("unexpected GC result: %#v %v", removed, err)
	}
	var backup bytes.Buffer
	if err := database.Backup(&backup); err != nil || backup.Len() == 0 {
		t.Fatalf("backup failed: bytes=%d err=%v", backup.Len(), err)
	}
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

func TestAuthenticationBanPolicyIsConfigurable(t *testing.T) {
	database := openTestStore(t)
	if err := database.SetAuthPolicy(time.Minute, 2, 15*time.Minute); err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	if banned, _, err := database.RecordFailure("cw_test", "203.0.113.6", now); err != nil || banned {
		t.Fatalf("first attempt unexpectedly banned: %v %v", banned, err)
	}
	banned, until, err := database.RecordFailure("cw_test", "203.0.113.6", now)
	if err != nil || !banned || until.Sub(now) != 15*time.Minute {
		t.Fatalf("custom policy was not applied: %v %v %v", banned, until, err)
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
