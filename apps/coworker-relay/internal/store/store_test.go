package store

import (
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func openTestStore(t *testing.T) *Store {
	t.Helper()
	database, err := Open(filepath.Join(t.TempDir(), "relay.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = database.Close() })
	return database
}

func TestPairingIsSingleUseUnderConcurrency(t *testing.T) {
	database := openTestStore(t)
	instance, code, err := database.CreateInstance("home")
	if err != nil {
		t.Fatal(err)
	}
	pairingID := code[len("pair_"):]
	for index, value := range pairingID {
		if value == '.' {
			pairingID = pairingID[:index]
			break
		}
	}
	var wait sync.WaitGroup
	var successes int
	var lock sync.Mutex
	for range 8 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			if _, err := database.CompletePairing(pairingID, "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"); err == nil {
				lock.Lock()
				successes++
				lock.Unlock()
			}
		}()
	}
	wait.Wait()
	if successes != 1 {
		t.Fatalf("pairing succeeded %d times", successes)
	}
	stored, err := database.GetInstance(instance.ID)
	if err != nil || stored.InstancePublicKey == "" {
		t.Fatalf("instance key was not persisted: %#v %v", stored, err)
	}
}

func TestAuthEpochOnlyIncreases(t *testing.T) {
	database := openTestStore(t)
	instance, _, err := database.CreateInstance("home")
	if err != nil {
		t.Fatal(err)
	}
	if err := database.UpdateAuthKey(instance.ID, "key-one", 1); err != nil {
		t.Fatal(err)
	}
	if err := database.UpdateAuthKey(instance.ID, "key-two", 1); err == nil {
		t.Fatal("reused authentication epoch was accepted")
	}
	stored, _ := database.GetInstance(instance.ID)
	if stored.AuthPublicKey != "key-one" || stored.AuthEpoch != 1 {
		t.Fatalf("unexpected instance: %#v", stored)
	}
}

func TestFifthFailureCreatesPersistentBan(t *testing.T) {
	database := openTestStore(t)
	now := time.Now().UTC()
	for attempt := 1; attempt <= 5; attempt++ {
		banned, _, err := database.RecordFailure("cw_abcdefgh", "203.0.113.7", now)
		if err != nil {
			t.Fatal(err)
		}
		if banned != (attempt == 5) {
			t.Fatalf("attempt %d banned=%v", attempt, banned)
		}
	}
	_, active, err := database.ActiveBan("cw_abcdefgh", "203.0.113.7", now)
	if err != nil || !active {
		t.Fatalf("ban missing: active=%v err=%v", active, err)
	}
	if err := database.RemoveBan("cw_abcdefgh", "203.0.113.7"); err != nil {
		t.Fatal(err)
	}
	_, active, _ = database.ActiveBan("cw_abcdefgh", "203.0.113.7", now)
	if active {
		t.Fatal("ban was not removed")
	}
}

func TestTrafficAggregatesAndAuditPersist(t *testing.T) {
	database := openTestStore(t)
	if err := database.AddTraffic("cw_abcdefgh", 1, 3, 128); err != nil {
		t.Fatal(err)
	}
	if err := database.AddTraffic("cw_abcdefgh", 2, 4, 256); err != nil {
		t.Fatal(err)
	}
	total, err := database.TrafficTotals()
	if err != nil {
		t.Fatal(err)
	}
	if total.Connections != 3 || total.Frames != 7 || total.Bytes != 384 {
		t.Fatalf("unexpected traffic aggregate: %#v", total)
	}
	if err := database.RecordAudit(AuditEvent{
		Action: "ban.remove", InstanceID: "cw_abcdefgh",
		SourceIP: "203.0.113.7", Reason: "false positive",
	}); err != nil {
		t.Fatal(err)
	}
}
