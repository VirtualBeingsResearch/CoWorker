package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestInitializeCreatesSecureComposeDeployment(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "deployment")
	token, publicURL, err := initialize(initOptions{
		directory:    directory,
		publicURL:    "https://relay.example.com",
		externalPort: 8443,
		image:        "coworker-relay:test",
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(token) < 40 || publicURL != "https://relay.example.com:8443" {
		t.Fatalf("unexpected initialization result: token=%q url=%q", token, publicURL)
	}
	envPath := filepath.Join(directory, ".env")
	info, err := os.Stat(envPath)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf(".env mode is %o", info.Mode().Perm())
	}
	env, _ := os.ReadFile(envPath)
	if !strings.Contains(string(env), "RELAY_ADMIN_TOKEN="+token) ||
		!strings.Contains(string(env), "RELAY_URL="+publicURL) ||
		!strings.Contains(string(env), "RELAY_ACME_DOMAIN=relay.example.com") {
		t.Fatalf("unexpected .env: %s", env)
	}
	compose, _ := os.ReadFile(filepath.Join(directory, "compose.yaml"))
	if !strings.Contains(string(compose), `"${RELAY_EXTERNAL_PORT}:8443"`) ||
		!strings.Contains(string(compose), `"80:8080"`) {
		t.Fatalf("unexpected compose file: %s", compose)
	}
	if _, _, err := initialize(initOptions{
		directory:    directory,
		publicURL:    publicURL,
		externalPort: 8443,
		image:        "coworker-relay:test",
	}); err == nil {
		t.Fatal("initialization overwrote an existing deployment without --force")
	}
}

func TestInitializePEMModeMountsCertificates(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "deployment")
	cert := filepath.Join(t.TempDir(), "certificate.pem")
	key := filepath.Join(t.TempDir(), "private-key.pem")
	if _, _, err := initialize(initOptions{
		directory:    directory,
		publicURL:    "https://192.0.2.10:8443",
		externalPort: 8443,
		tlsCert:      cert,
		tlsKey:       key,
		image:        "coworker-relay:test",
	}); err != nil {
		t.Fatal(err)
	}
	compose, _ := os.ReadFile(filepath.Join(directory, "compose.yaml"))
	if !strings.Contains(string(compose), cert+":/run/tls/fullchain.pem:ro") ||
		!strings.Contains(string(compose), key+":/run/tls/privkey.pem:ro") {
		t.Fatalf("certificate mounts missing: %s", compose)
	}
}

func TestHTTPClientRejectsInvalidPrivateCA(t *testing.T) {
	path := filepath.Join(t.TempDir(), "invalid-ca.pem")
	if err := os.WriteFile(path, []byte("not a certificate"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("RELAY_CA_CERT", path)
	if _, err := newHTTPClient(); err == nil {
		t.Fatal("invalid private CA was accepted")
	}
}
