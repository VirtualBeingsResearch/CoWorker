package store

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base32"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
	"time"

	bolt "go.etcd.io/bbolt"
)

var (
	instancesBucket   = []byte("instances")
	pairingsBucket    = []byte("pairings")
	failuresBucket    = []byte("failures")
	bansBucket        = []byte("bans")
	updateStatsBucket = []byte("update_stats")
)

type Instance struct {
	ID                  string    `json:"id"`
	Name                string    `json:"name"`
	CredentialHash      string    `json:"credential_hash"`
	Verifier            string    `json:"verifier"`
	VerifierGeneration  string    `json:"verifier_generation"`
	UpdateAuthMode      string    `json:"update_auth_mode"`
	CreatedAt           time.Time `json:"created_at"`
	LastConnectedAt     time.Time `json:"last_connected_at,omitempty"`
	LastAnonymousUpdate time.Time `json:"last_anonymous_update,omitempty"`
}

type Pairing struct {
	InstanceID string    `json:"instance_id"`
	ExpiresAt  time.Time `json:"expires_at"`
	Used       bool      `json:"used"`
}

type Ban struct {
	InstanceID string    `json:"instance_id"`
	IP         string    `json:"ip"`
	Until      time.Time `json:"until"`
	Reason     string    `json:"reason"`
	CreatedAt  time.Time `json:"created_at"`
}

type Store struct{ db *bolt.DB }

type UpdateDay struct {
	Authenticated uint64            `json:"authenticated"`
	Anonymous     uint64            `json:"anonymous"`
	Versions      map[string]uint64 `json:"versions"`
}

type UpdateStats struct {
	Days map[string]UpdateDay `json:"days"`
}

func Open(path string) (*Store, error) {
	db, err := bolt.Open(path, 0o600, &bolt.Options{Timeout: 5 * time.Second})
	if err != nil {
		return nil, err
	}
	err = db.Update(func(tx *bolt.Tx) error {
		for _, name := range [][]byte{
			instancesBucket, pairingsBucket, failuresBucket, bansBucket, updateStatsBucket,
		} {
			if _, err := tx.CreateBucketIfNotExists(name); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		_ = db.Close()
		return nil, err
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() error { return s.db.Close() }

func randomToken(bytes int) (string, error) {
	raw := make([]byte, bytes)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return strings.TrimRight(base32.StdEncoding.WithPadding(base32.NoPadding).EncodeToString(raw), "="), nil
}

func digest(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

func putJSON(bucket *bolt.Bucket, key string, value any) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return bucket.Put([]byte(key), raw)
}

func getJSON[T any](bucket *bolt.Bucket, key string) (T, error) {
	var value T
	raw := bucket.Get([]byte(key))
	if raw == nil {
		return value, errors.New("not found")
	}
	err := json.Unmarshal(raw, &value)
	return value, err
}

func (s *Store) CreateInstance(name string) (Instance, string, error) {
	idToken, err := randomToken(10)
	if err != nil {
		return Instance{}, "", err
	}
	pairingCode, err := randomToken(12)
	if err != nil {
		return Instance{}, "", err
	}
	instance := Instance{
		ID:             "cw_" + strings.ToLower(idToken),
		Name:           strings.TrimSpace(name),
		UpdateAuthMode: "optional",
		CreatedAt:      time.Now().UTC(),
	}
	pairing := Pairing{
		InstanceID: instance.ID,
		ExpiresAt:  time.Now().UTC().Add(10 * time.Minute),
	}
	err = s.db.Update(func(tx *bolt.Tx) error {
		if err := putJSON(tx.Bucket(instancesBucket), instance.ID, instance); err != nil {
			return err
		}
		return putJSON(tx.Bucket(pairingsBucket), digest(pairingCode), pairing)
	})
	return instance, pairingCode, err
}

func (s *Store) Enroll(pairingCode, verifier string) (Instance, string, error) {
	var instance Instance
	credential, err := randomToken(32)
	if err != nil {
		return instance, "", err
	}
	err = s.db.Update(func(tx *bolt.Tx) error {
		pairings := tx.Bucket(pairingsBucket)
		pairing, err := getJSON[Pairing](pairings, digest(strings.TrimSpace(pairingCode)))
		if err != nil || pairing.Used || time.Now().UTC().After(pairing.ExpiresAt) {
			return errors.New("pairing code is invalid, expired, or already used")
		}
		instance, err = getJSON[Instance](tx.Bucket(instancesBucket), pairing.InstanceID)
		if err != nil {
			return err
		}
		instance.CredentialHash = digest(credential)
		instance.Verifier = verifier
		pairing.Used = true
		if err := putJSON(tx.Bucket(instancesBucket), instance.ID, instance); err != nil {
			return err
		}
		return putJSON(pairings, digest(strings.TrimSpace(pairingCode)), pairing)
	})
	return instance, credential, err
}

func (s *Store) GetInstance(id string) (Instance, error) {
	var instance Instance
	err := s.db.View(func(tx *bolt.Tx) error {
		var err error
		instance, err = getJSON[Instance](tx.Bucket(instancesBucket), id)
		return err
	})
	return instance, err
}

func (s *Store) AuthenticateInstance(id, credential string) (Instance, error) {
	instance, err := s.GetInstance(id)
	if err != nil ||
		instance.CredentialHash == "" ||
		subtle.ConstantTimeCompare(
			[]byte(digest(credential)),
			[]byte(instance.CredentialHash),
		) != 1 {
		return Instance{}, errors.New("invalid instance credential")
	}
	return instance, nil
}

func (s *Store) UpdateVerifier(id, verifier, generation string) error {
	return s.updateInstance(id, func(instance *Instance) {
		instance.Verifier = verifier
		instance.VerifierGeneration = generation
		instance.LastConnectedAt = time.Now().UTC()
	})
}

func (s *Store) SetUpdateAuthMode(id, mode string) error {
	if mode != "optional" && mode != "required" {
		return errors.New("update auth mode must be optional or required")
	}
	return s.updateInstance(id, func(instance *Instance) { instance.UpdateAuthMode = mode })
}

func (s *Store) updateInstance(id string, mutate func(*Instance)) error {
	return s.db.Update(func(tx *bolt.Tx) error {
		bucket := tx.Bucket(instancesBucket)
		instance, err := getJSON[Instance](bucket, id)
		if err != nil {
			return err
		}
		mutate(&instance)
		return putJSON(bucket, id, instance)
	})
}

func (s *Store) DeleteInstance(id string) error {
	return s.db.Update(func(tx *bolt.Tx) error {
		return tx.Bucket(instancesBucket).Delete([]byte(id))
	})
}

func (s *Store) ListInstances() ([]Instance, error) {
	var values []Instance
	err := s.db.View(func(tx *bolt.Tx) error {
		return tx.Bucket(instancesBucket).ForEach(func(_, raw []byte) error {
			var item Instance
			if err := json.Unmarshal(raw, &item); err != nil {
				return err
			}
			values = append(values, item)
			return nil
		})
	})
	return values, err
}

func failureKey(instanceID, ip string) string { return instanceID + "\x00" + ip }

func (s *Store) RecordFailure(instanceID, ip string, now time.Time) (bool, time.Time, error) {
	key := failureKey(instanceID, ip)
	var bannedUntil time.Time
	err := s.db.Update(func(tx *bolt.Tx) error {
		failures := tx.Bucket(failuresBucket)
		var times []time.Time
		if raw := failures.Get([]byte(key)); raw != nil {
			_ = json.Unmarshal(raw, &times)
		}
		cutoff := now.Add(-10 * time.Minute)
		filtered := times[:0]
		for _, value := range times {
			if value.After(cutoff) {
				filtered = append(filtered, value)
			}
		}
		filtered = append(filtered, now)
		if len(filtered) >= 5 {
			bannedUntil = now.Add(time.Hour)
			ban := Ban{InstanceID: instanceID, IP: ip, Until: bannedUntil, Reason: "authentication failures", CreatedAt: now}
			if err := putJSON(tx.Bucket(bansBucket), key, ban); err != nil {
				return err
			}
			return failures.Delete([]byte(key))
		}
		return putJSON(failures, key, filtered)
	})
	return !bannedUntil.IsZero(), bannedUntil, err
}

func (s *Store) ActiveBan(instanceID, ip string, now time.Time) (Ban, bool, error) {
	key := failureKey(instanceID, ip)
	var ban Ban
	var active bool
	err := s.db.Update(func(tx *bolt.Tx) error {
		bans := tx.Bucket(bansBucket)
		value, err := getJSON[Ban](bans, key)
		if err != nil {
			return nil
		}
		if !value.Until.After(now) {
			return bans.Delete([]byte(key))
		}
		ban, active = value, true
		return nil
	})
	return ban, active, err
}

func (s *Store) ListBans(instanceID string) ([]Ban, error) {
	var values []Ban
	now := time.Now().UTC()
	err := s.db.View(func(tx *bolt.Tx) error {
		return tx.Bucket(bansBucket).ForEach(func(_, raw []byte) error {
			var item Ban
			if err := json.Unmarshal(raw, &item); err != nil {
				return err
			}
			if item.Until.After(now) && (instanceID == "" || item.InstanceID == instanceID) {
				values = append(values, item)
			}
			return nil
		})
	})
	return values, err
}

func (s *Store) RemoveBan(instanceID, ip string) error {
	return s.db.Update(func(tx *bolt.Tx) error {
		return tx.Bucket(bansBucket).Delete([]byte(failureKey(instanceID, ip)))
	})
}

func (s *Store) RecordUpdateCheck(
	instanceID, version string,
	authenticated bool,
	now time.Time,
) error {
	return s.db.Update(func(tx *bolt.Tx) error {
		bucket := tx.Bucket(updateStatsBucket)
		stats := UpdateStats{Days: make(map[string]UpdateDay)}
		if raw := bucket.Get([]byte(instanceID)); raw != nil {
			if err := json.Unmarshal(raw, &stats); err != nil {
				return err
			}
		}
		dayKey := now.UTC().Format("2006-01-02")
		day := stats.Days[dayKey]
		if day.Versions == nil {
			day.Versions = make(map[string]uint64)
		}
		if authenticated {
			day.Authenticated++
		} else {
			day.Anonymous++
			instances := tx.Bucket(instancesBucket)
			instance, err := getJSON[Instance](instances, instanceID)
			if err == nil {
				instance.LastAnonymousUpdate = now.UTC()
				if err := putJSON(instances, instanceID, instance); err != nil {
					return err
				}
			}
		}
		if version != "" {
			day.Versions[version]++
		}
		stats.Days[dayKey] = day
		cutoff := now.UTC().AddDate(0, 0, -31)
		for key := range stats.Days {
			parsed, err := time.Parse("2006-01-02", key)
			if err != nil || parsed.Before(cutoff) {
				delete(stats.Days, key)
			}
		}
		return putJSON(bucket, instanceID, stats)
	})
}

func (s *Store) UpdateStatistics(instanceID string, now time.Time) (map[string]any, error) {
	stats := UpdateStats{Days: make(map[string]UpdateDay)}
	err := s.db.View(func(tx *bolt.Tx) error {
		raw := tx.Bucket(updateStatsBucket).Get([]byte(instanceID))
		if raw == nil {
			return nil
		}
		return json.Unmarshal(raw, &stats)
	})
	if err != nil {
		return nil, err
	}
	aggregate := func(days int) map[string]any {
		cutoff := now.UTC().AddDate(0, 0, -(days - 1))
		var authenticated, anonymous uint64
		versions := make(map[string]uint64)
		for key, day := range stats.Days {
			parsed, err := time.Parse("2006-01-02", key)
			if err != nil || parsed.Before(cutoff) {
				continue
			}
			authenticated += day.Authenticated
			anonymous += day.Anonymous
			for version, count := range day.Versions {
				versions[version] += count
			}
		}
		return map[string]any{
			"authenticated": authenticated,
			"anonymous":     anonymous,
			"versions":      versions,
		}
	}
	instance, _ := s.GetInstance(instanceID)
	return map[string]any{
		"last_7_days":           aggregate(7),
		"last_30_days":          aggregate(30),
		"last_anonymous_update": instance.LastAnonymousUpdate,
	}, nil
}
