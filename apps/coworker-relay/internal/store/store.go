package store

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"
	"time"

	bolt "go.etcd.io/bbolt"
)

var (
	instancesBucket  = []byte("instances")
	pairingsBucket   = []byte("pairings")
	failuresBucket   = []byte("failures")
	bansBucket       = []byte("bans")
	auditsBucket     = []byte("audits")
	trafficBucket    = []byte("traffic")
	metaBucket       = []byte("meta")
	schemaVersionKey = []byte("schema_version")
)

const currentSchemaVersion = 2

type Instance struct {
	ID                string    `json:"id"`
	Name              string    `json:"name"`
	InstancePublicKey string    `json:"instance_public_key,omitempty"`
	AuthPublicKey     string    `json:"auth_public_key,omitempty"`
	AuthEpoch         uint64    `json:"auth_epoch"`
	CreatedAt         time.Time `json:"created_at"`
	LastConnectedAt   time.Time `json:"last_connected_at,omitempty"`
}

type Pairing struct {
	ID         string    `json:"id"`
	InstanceID string    `json:"instance_id"`
	Secret     string    `json:"secret"`
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

type AuditEvent struct {
	At         time.Time `json:"at"`
	Action     string    `json:"action"`
	InstanceID string    `json:"instance_id,omitempty"`
	SourceIP   string    `json:"source_ip,omitempty"`
	Reason     string    `json:"reason,omitempty"`
}

type Traffic struct {
	InstanceID  string    `json:"instance_id"`
	Connections uint64    `json:"connections"`
	Frames      uint64    `json:"frames"`
	Bytes       uint64    `json:"bytes"`
	UpdatedAt   time.Time `json:"updated_at"`
}

type Store struct {
	db            *bolt.DB
	failureWindow time.Duration
	failureLimit  int
	banDuration   time.Duration
}

func Open(path string) (*Store, error) {
	db, err := bolt.Open(path, 0o600, &bolt.Options{Timeout: 5 * time.Second})
	if err != nil {
		return nil, err
	}
	err = db.Update(func(tx *bolt.Tx) error {
		meta, err := tx.CreateBucketIfNotExists(metaBucket)
		if err != nil {
			return err
		}
		rawVersion := meta.Get(schemaVersionKey)
		if rawVersion != nil && string(rawVersion) != "2" {
			return fmt.Errorf(
				"relay database schema %s is incompatible with E2EE Relay v1; "+
					"back it up, remove it, and run coworker-relay init again",
				string(rawVersion),
			)
		}
		for _, name := range [][]byte{
			instancesBucket, pairingsBucket, failuresBucket, bansBucket,
			auditsBucket, trafficBucket,
		} {
			if _, err := tx.CreateBucketIfNotExists(name); err != nil {
				return err
			}
		}
		return meta.Put(schemaVersionKey, []byte("2"))
	})
	if err != nil {
		_ = db.Close()
		return nil, err
	}
	return &Store{
		db: db, failureWindow: 10 * time.Minute, failureLimit: 5,
		banDuration: time.Hour,
	}, nil
}

func Validate(path string) error {
	db, err := bolt.Open(path, 0o600, &bolt.Options{ReadOnly: true, Timeout: 5 * time.Second})
	if err != nil {
		return err
	}
	defer db.Close()
	return db.View(func(tx *bolt.Tx) error {
		meta := tx.Bucket(metaBucket)
		if meta == nil || string(meta.Get(schemaVersionKey)) != "2" {
			return errors.New("backup has an unsupported or missing E2EE Relay schema")
		}
		if tx.Bucket(instancesBucket) == nil {
			return errors.New("backup is missing the instances bucket")
		}
		return nil
	})
}

func (s *Store) Close() error { return s.db.Close() }

func (s *Store) Check() error {
	return s.db.View(func(tx *bolt.Tx) error {
		if tx.Bucket(instancesBucket) == nil || tx.Bucket(metaBucket) == nil {
			return errors.New("relay database is missing required buckets")
		}
		return nil
	})
}

func (s *Store) Backup(writer io.Writer) error {
	return s.db.View(func(tx *bolt.Tx) error {
		_, err := tx.WriteTo(writer)
		return err
	})
}

func (s *Store) SetAuthPolicy(
	failureWindow time.Duration,
	failureLimit int,
	banDuration time.Duration,
) error {
	if failureWindow <= 0 || failureLimit < 1 || failureLimit > 100 || banDuration <= 0 {
		return errors.New("invalid relay authentication policy")
	}
	s.failureWindow = failureWindow
	s.failureLimit = failureLimit
	s.banDuration = banDuration
	return nil
}

func randomToken(size int) (string, error) {
	raw := make([]byte, size)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(raw), nil
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
	idToken, err := randomToken(16)
	if err != nil {
		return Instance{}, "", err
	}
	pairingID, err := randomToken(12)
	if err != nil {
		return Instance{}, "", err
	}
	secret, err := randomToken(32)
	if err != nil {
		return Instance{}, "", err
	}
	instance := Instance{
		ID:        "cw_" + strings.ToLower(idToken),
		Name:      strings.TrimSpace(name),
		CreatedAt: time.Now().UTC(),
	}
	pairing := Pairing{
		ID: pairingID, InstanceID: instance.ID, Secret: secret,
		ExpiresAt: time.Now().UTC().Add(10 * time.Minute),
	}
	err = s.db.Update(func(tx *bolt.Tx) error {
		if err := putJSON(tx.Bucket(instancesBucket), instance.ID, instance); err != nil {
			return err
		}
		return putJSON(tx.Bucket(pairingsBucket), pairing.ID, pairing)
	})
	return instance, "pair_" + pairingID + "." + secret, err
}

func (s *Store) GetPairing(id string) (Pairing, error) {
	var pairing Pairing
	err := s.db.View(func(tx *bolt.Tx) error {
		var err error
		pairing, err = getJSON[Pairing](tx.Bucket(pairingsBucket), id)
		if err != nil || pairing.Used || !pairing.ExpiresAt.After(time.Now().UTC()) {
			return errors.New("pairing code is invalid, expired, or already used")
		}
		return nil
	})
	return pairing, err
}

func (s *Store) CompletePairing(pairingID, instancePublicKey string) (Instance, error) {
	var instance Instance
	err := s.db.Update(func(tx *bolt.Tx) error {
		pairings := tx.Bucket(pairingsBucket)
		pairing, err := getJSON[Pairing](pairings, pairingID)
		if err != nil || pairing.Used || !pairing.ExpiresAt.After(time.Now().UTC()) {
			return errors.New("pairing code is invalid, expired, or already used")
		}
		instance, err = getJSON[Instance](tx.Bucket(instancesBucket), pairing.InstanceID)
		if err != nil {
			return err
		}
		if instance.InstancePublicKey != "" {
			return errors.New("instance is already paired")
		}
		instance.InstancePublicKey = instancePublicKey
		pairing.Used = true
		pairing.Secret = ""
		if err := putJSON(tx.Bucket(instancesBucket), instance.ID, instance); err != nil {
			return err
		}
		return putJSON(pairings, pairing.ID, pairing)
	})
	return instance, err
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

func (s *Store) UpdateAuthKey(id, publicKey string, epoch uint64) error {
	return s.updateInstance(id, func(instance *Instance) error {
		if epoch <= instance.AuthEpoch {
			return errors.New("authentication epoch must increase")
		}
		instance.AuthPublicKey = publicKey
		instance.AuthEpoch = epoch
		instance.LastConnectedAt = time.Now().UTC()
		return nil
	})
}

func (s *Store) TouchInstance(id string) error {
	return s.updateInstance(id, func(instance *Instance) error {
		instance.LastConnectedAt = time.Now().UTC()
		return nil
	})
}

func (s *Store) updateInstance(id string, mutate func(*Instance) error) error {
	return s.db.Update(func(tx *bolt.Tx) error {
		bucket := tx.Bucket(instancesBucket)
		instance, err := getJSON[Instance](bucket, id)
		if err != nil {
			return err
		}
		if err := mutate(&instance); err != nil {
			return err
		}
		return putJSON(bucket, id, instance)
	})
}

func (s *Store) DeleteInstance(id string) error {
	return s.db.Update(func(tx *bolt.Tx) error {
		return deleteInstance(tx, id)
	})
}

func deleteInstance(tx *bolt.Tx, id string) error {
	if err := tx.Bucket(instancesBucket).Delete([]byte(id)); err != nil {
		return err
	}
	pairings := tx.Bucket(pairingsBucket)
	var pairingKeys [][]byte
	if err := pairings.ForEach(func(key, raw []byte) error {
		var pairing Pairing
		if json.Unmarshal(raw, &pairing) == nil && pairing.InstanceID == id {
			pairingKeys = append(pairingKeys, append([]byte(nil), key...))
		}
		return nil
	}); err != nil {
		return err
	}
	for _, key := range pairingKeys {
		if err := pairings.Delete(key); err != nil {
			return err
		}
	}
	for _, bucketName := range [][]byte{failuresBucket, bansBucket} {
		bucket := tx.Bucket(bucketName)
		prefix := []byte(id + "\x00")
		var keys [][]byte
		cursor := bucket.Cursor()
		for key, _ := cursor.Seek(prefix); key != nil &&
			strings.HasPrefix(string(key), string(prefix)); key, _ = cursor.Next() {
			keys = append(keys, append([]byte(nil), key...))
		}
		for _, key := range keys {
			if err := bucket.Delete(key); err != nil {
				return err
			}
		}
	}
	return tx.Bucket(trafficBucket).Delete([]byte(id))
}

func (s *Store) RecordAudit(event AuditEvent) error {
	if event.At.IsZero() {
		event.At = time.Now().UTC()
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		return putAudit(tx, event)
	})
}

func putAudit(tx *bolt.Tx, event AuditEvent) error {
	bucket := tx.Bucket(auditsBucket)
	sequence, err := bucket.NextSequence()
	if err != nil {
		return err
	}
	key := make([]byte, 8)
	binary.BigEndian.PutUint64(key, sequence)
	raw, err := json.Marshal(event)
	if err != nil {
		return err
	}
	return bucket.Put(key, raw)
}

func (s *Store) AddTraffic(instanceID string, connections, frames, bytes uint64) error {
	if instanceID == "" || (connections == 0 && frames == 0 && bytes == 0) {
		return nil
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		bucket := tx.Bucket(trafficBucket)
		value := Traffic{InstanceID: instanceID}
		if raw := bucket.Get([]byte(instanceID)); raw != nil {
			if err := json.Unmarshal(raw, &value); err != nil {
				return err
			}
		}
		value.Connections += connections
		value.Frames += frames
		value.Bytes += bytes
		value.UpdatedAt = time.Now().UTC()
		return putJSON(bucket, instanceID, value)
	})
}

func (s *Store) TrafficTotals() (Traffic, error) {
	var total Traffic
	err := s.db.View(func(tx *bolt.Tx) error {
		return tx.Bucket(trafficBucket).ForEach(func(_, raw []byte) error {
			var value Traffic
			if err := json.Unmarshal(raw, &value); err != nil {
				return err
			}
			total.Connections += value.Connections
			total.Frames += value.Frames
			total.Bytes += value.Bytes
			if value.UpdatedAt.After(total.UpdatedAt) {
				total.UpdatedAt = value.UpdatedAt
			}
			return nil
		})
	})
	return total, err
}

func (s *Store) GarbageCollect(now time.Time) (map[string]int, error) {
	removed := map[string]int{"instances": 0, "pairings": 0, "failures": 0, "bans": 0}
	err := s.db.Update(func(tx *bolt.Tx) error {
		pairings := tx.Bucket(pairingsBucket)
		var pairingKeys [][]byte
		expiredInstanceIDs := make(map[string]struct{})
		if err := pairings.ForEach(func(key, raw []byte) error {
			var pairing Pairing
			if json.Unmarshal(raw, &pairing) != nil {
				pairingKeys = append(pairingKeys, append([]byte(nil), key...))
				return nil
			}
			if pairing.Used || !pairing.ExpiresAt.After(now) {
				pairingKeys = append(pairingKeys, append([]byte(nil), key...))
				if !pairing.Used {
					expiredInstanceIDs[pairing.InstanceID] = struct{}{}
				}
			}
			return nil
		}); err != nil {
			return err
		}
		for _, key := range pairingKeys {
			if err := pairings.Delete(key); err != nil {
				return err
			}
			removed["pairings"]++
		}
		for instanceID := range expiredInstanceIDs {
			raw := tx.Bucket(instancesBucket).Get([]byte(instanceID))
			if raw == nil {
				continue
			}
			var instance Instance
			if err := json.Unmarshal(raw, &instance); err != nil {
				return err
			}
			if instance.InstancePublicKey != "" {
				continue
			}
			hasActivePairing := false
			if err := pairings.ForEach(func(_, raw []byte) error {
				var pairing Pairing
				if json.Unmarshal(raw, &pairing) == nil &&
					pairing.InstanceID == instanceID && !pairing.Used &&
					pairing.ExpiresAt.After(now) {
					hasActivePairing = true
				}
				return nil
			}); err != nil {
				return err
			}
			if hasActivePairing {
				continue
			}
			if err := deleteInstance(tx, instanceID); err != nil {
				return err
			}
			if err := putAudit(tx, AuditEvent{
				At: now, Action: "instance.expire", InstanceID: instanceID,
			}); err != nil {
				return err
			}
			removed["instances"]++
		}
		for name, bucketName := range map[string][]byte{
			"failures": failuresBucket, "bans": bansBucket,
		} {
			bucket := tx.Bucket(bucketName)
			var keys [][]byte
			if err := bucket.ForEach(func(key, raw []byte) error {
				expired := false
				switch name {
				case "failures":
					var values []time.Time
					expired = json.Unmarshal(raw, &values) != nil
					if !expired {
						expired = true
						cutoff := now.Add(-s.failureWindow)
						for _, value := range values {
							if value.After(cutoff) {
								expired = false
								break
							}
						}
					}
				case "bans":
					var value Ban
					expired = json.Unmarshal(raw, &value) != nil || !value.Until.After(now)
				}
				if expired {
					keys = append(keys, append([]byte(nil), key...))
				}
				return nil
			}); err != nil {
				return err
			}
			for _, key := range keys {
				if err := bucket.Delete(key); err != nil {
					return err
				}
				removed[name]++
			}
		}
		return nil
	})
	return removed, err
}

func (s *Store) ListInstances() ([]Instance, error) {
	var result []Instance
	err := s.db.View(func(tx *bolt.Tx) error {
		return tx.Bucket(instancesBucket).ForEach(func(_, raw []byte) error {
			var instance Instance
			if err := json.Unmarshal(raw, &instance); err != nil {
				return err
			}
			result = append(result, instance)
			return nil
		})
	})
	sort.Slice(result, func(i, j int) bool { return result[i].CreatedAt.Before(result[j].CreatedAt) })
	return result, err
}

func authKey(instanceID, ip string) string { return instanceID + "\x00" + ip }

func (s *Store) RecordFailure(instanceID, ip string, now time.Time) (bool, time.Time, error) {
	var banned bool
	var until time.Time
	err := s.db.Update(func(tx *bolt.Tx) error {
		key := authKey(instanceID, ip)
		bucket := tx.Bucket(failuresBucket)
		var values []time.Time
		if raw := bucket.Get([]byte(key)); raw != nil {
			_ = json.Unmarshal(raw, &values)
		}
		cutoff := now.Add(-s.failureWindow)
		filtered := values[:0]
		for _, value := range values {
			if value.After(cutoff) {
				filtered = append(filtered, value)
			}
		}
		filtered = append(filtered, now)
		if len(filtered) >= s.failureLimit {
			until = now.Add(s.banDuration)
			ban := Ban{
				InstanceID: instanceID, IP: ip, Until: until,
				Reason: "authentication failures", CreatedAt: now,
			}
			if err := putJSON(tx.Bucket(bansBucket), key, ban); err != nil {
				return err
			}
			banned = true
			return bucket.Delete([]byte(key))
		}
		return putJSON(bucket, key, filtered)
	})
	return banned, until, err
}

func (s *Store) ActiveBan(instanceID, ip string, now time.Time) (Ban, bool, error) {
	var ban Ban
	key := authKey(instanceID, ip)
	err := s.db.Update(func(tx *bolt.Tx) error {
		bucket := tx.Bucket(bansBucket)
		raw := bucket.Get([]byte(key))
		if raw == nil {
			return nil
		}
		if err := json.Unmarshal(raw, &ban); err != nil {
			return bucket.Delete([]byte(key))
		}
		if !ban.Until.After(now) {
			ban = Ban{}
			return bucket.Delete([]byte(key))
		}
		return nil
	})
	return ban, !ban.Until.IsZero(), err
}

func (s *Store) ListBans(instanceID string) ([]Ban, error) {
	var result []Ban
	err := s.db.View(func(tx *bolt.Tx) error {
		return tx.Bucket(bansBucket).ForEach(func(_, raw []byte) error {
			var ban Ban
			if err := json.Unmarshal(raw, &ban); err != nil {
				return err
			}
			if instanceID == "" || ban.InstanceID == instanceID {
				result = append(result, ban)
			}
			return nil
		})
	})
	sort.Slice(result, func(i, j int) bool { return result[i].Until.Before(result[j].Until) })
	return result, err
}

func (s *Store) RemoveBan(instanceID, ip string) error {
	return s.db.Update(func(tx *bolt.Tx) error {
		return tx.Bucket(bansBucket).Delete([]byte(authKey(instanceID, ip)))
	})
}

func (s *Store) RemoveBanWithAudit(instanceID, ip, reason string) error {
	return s.db.Update(func(tx *bolt.Tx) error {
		key := []byte(authKey(instanceID, ip))
		if err := tx.Bucket(bansBucket).Delete(key); err != nil {
			return err
		}
		if err := tx.Bucket(failuresBucket).Delete(key); err != nil {
			return err
		}
		return putAudit(tx, AuditEvent{
			At: time.Now().UTC(), Action: "ban.remove",
			InstanceID: instanceID, SourceIP: ip, Reason: reason,
		})
	})
}
