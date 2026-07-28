package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestInitializeCreatesPlainWebSocketComposeAndLoopbackAdmin(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "relay")
	token, publicURL, err := initialize(initOptions{
		directory: directory, publicURL: "http://203.0.113.10:8443",
		externalPort: 8443, adminPort: 9444, image: "example/relay:test",
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(token) < 32 || publicURL != "http://203.0.113.10:8443" {
		t.Fatalf("unexpected init result: %q %q", token, publicURL)
	}
	environment, _ := os.ReadFile(filepath.Join(directory, ".env"))
	compose, _ := os.ReadFile(filepath.Join(directory, "compose.yaml"))
	if strings.Contains(string(environment), "TLS") ||
		strings.Contains(string(environment), "ACME") ||
		strings.Contains(string(environment), "CACHE") {
		t.Fatalf("obsolete settings remain:\n%s", environment)
	}
	if !strings.Contains(string(compose), `"${RELAY_EXTERNAL_PORT}:8443"`) ||
		!strings.Contains(string(compose), `"127.0.0.1:${RELAY_ADMIN_PORT}:8444"`) ||
		!strings.Contains(
			string(compose),
			"RELAY_ADMIN_URL: http://127.0.0.1:8444",
		) {
		t.Fatalf("unexpected compose ports:\n%s", compose)
	}
	info, err := os.Stat(filepath.Join(directory, ".env"))
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf(".env permissions=%v err=%v", info.Mode().Perm(), err)
	}
}

func TestNormalizePublicURLAcceptsHTTPAndRequiresMatchingPort(t *testing.T) {
	_, normalized, err := normalizePublicURL("http://relay.test", 8443)
	if err != nil || normalized != "http://relay.test:8443" {
		t.Fatalf("unexpected result: %q %v", normalized, err)
	}
	if _, _, err := normalizePublicURL("https://relay.test:443", 8443); err == nil {
		t.Fatal("mismatched port was accepted")
	}
}
