package cache

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"hash"
	"io"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

type Header [2]string

type Metadata struct {
	InstanceID string    `json:"instance_id,omitempty"`
	Status     int       `json:"status"`
	Headers    []Header  `json:"headers"`
	Size       int64     `json:"size"`
	SHA256     string    `json:"sha256"`
	CreatedAt  time.Time `json:"created_at"`
}

type Entry struct {
	Metadata Metadata
	Path     string
}

type keyLock struct {
	mutex sync.Mutex
	refs  int
}

type verifiedFile struct {
	size    int64
	modTime time.Time
	digest  string
	checked time.Time
}

const validationInterval = 15 * time.Minute

type Cache struct {
	root      string
	maxBytes  int64
	locksMu   sync.Mutex
	locks     map[string]*keyLock
	verified  map[string]verifiedFile
	healthMu  sync.Mutex
	healthAt  time.Time
	healthErr error
}

func New(root string, maxBytes int64) (*Cache, error) {
	if maxBytes <= 0 {
		maxBytes = 4 << 30
	}
	if err := os.MkdirAll(root, 0o700); err != nil {
		return nil, err
	}
	return &Cache{
		root: root, maxBytes: maxBytes,
		locks: make(map[string]*keyLock), verified: make(map[string]verifiedFile),
	}, nil
}

func Key(instanceID, target string) string {
	sum := sha256.Sum256([]byte(instanceID + "\x00" + target))
	return hex.EncodeToString(sum[:])
}

func (c *Cache) Lock(key string) func() {
	c.locksMu.Lock()
	entry := c.locks[key]
	if entry == nil {
		entry = &keyLock{}
		c.locks[key] = entry
	}
	entry.refs++
	c.locksMu.Unlock()
	entry.mutex.Lock()
	return func() {
		entry.mutex.Unlock()
		c.locksMu.Lock()
		entry.refs--
		if entry.refs == 0 && c.locks[key] == entry {
			delete(c.locks, key)
		}
		c.locksMu.Unlock()
	}
}

func (c *Cache) Get(key string) (Entry, bool) {
	var entry Entry
	metaPath, bodyPath := c.paths(key)
	raw, err := os.ReadFile(metaPath)
	if err != nil || json.Unmarshal(raw, &entry.Metadata) != nil {
		c.removeEntry(key, metaPath, bodyPath)
		return Entry{}, false
	}
	info, err := os.Stat(bodyPath)
	if err != nil || info.Size() != entry.Metadata.Size {
		c.removeEntry(key, metaPath, bodyPath)
		return Entry{}, false
	}
	c.locksMu.Lock()
	verified := c.verified[key]
	unchanged := verified.size == info.Size() &&
		verified.modTime.Equal(info.ModTime()) &&
		verified.digest == entry.Metadata.SHA256 &&
		time.Since(verified.checked) < validationInterval
	c.locksMu.Unlock()
	if !unchanged {
		digest, err := fileSHA256(bodyPath)
		if err != nil || digest != entry.Metadata.SHA256 {
			c.removeEntry(key, metaPath, bodyPath)
			return Entry{}, false
		}
		c.locksMu.Lock()
		c.verified[key] = verifiedFile{
			size: info.Size(), modTime: info.ModTime(), digest: digest,
			checked: time.Now(),
		}
		c.locksMu.Unlock()
	}
	now := time.Now()
	_ = os.Chtimes(metaPath, now, now)
	entry.Path = bodyPath
	return entry, true
}

func (c *Cache) removeEntry(key string, paths ...string) {
	for _, path := range paths {
		_ = os.Remove(path)
	}
	c.locksMu.Lock()
	delete(c.verified, key)
	c.locksMu.Unlock()
}

func fileSHA256(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	digest := sha256.New()
	if _, err := io.Copy(digest, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(digest.Sum(nil)), nil
}

type Writer struct {
	cache    *Cache
	key      string
	file     *os.File
	hash     hashWriter
	size     int64
	metadata Metadata
}

type hashWriter struct{ value hash.Hash }

func (c *Cache) Begin(key, instanceID string, status int, headers []Header) (*Writer, error) {
	temporary, err := os.CreateTemp(c.root, ".relay-cache-*")
	if err != nil {
		return nil, err
	}
	return &Writer{
		cache: c, key: key, file: temporary,
		hash: hashWriter{value: sha256.New()},
		metadata: Metadata{
			InstanceID: instanceID,
			Status:     status, Headers: headers, CreatedAt: time.Now().UTC(),
		},
	}, nil
}

func (w *Writer) Write(value []byte) error {
	count, err := w.file.Write(value)
	if err == nil {
		_, _ = w.hash.value.Write(value[:count])
		w.size += int64(count)
	}
	return err
}

func (w *Writer) Abort() {
	name := w.file.Name()
	_ = w.file.Close()
	_ = os.Remove(name)
}

func (w *Writer) Commit() error {
	if err := w.file.Sync(); err != nil {
		w.Abort()
		return err
	}
	if err := w.file.Close(); err != nil {
		w.Abort()
		return err
	}
	metaPath, bodyPath := w.cache.paths(w.key)
	if err := os.Rename(w.file.Name(), bodyPath); err != nil {
		w.Abort()
		return err
	}
	w.metadata.Size = w.size
	w.metadata.SHA256 = hex.EncodeToString(w.hash.value.Sum(nil))
	raw, err := json.Marshal(w.metadata)
	if err != nil {
		return err
	}
	tempMeta := metaPath + ".tmp"
	if err := os.WriteFile(tempMeta, raw, 0o600); err != nil {
		return err
	}
	if err := os.Rename(tempMeta, metaPath); err != nil {
		return err
	}
	if info, err := os.Stat(bodyPath); err == nil {
		w.cache.locksMu.Lock()
		w.cache.verified[w.key] = verifiedFile{
			size: info.Size(), modTime: info.ModTime(), digest: w.metadata.SHA256,
			checked: time.Now(),
		}
		w.cache.locksMu.Unlock()
	}
	return w.cache.prune()
}

func (c *Cache) Purge() error {
	entries, err := os.ReadDir(c.root)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		if err := os.Remove(filepath.Join(c.root, entry.Name())); err != nil &&
			!errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	c.locksMu.Lock()
	clear(c.verified)
	c.locksMu.Unlock()
	return nil
}

func (c *Cache) PurgeInstance(instanceID string) error {
	return c.purgeMatching(func(metadata Metadata) bool {
		return metadata.InstanceID == instanceID
	})
}

func (c *Cache) purgeMatching(match func(Metadata) bool) error {
	entries, err := os.ReadDir(c.root)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if entry.IsDir() || filepath.Ext(entry.Name()) != ".json" {
			continue
		}
		metaPath := filepath.Join(c.root, entry.Name())
		raw, readErr := os.ReadFile(metaPath)
		var metadata Metadata
		if readErr == nil && json.Unmarshal(raw, &metadata) == nil && !match(metadata) {
			continue
		}
		key := entry.Name()[:len(entry.Name())-len(".json")]
		for _, path := range []string{metaPath, filepath.Join(c.root, key+".body")} {
			if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
				return err
			}
		}
		c.locksMu.Lock()
		delete(c.verified, key)
		c.locksMu.Unlock()
	}
	return nil
}

func (c *Cache) Check() error {
	c.healthMu.Lock()
	defer c.healthMu.Unlock()
	if time.Since(c.healthAt) < 10*time.Second {
		return c.healthErr
	}
	c.healthAt = time.Now()
	probe, err := os.CreateTemp(c.root, ".health-*")
	if err != nil {
		c.healthErr = err
		return err
	}
	name := probe.Name()
	if err := probe.Close(); err != nil {
		_ = os.Remove(name)
		c.healthErr = err
		return err
	}
	c.healthErr = os.Remove(name)
	return c.healthErr
}

func (c *Cache) Stats() (map[string]int64, error) {
	entries, err := os.ReadDir(c.root)
	if err != nil {
		return nil, err
	}
	var files, bytes int64
	for _, entry := range entries {
		if entry.IsDir() || filepath.Ext(entry.Name()) != ".body" {
			continue
		}
		info, err := entry.Info()
		if err == nil {
			files++
			bytes += info.Size()
		}
	}
	return map[string]int64{"entries": files, "bytes": bytes, "max_bytes": c.maxBytes}, nil
}

func (c *Cache) paths(key string) (string, string) {
	return filepath.Join(c.root, key+".json"), filepath.Join(c.root, key+".body")
}

func (c *Cache) prune() error {
	type item struct {
		path string
		key  string
		size int64
		used time.Time
	}
	entries, err := os.ReadDir(c.root)
	if err != nil {
		return err
	}
	var items []item
	var total int64
	for _, entry := range entries {
		if entry.IsDir() || filepath.Ext(entry.Name()) != ".body" {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			continue
		}
		used := info.ModTime()
		key := entry.Name()[:len(entry.Name())-len(".body")]
		if metadataInfo, metadataErr := os.Stat(filepath.Join(c.root, key+".json")); metadataErr == nil {
			used = metadataInfo.ModTime()
		}
		total += info.Size()
		items = append(items, item{
			path: filepath.Join(c.root, entry.Name()),
			key:  key,
			size: info.Size(), used: used,
		})
	}
	sort.Slice(items, func(i, j int) bool { return items[i].used.Before(items[j].used) })
	for _, entry := range items {
		if total <= c.maxBytes {
			break
		}
		_ = os.Remove(entry.path)
		_ = os.Remove(filepath.Join(c.root, entry.key+".json"))
		c.locksMu.Lock()
		delete(c.verified, entry.key)
		c.locksMu.Unlock()
		total -= entry.size
	}
	return nil
}
